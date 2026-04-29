"""mnemosyne add — scaffold vault-side project structure."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault
from mnemosyne_cli.lib.symlinks import DEFAULT_SKILLS, SKILLS_YAML_FILENAME

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


def _discover_technologies(vault_path: Path) -> list[str]:
    """Return sorted list of technology directory names that have an index.md."""
    tech_dir = vault_path / "technologies"
    if not tech_dir.is_dir():
        return []
    return sorted(
        d.name
        for d in tech_dir.iterdir()
        if d.is_dir() and (d / "index.md").exists()
    )


def _prompt_tech_stack(available: list[str]) -> list[str]:
    """Interactively prompt user to select technologies for the project."""
    if not available:
        console.print("[yellow]No technologies found in vault.[/yellow]")
        return []

    console.print("\n[bold]Available technologies:[/bold]")
    for i, tech in enumerate(available, 1):
        has_brief = "✓" if (vault.resolve_vault_path() / "technologies" / tech / "standards-brief.md").exists() else " "
        console.print(f"  {i}. {tech} [{has_brief}]")

    console.print(
        "\nEnter numbers separated by commas, or technology names "
        "(e.g. [cyan]1,3,4[/cyan] or [cyan]anvil,python,git[/cyan]):"
    )
    raw = typer.prompt("Tech stack")

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(available):
                selected.append(available[idx])
            else:
                error_console.print(f"[yellow]Skipping invalid index: {part}[/yellow]")
        elif part in available:
            selected.append(part)
        else:
            error_console.print(f"[yellow]Skipping unknown technology: {part}[/yellow]")

    return selected


def _render_agents_md(template_text: str, org: str, project: str, tech_stack: list[str]) -> str:
    """Substitute all $VARIABLES in the AGENTS.md template."""
    tech_stack_str = ", ".join(tech_stack) if tech_stack else "[no technologies selected]"

    tech_root_notes = "\n".join(
        f"   $MNEMOSYNE_VAULT/technologies/{t}/index.md"
        for t in tech_stack
    ) if tech_stack else "   [no technologies declared]"

    standards_brief_paths = "\n".join(
        f"$MNEMOSYNE_VAULT/technologies/{t}/standards-brief.md"
        for t in tech_stack
    ) if tech_stack else "[no standards briefs available]"

    result = template_text
    result = result.replace("$PROJECT", project)
    result = result.replace("$ORG", org)
    result = result.replace("$TECH_STACK", tech_stack_str)
    result = result.replace("$TECH_ROOT_NOTES", tech_root_notes)
    result = result.replace("$STANDARDS_BRIEF_PATHS", standards_brief_paths)
    return result


def run(
    org: str = typer.Argument(None, help="Organisation name (vault directory)"),
    project: str = typer.Argument(None, help="Project name"),
    tech: str = typer.Option(
        None,
        help="Comma-separated tech stack (e.g. anvil,python,git). Prompts interactively if omitted.",
    ),
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

    # Resolve tech stack
    available_techs = _discover_technologies(vault_path)
    if tech is not None:
        tech_stack = [t.strip() for t in tech.split(",") if t.strip()]
        unknown = [t for t in tech_stack if t not in available_techs]
        for t in unknown:
            error_console.print(
                f"[yellow]Warning: '{t}' not found in vault technologies[/yellow]"
            )
    else:
        tech_stack = _prompt_tech_stack(available_techs)

    # Create directory structure
    dirs = [
        project_dir / "gsd-planning",
        project_dir / "claude-config" / "rules",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]Created[/green] {d.relative_to(vault_path)}/")

    # Seed claude-config/skills.yaml so `mnemosyne init` populates .claude/skills/
    # without requiring a manual follow-up. Projects that need a different set
    # can edit this file after creation.
    skills_yaml = project_dir / "claude-config" / SKILLS_YAML_FILENAME
    skills_yaml.write_text(
        "# claude-config/skills.yaml — per-project skill allowlist (vault-side)\n"
        "# Each entry is a skill name at $MNEMOSYNE_VAULT/agents/skills/<name>/\n"
        "skills:\n"
        + "".join(f"  - {name}\n" for name in DEFAULT_SKILLS),
        encoding="utf-8",
    )
    console.print(f"[green]Created[/green] {skills_yaml.relative_to(vault_path)}")

    # Create AGENTS.md from template
    agents_md = project_dir / "AGENTS.md"
    template_path = vault_path / "templates" / "AGENTS.md"
    if template_path.exists():
        template_text = template_path.read_text()
    else:
        error_console.print(
            f"[yellow]Warning:[/yellow] Template not found at {template_path}, using minimal fallback"
        )
        template_text = "# $PROJECT — Agent Instructions\n"
    agents_md.write_text(_render_agents_md(template_text, org, project, tech_stack))
    console.print(f"[green]Created[/green] {agents_md.relative_to(vault_path)}")

    # Report standards brief coverage
    if tech_stack:
        missing_briefs = [
            t for t in tech_stack
            if not (vault_path / "technologies" / t / "standards-brief.md").exists()
        ]
        if missing_briefs:
            console.print(
                f"[yellow]Missing standards-brief.md for: {', '.join(missing_briefs)}[/yellow]"
            )
            console.print("Sub-agents won't have standards context for these technologies.")

    # Create project note with frontmatter
    project_note = project_dir / f"{project}.md"
    project_note.write_text(
        PROJECT_NOTE_TEMPLATE.format(org=org, project=project, today=today)
    )
    console.print(f"[green]Created[/green] {project_note.relative_to(vault_path)}")

    console.print()
    if tech_stack:
        console.print(f"[bold]Tech stack:[/bold] {', '.join(tech_stack)}")
    console.print(
        f"[bold green]Done.[/bold green] "
        f"Vault project [cyan]projects/{org}/{project}[/cyan] is ready."
    )
    console.print(
        "\nNext step: Run [bold]mnemosyne init[/bold] from your client codebase to wire it up."
    )
