"""mnemosyne phase — inspect and maintain phase.md cards across all projects.

Subcommands:

    mnemosyne phase backfill   — generate or refresh phase.md across every
                                 gsd-planning/phases/<dir>/ in the active vault

Phase 37 (this phase) ships ``backfill``. Phase 38 will add lifecycle
transition commands (``mnemosyne phase set-status``, etc.) that consume
the same derivation library at ``mnemosyne_cli.lib.phase_card``.

Security mitigations (see Phase 37 PLAN §threat_model):

- T-37-01 — ``validate_project_slug`` is the first thing called on the
  ``--project`` flag, before any filesystem operation.
- T-37-02 — all subprocess invocations in this command and in
  ``lib/phase_card.py`` use list-form arg vectors. The shell-injection
  switch is never enabled on subprocess.run.
- T-37-03 — frontmatter writes go through ``frontmatter.dumps`` which
  handles YAML escaping. We never string-concatenate into YAML.
- T-37-04 — multi-vault iteration consults
  ``lib_vault.can_read(primary, target)`` before touching any vault
  other than the primary. Closed by default per Phase 19-03.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import frontmatter
import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib import vault as lib_vault
from mnemosyne_cli.lib.phase_card import (
    _PHASE_OPTIONAL_EVENTS,
    _VALID_EVENTS,
    apply_event,
    card_from_frontmatter,
    derive_phase_card,
    discover_phase_dirs,
    read_current_phase_from_state,
    resolve_phase_dir,
    validate_project_slug,
    write_phase_md,
    write_phase_md_atomic,
)

app = typer.Typer(no_args_is_help=True, help="Inspect and maintain phase.md cards.")
console = Console()
error_console = Console(stderr=True, style="bold red")

# Plain stderr console for the `update` command — no color codes that could
# confuse downstream parsing (Pitfall 6: don't pollute stdout or add ANSI).
_update_stderr = Console(stderr=True)


_ACTION_STYLES = {
    "created": "green",
    "updated": "yellow",
    "unchanged": "dim",
    "dry-run": "cyan",
}


@app.command("backfill")
def backfill(
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Limit to one project (vault-relative slug under projects/, "
            "e.g. 'empiria/mnemosyne')"
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print proposed changes without writing any files",
    ),
) -> None:
    """Generate or refresh phase.md across every ``gsd-planning/phases/<dir>/``.

    Idempotent: re-runs only update changed fields (frontmatter-only writes;
    existing user-edited bodies are preserved verbatim).

    Multi-vault: iterates every vault registered in
    ``~/.config/mnemosyne/config.toml`` IF the primary vault has
    ``can_read`` permission via ``[[vault_rules]]``. Vaults the primary
    cannot read are silently skipped (closed by default — Phase 19-03).

    See ``docs/reference/vault-taxonomy.md`` §Phase Cards for the schema.
    """
    # T-37-04 — resolve every registered vault, then filter by can_read.
    # In single-vault setups, ``resolve_vaults()`` falls back to a one-entry
    # list wrapping the resolved vault path.
    vaults = lib_vault.resolve_vaults()
    if not vaults:
        error_console.print("Cannot locate any Mnemosyne vault.")
        raise typer.Exit(1)

    try:
        primary = lib_vault.resolve_primary_vault()
    except SystemExit:
        # resolve_primary_vault raises typer.Exit when vault path
        # cannot be resolved at all — match that behaviour.
        raise

    table = Table(
        title=(
            "Phase backfill — proposed changes"
            if dry_run
            else "Phase backfill"
        )
    )
    table.add_column("Vault")
    table.add_column("Project")
    table.add_column("Phase #")
    table.add_column("Status")
    table.add_column("Action")

    totals = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "dry-run": 0,
        "skipped": 0,
    }

    for vault_cfg in vaults:
        # T-37-04: respect can_read permission for any non-primary vault.
        # Primary is exempt — it is always allowed to write to itself.
        if vault_cfg.name != primary.name:
            if not lib_vault.can_read(primary.name, vault_cfg.name):
                continue  # silent skip per closed-by-default security model

        vault_path = Path(vault_cfg.path)

        try:
            phase_dirs = discover_phase_dirs(vault_path, project)
        except ValueError as exc:
            # T-37-01 — invalid --project slug is a user error; surface it
            # and exit non-zero so test_path_traversal_rejected passes.
            error_console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(1)

        for phase_dir in phase_dirs:
            try:
                card = derive_phase_card(
                    phase_dir, vault_path, console=error_console
                )
            except Exception as exc:  # noqa: BLE001 — backfill must not abort the run
                error_console.print(
                    f"[yellow]warning:[/yellow] skipping {phase_dir}: {exc}"
                )
                totals["skipped"] += 1
                continue

            action = write_phase_md(phase_dir, card, dry_run=dry_run)
            totals[action] = totals.get(action, 0) + 1

            # Display: vault-relative "org/code" path for the project
            # column. phase_dir = projects/<org>/<code>/gsd-planning/phases/<dir>
            # → three .parents up = projects/<org>/<code>.
            try:
                project_rel = phase_dir.parent.parent.parent.relative_to(
                    vault_path / "projects"
                )
                project_label = str(project_rel)
            except ValueError:
                project_label = phase_dir.parent.parent.name

            style = _ACTION_STYLES.get(action, "white")
            table.add_row(
                vault_cfg.name,
                project_label,
                card.phase_number,
                card.status,
                f"[{style}]{action}[/{style}]",
            )

    console.print(table)
    summary_bits = [f"{k}={v}" for k, v in totals.items() if v]
    console.print(
        f"\n[bold]Totals:[/bold] {', '.join(summary_bits) or 'no changes'}"
    )
    if dry_run:
        console.print("[cyan]Dry run — no files written.[/cyan]")


# --------------------------------------------------------------------------- #
# Phase 38 — `mnemosyne phase update` command                                 #
# --------------------------------------------------------------------------- #


def _replace_field(card, field_name: str, value):
    """Tiny helper: dataclass replace for one field."""
    from dataclasses import replace as _replace
    return _replace(card, **{field_name: value})


@app.command("update")
def update(
    # --phase is OPTIONAL per RESEARCH.md Open Question 4 + Pitfall 2 resolution.
    # For blocked/unblocked events, omit and Python falls back to STATE.md's `Current Phase`.
    # For other events, omitting --phase emits a stderr warning and exits 0 (D-08).
    phase: Optional[str] = typer.Option(
        None,
        "--phase",
        help=(
            "Phase identifier (e.g. '38', '195.02', 'empiria-01'); "
            "omit for blocked/unblocked events to use STATE.md current phase."
        ),
    ),
    event: str = typer.Option(
        ..., "--event", help=f"One of: {', '.join(sorted(_VALID_EVENTS))}"
    ),
    reason: Optional[str] = typer.Option(
        None, "--reason", help="Blocker reason (required for --event blocked)."
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help="Vault-relative project path (e.g. 'empiria/mnemosyne').",
    ),
) -> None:
    """Apply a single lifecycle event to one phase.md card.

    Invoked by the gsd-sdk shim at each lifecycle touchpoint. Self-heals
    a missing phase.md by deriving from STATE.md + ROADMAP.md + git log (D-07).

    Silent no-op on resolution failure (D-08): exits 0 with stderr warning so
    the calling GSD command never fails because of phase.md issues.
    """
    # T-38-input (sec-2): validate event enum
    if event not in _VALID_EVENTS:
        _update_stderr.print(
            f"phase.md update skipped: unknown event {event!r}. Valid: {sorted(_VALID_EVENTS)}"
        )
        raise typer.Exit(0)

    # Resolve vault
    try:
        vault = lib_vault.resolve_vault_path()
    except Exception as e:
        _update_stderr.print(f"phase.md update skipped: cannot resolve vault path ({e})")
        raise typer.Exit(0)

    # T-37-01 (sec-1): validate --project against path traversal
    if project is not None:
        try:
            validate_project_slug(project, vault)
        except Exception as e:
            _update_stderr.print(
                f"phase.md update skipped: invalid --project {project!r} ({e})"
            )
            raise typer.Exit(0)

    # --- STATE.md fallback for missing --phase (Pitfall 2 / Open Question 4) ---
    # Allow --phase to be omitted only for blocker/unblocker events. For all
    # other events, silently skip with a stderr warning (D-08) — we cannot
    # invent a phase ID safely. For blocker events, read STATE.md's `Current Phase`.
    if phase is None:
        if event not in _PHASE_OPTIONAL_EVENTS:
            _update_stderr.print(
                f"phase.md update skipped: --phase is required for event {event!r} "
                "(only blocked/unblocked may omit it)"
            )
            raise typer.Exit(0)
        phase = read_current_phase_from_state(vault, project)
        if phase is None:
            _update_stderr.print(
                "phase.md update skipped: cannot determine current phase from STATE.md "
                "(no --phase passed, no `Current Phase` field)"
            )
            raise typer.Exit(0)

    phase_dir = resolve_phase_dir(vault, phase, project=project)
    if phase_dir is None:
        _update_stderr.print(
            f"phase.md update skipped: cannot resolve phase dir for {phase!r}"
        )
        raise typer.Exit(0)

    phase_md = phase_dir / "phase.md"

    # Load or self-heal (D-07)
    if phase_md.exists():
        try:
            existing = frontmatter.load(phase_md)
            card = card_from_frontmatter(dict(existing.metadata))
            body = existing.content
        except Exception as e:
            _update_stderr.print(
                f"phase.md update skipped: cannot parse existing phase.md ({e})"
            )
            raise typer.Exit(0)
    else:
        try:
            card = derive_phase_card(phase_dir, vault, console=_update_stderr)
            body = ""
        except Exception as e:
            _update_stderr.print(
                f"phase.md update skipped: cannot derive phase card ({e})"
            )
            raise typer.Exit(0)

    # For 'complete' and 'unblocked' events, re-derive to pull in
    # summary_doc / restore prior status (Pitfall 4 and D-03 row 6).
    if event == "complete":
        try:
            derived = derive_phase_card(phase_dir, vault, console=_update_stderr)
            card = _replace_field(card, "summary_doc", derived.summary_doc)
        except Exception:
            pass  # non-fatal — continue without updated summary_doc
    elif event == "unblocked":
        try:
            derived = derive_phase_card(phase_dir, vault, console=_update_stderr)
            card = _replace_field(card, "status", derived.status)
        except Exception:
            pass  # non-fatal — apply_event will still clear blocked_on

    # Apply the event mutation (T-38-input enum check inside apply_event too)
    try:
        new_card = apply_event(card, event, reason=reason)
    except ValueError as e:
        _update_stderr.print(f"phase.md update skipped: {e}")
        raise typer.Exit(0)

    # Atomic write (ACC-38-08) — write failure IS a real error
    try:
        write_phase_md_atomic(phase_md, new_card, body=body)
    except Exception as e:
        _update_stderr.print(f"phase.md update failed: {e}")
        raise typer.Exit(1)
