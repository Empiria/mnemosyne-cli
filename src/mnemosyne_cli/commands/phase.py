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

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib import vault as lib_vault
from mnemosyne_cli.lib.phase_card import (
    derive_phase_card,
    discover_phase_dirs,
    write_phase_md,
)

app = typer.Typer(no_args_is_help=True, help="Inspect and maintain phase.md cards.")
console = Console()
error_console = Console(stderr=True, style="bold red")


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
