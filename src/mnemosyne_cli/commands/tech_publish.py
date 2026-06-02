"""mnemosyne tech-publish — publish the tech sharing set to a client vault (direct mode).

Implements the §4.3 steps 5–9 pipeline end-to-end:
- Stage the closure walker's in-set with SPDX frontmatter injection.
- Render LICENSE.md and THIRD-PARTY-NOTICES.md.
- Write PUBLISHED.json provenance.
- Commit and push to the target vault via a deploy key.

All logic lives in :func:`~mnemosyne_cli.share.publish.run_publish`; this module
is a thin Typer adapter.
"""

from __future__ import annotations

import typer
from rich.console import Console

from mnemosyne_cli.share.publish import PublishError, PublishResult, run_publish

console = Console()
error_console = Console(stderr=True, style="bold red")


def run(
    client: str = typer.Option(
        ...,
        "--client",
        help="Client slug to publish to (e.g. 'friendly-fox').",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Override detect-and-refuse on client edits (D-04).",
    ),
    skip_review_check: bool = typer.Option(
        False,
        "--skip-review-check",
        help=(
            "Bypass the licence-template review gate (D-10). "
            "Emits a loud warning. For use only when you have reviewed the "
            "template out-of-band."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Show the publish plan (files to write/delete) without making any "
            "changes to the target vault. No staging, no commit, no push."
        ),
    ),
) -> None:
    """Publish the technology sharing set to a registered client vault (direct mode).

    Resolves the share-manifest for CLIENT, runs the Phase 48 closure walker,
    applies the breach policy (refuse/warn/strip), stages in-set notes with SPDX
    frontmatter, renders LICENSE.md and THIRD-PARTY-NOTICES.md, writes
    PUBLISHED.json, then commits and pushes to the target's main branch via the
    configured deploy key.

    Re-runs are idempotent: a zero-source-change run prints "nothing to publish"
    and performs no commit/push (D-06).
    """
    # Normalise OptionInfo sentinels (for programmatic / test invocations where
    # Typer has not processed the defaults yet)
    if isinstance(client, typer.OptionInfo):  # type: ignore[arg-type]
        client = client.default  # type: ignore[assignment]
    if isinstance(force, typer.OptionInfo):  # type: ignore[arg-type]
        force = force.default  # type: ignore[assignment]
    if isinstance(skip_review_check, typer.OptionInfo):  # type: ignore[arg-type]
        skip_review_check = skip_review_check.default  # type: ignore[assignment]
    if isinstance(dry_run, typer.OptionInfo):  # type: ignore[arg-type]
        dry_run = dry_run.default  # type: ignore[assignment]

    try:
        result: PublishResult = run_publish(
            client=client,
            force=bool(force),
            skip_review_check=bool(skip_review_check),
            dry_run=bool(dry_run),
        )
    except PublishError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    if not result.success:
        error_console.print(f"[bold red]Publish failed:[/bold red] {result.message}")
        raise typer.Exit(1)

    if result.published:
        console.print(f"[bold green]Published:[/bold green] {result.message}")
    else:
        console.print(result.message)
