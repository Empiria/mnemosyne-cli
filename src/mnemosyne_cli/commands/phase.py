"""mnemosyne phase — inspect and maintain phase.md cards across all projects.

Subcommands:

    mnemosyne phase backfill   — generate or refresh phase.md across every
                                 gsd-planning/phases/<dir>/ in the active vault

Phase 37 (this phase) ships `backfill`. Phase 38 will add lifecycle
transition commands (mnemosyne phase set-status, etc.) that consume the same
derivation library at `mnemosyne_cli.lib.phase_card`.
"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True, help="Inspect and maintain phase.md cards.")
console = Console()
error_console = Console(stderr=True, style="bold red")


@app.command("backfill")
def backfill(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit to one project (vault-relative path under projects/, e.g. 'empiria/mnemosyne')",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print proposed changes without writing any files",
    ),
) -> None:
    """Generate or refresh phase.md across every gsd-planning/phases/<dir>/.

    Idempotent: re-runs only update changed fields (frontmatter-only writes;
    existing user-edited bodies are preserved).

    Phase 37 P2 ships the skeleton; P3 implements the write loop. This
    command currently raises NotImplementedError — the implementation lands
    in P3.
    """
    raise NotImplementedError(
        "mnemosyne phase backfill: writer implementation lands in Phase 37 P3. "
        "P2 ships the read-side derivation library (mnemosyne_cli.lib.phase_card)."
    )
