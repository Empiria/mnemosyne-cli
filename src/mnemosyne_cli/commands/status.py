"""mnemosyne status — show vault sync status."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mnemosyne_cli.lib import git, vault

console = Console()
error_console = Console(stderr=True, style="bold red")

CACHE_FILE = Path.home() / ".claude" / "cache" / "mnemosyne-status.json"


def _get_branch(repo_path: Path) -> str:
    """Return the current branch name."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "unknown"


def _get_last_pull_timestamp(repo_path: Path, branch: str = "main") -> int | None:
    """Return UNIX timestamp of the last commit on origin/<branch>, or None."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "-1", "--format=%ct", f"origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            return int(result.stdout.strip())
        except ValueError:
            pass
    return None


def _format_timestamp(ts: int | None) -> str:
    """Format a UNIX timestamp as a human-readable string."""
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _write_cache(behind: int, ahead: int, checked: int) -> None:
    """Write status to the cache file."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"behind": behind, "ahead": ahead, "checked": checked})
    )


def run(
    json_output: bool = typer.Option(
        False, "--json", help="Output machine-readable JSON"
    ),
) -> None:
    """Show Mnemosyne vault sync status."""
    vault_path = vault.resolve_vault_path()

    # Fetch origin with a spinner
    with console.status("Fetching origin..."):
        git.fetch_origin(vault_path)

    behind, ahead = git.get_behind_ahead(vault_path)
    branch = _get_branch(vault_path)
    last_pull_ts = _get_last_pull_timestamp(vault_path)
    checked = int(time.time())

    # Write cache unconditionally on every invocation
    _write_cache(behind, ahead, checked)

    # Gather vault worktree data (used by both JSON and Rich output paths)
    vault_wt_data: list[dict] = []
    try:
        all_worktrees = git.list_worktrees(vault_path)
        worktrees_dir = vault_path / "worktrees"
        for wt in all_worktrees:
            if Path(wt["worktree"]).is_relative_to(worktrees_dir):
                wt_branch = wt.get("branch", "(detached)")
                vault_wt_data.append({
                    "branch": wt_branch,
                    "path": wt["worktree"],
                    "merged": git.is_branch_merged_to_main(vault_path, wt_branch),
                })
    except subprocess.CalledProcessError:
        pass

    status_data = {"behind": behind, "ahead": ahead, "checked": checked, "vault_worktrees": vault_wt_data}

    if json_output:
        typer.echo(json.dumps(status_data))
        return

    # Build Rich dashboard panel
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    table.add_row("Branch", branch)
    table.add_row(
        "Behind",
        f"[red]{behind}[/red]" if behind > 0 else f"[green]{behind}[/green]",
    )
    table.add_row(
        "Ahead",
        f"[yellow]{ahead}[/yellow]" if ahead > 0 else str(ahead),
    )
    table.add_row("Last commit (origin/main)", _format_timestamp(last_pull_ts))
    table.add_row("Checked", _format_timestamp(checked))

    console.print(Panel(table, title="Mnemosyne Status", border_style="cyan"))

    if behind > 0:
        console.print(
            f"\n[yellow]Vault is {behind} commit(s) behind origin/main.[/yellow]"
        )
        console.print(f"  Run: [bold]cd {vault_path} && git pull[/bold]")

    # Vault Worktrees section
    if vault_wt_data:
        wt_table = Table(title="Vault Worktrees", show_lines=False)
        wt_table.add_column("Branch", style="cyan", no_wrap=True)
        wt_table.add_column("Status", no_wrap=True)

        for wt in vault_wt_data:
            if wt["merged"]:
                status_str = "[green]merged[/green]"
            else:
                status_str = "[yellow]unmerged[/yellow]"
            wt_table.add_row(wt["branch"], status_str)

        console.print()
        console.print(wt_table)

        unmerged = [wt["branch"] for wt in vault_wt_data if not wt["merged"]]
        if unmerged:
            console.print(
                f"\n[yellow]{len(unmerged)} vault worktree(s) with unmerged work.[/yellow]"
            )
