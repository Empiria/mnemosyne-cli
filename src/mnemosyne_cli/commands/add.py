"""mnemosyne add — scaffold vault-side project structure."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault

console = Console()
error_console = Console(stderr=True, style="bold red")

PROJECT_NOTE_TEMPLATE = """\
---
tags:
  - project
status: active
organisation: "[[{org}]]"
repositories: []
created: {today}
updated: {today}
---

# {project}
"""


def run(
    org: str = typer.Argument(None, help="Organisation name (vault directory)"),
    project: str = typer.Argument(None, help="Project name"),
) -> None:
    """Scaffold vault-side project structure for a new client project."""
    vault_path = vault.resolve_vault_path()

    if org is None:
        org = typer.prompt("Organisation name")
    if project is None:
        project = typer.prompt("Project name")

    project_dir = vault_path / "projects" / org / project

    # Idempotency check
    if project_dir.exists():
        console.print(
            f"[yellow]Project already exists:[/yellow] projects/{org}/{project}"
        )
        console.print("No changes made.")
        return

    today = date.today().isoformat()

    # Create directory structure
    dirs = [
        project_dir / "gsd-planning",
        project_dir / "claude-config" / "rules",
        project_dir / "claude-config" / "commands",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]Created[/green] {d.relative_to(vault_path)}/")

    # Create AGENTS.md from template
    agents_md = project_dir / "AGENTS.md"
    template_path = vault_path / "templates" / "AGENTS.md"
    if template_path.exists():
        template_text = template_path.read_text()
    else:
        error_console.print(
            f"[yellow]Warning:[/yellow] Template not found at {template_path}, using minimal fallback"
        )
        template_text = "# {project} — Agent Instructions\n"
    agents_md.write_text(template_text.format(org=org, project=project))
    console.print(f"[green]Created[/green] {agents_md.relative_to(vault_path)}")

    # Create project note with frontmatter
    project_note = project_dir / f"{project}.md"
    project_note.write_text(
        PROJECT_NOTE_TEMPLATE.format(org=org, project=project, today=today)
    )
    console.print(f"[green]Created[/green] {project_note.relative_to(vault_path)}")

    console.print()
    console.print(
        f"[bold green]Done.[/bold green] "
        f"Vault project [cyan]projects/{org}/{project}[/cyan] is ready."
    )
    console.print(
        "\nNext step: Run [bold]mnemosyne init[/bold] from your client codebase to wire it up."
    )
