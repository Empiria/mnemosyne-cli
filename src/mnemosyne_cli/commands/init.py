"""mnemosyne init — wire a client codebase to the vault (one-shot setup)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mnemosyne_cli.lib import envrc as lib_envrc
from mnemosyne_cli.lib import git as lib_git
from mnemosyne_cli.lib import overrides as lib_overrides
from mnemosyne_cli.lib import symlinks as lib_symlinks
from mnemosyne_cli.lib import vault as lib_vault
from mnemosyne_cli.lib.embeds import read_embed_targets
from mnemosyne_cli.lib.symlinks import (
    SKILLS_YAML_FILENAME,
    create_skill_symlink,
    expand_skill_names,
    parse_skills_list,
)
from mnemosyne_cli.lib.techstack import discover_tech_rules, parse_tech_stack

console = Console()
error_console = Console(stderr=True, style="bold red")


def run(
    project: str = typer.Argument(None, help="Vault project path (e.g. projects/org/project)"),
) -> None:
    """Wire a client codebase to the Mnemosyne vault."""
    cwd = Path.cwd()

    # 1. Resolve vault path
    vault_path = lib_vault.resolve_vault_path()

    # 2. Check we're in a git repo
    try:
        git_dir = lib_git.get_git_dir(cwd)
    except Exception:
        error_console.print("Not inside a git repository. Run from the root of a client codebase.")
        raise typer.Exit(1)

    # 3. Prompt for project if not provided
    if project is None:
        project = typer.prompt("Vault project path (e.g. projects/org/project)")

    project = project.strip().strip("/")

    # 4. Check vault project exists
    vault_project_path = vault_path / project
    if not vault_project_path.is_dir():
        org_parts = project.split("/")
        org = org_parts[1] if len(org_parts) >= 2 else "<org>"
        proj = org_parts[2] if len(org_parts) >= 3 else "<project>"
        error_console.print(
            f"Project not found in vault: {vault_project_path}\n"
            f"Run [bold]mnemosyne add {org} {proj}[/bold] first."
        )
        raise typer.Exit(1)

    # 5. Partial setup detection — stop if .planning already exists
    planning_link = cwd / ".planning"
    if planning_link.is_symlink() or planning_link.exists():
        error_console.print(
            "Partial setup detected (.planning already exists).\n"
            "Run [bold]mnemosyne doctor --fix[/bold] to repair."
        )
        raise typer.Exit(1)

    # Track what we created for summary and git exclusions
    created_symlinks: list[str] = []
    errors: list[str] = []

    # --- Section: Creating symlinks ---
    console.rule("[bold cyan]Creating symlinks[/bold cyan]")

    def _create(name: Path, target: Path, display: str) -> bool:
        """Create a symlink and report result. Returns True on success."""
        try:
            lib_symlinks.create_symlink(name, target)
            console.print(f"  [green]Created[/green] {display}")
            return True
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] {display}: {exc}")
            errors.append(display)
            return False

    # 1. .planning -> {vault}/{project}/gsd-planning
    target = vault_project_path / "gsd-planning"
    if _create(planning_link, target, f".planning -> {target}"):
        created_symlinks.append(".planning")

    # 2. AGENTS.md -> {vault}/{project}/AGENTS.md
    agents_link = cwd / "AGENTS.md"
    agents_target = vault_project_path / "AGENTS.md"
    if agents_target.exists() and _create(agents_link, agents_target, f"AGENTS.md -> {agents_target}"):
        created_symlinks.append("AGENTS.md")
    elif not agents_target.exists():
        error_console.print(f"  [yellow]Skipped[/yellow] AGENTS.md (not found in vault: {agents_target})")

    # 3. CLAUDE.md -> AGENTS.md (local, not absolute).
    # If upstream tracks CLAUDE.md (e.g. the client added one), apply the
    # full override pattern instead of a plain symlink — otherwise the
    # symlink would land as a staged typechange that any `git commit -a`
    # would push back to the client.
    claude_link = cwd / "CLAUDE.md"
    if lib_overrides.is_tracked(cwd, "CLAUDE.md"):
        try:
            lib_overrides.apply_claude_md_override(cwd, git_dir)
            console.print(
                "  [green]Created[/green] CLAUDE.md -> AGENTS.md "
                "(upstream-tracked: applied sparse-checkout + assume-unchanged)"
            )
            created_symlinks.append("CLAUDE.md")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] CLAUDE.md override: {exc}")
            errors.append("CLAUDE.md")
    else:
        try:
            lib_symlinks.create_symlink(claude_link, Path("AGENTS.md"))
            console.print("  [green]Created[/green] CLAUDE.md -> AGENTS.md")
            created_symlinks.append("CLAUDE.md")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] CLAUDE.md: {exc}")
            errors.append("CLAUDE.md")

    # 4-6. Optional .claude/ symlinks
    claude_config = vault_project_path / "claude-config"
    claude_dir = cwd / ".claude"

    # .claude/rules — read embed notes and create per-file symlinks
    rules_embed_dir = claude_config / "rules"
    if rules_embed_dir.is_dir():
        rules_targets = read_embed_targets(rules_embed_dir)
        if rules_targets:
            client_rules = claude_dir / "rules"
            client_rules.mkdir(parents=True, exist_ok=True)
            for filename, target_rel in rules_targets.items():
                target_abs = vault_path / target_rel
                symlink = client_rules / filename
                if _create(symlink, target_abs, f".claude/rules/{filename} -> {target_rel}"):
                    created_symlinks.append(f".claude/rules/{filename}")
        else:
            console.print("  [yellow]Skipped[/yellow] .claude/rules — no embed targets found")

    # .claude/skills — read skills.yaml and create per-skill directory symlinks
    skills_yaml = claude_config / SKILLS_YAML_FILENAME
    if skills_yaml.exists():
        try:
            raw_names = parse_skills_list(skills_yaml)
            skill_names = expand_skill_names(raw_names, vault_path)
        except ValueError as exc:
            error_console.print(f"  [red]Error[/red] skills.yaml: {exc}")
            errors.append("skills.yaml")
            skill_names = []
        for name in skill_names:
            try:
                create_skill_symlink(cwd, name, vault_path)
                console.print(f"  [green]Created[/green] .claude/skills/{name}/ -> agents/skills/{name}/")
                created_symlinks.append(f".claude/skills/{name}")
            except Exception as exc:
                error_console.print(f"  [red]Error[/red] .claude/skills/{name}: {exc}")
                errors.append(f".claude/skills/{name}")
    else:
        console.print("  [yellow]Skipped[/yellow] .claude/skills — no skills.yaml found in claude-config/")

    # Tech stack auto-rules — derive from AGENTS.md Tech stack: line
    if agents_target.exists():
        tech_stack = parse_tech_stack(agents_target)
        if tech_stack:
            client_rules = claude_dir / "rules"
            client_rules.mkdir(parents=True, exist_ok=True)
            for tech in tech_stack:
                tech_rules = discover_tech_rules(vault_path, tech)
                for filename, target_abs in tech_rules.items():
                    symlink = client_rules / filename
                    if symlink.exists() or symlink.is_symlink():
                        # Manual embed or prior run takes precedence
                        continue
                    if _create(symlink, target_abs, f".claude/rules/{filename} -> {target_abs.relative_to(vault_path)}"):
                        created_symlinks.append(f".claude/rules/{filename}")

    settings_src = claude_config / "settings.json"
    if settings_src.exists():
        settings_link = claude_dir / "settings.json"
        if _create(settings_link, settings_src, f".claude/settings.json -> {settings_src}"):
            created_symlinks.append(".claude/settings.json")

    # --- Section: Configuring git exclusions ---
    console.rule("[bold cyan]Configuring git exclusions[/bold cyan]")

    # Always exclude these core items plus .envrc
    always_exclude = [".planning", "AGENTS.md", "CLAUDE.md", ".envrc"]
    # Conditionally exclude optional .claude/ items
    # For per-file symlink dirs (.claude/rules, .claude/commands), exclude the directory
    # entry when any per-file symlink was created inside it.
    optional_excludes = []
    if any(s.startswith(".claude/rules/") for s in created_symlinks):
        optional_excludes.append(".claude/rules")
    if any(s.startswith(".claude/skills/") for s in created_symlinks):
        optional_excludes.append(".claude/skills")
    if ".claude/settings.json" in created_symlinks:
        optional_excludes.append(".claude/settings.json")
    all_to_exclude = always_exclude + optional_excludes

    for entry in all_to_exclude:
        try:
            lib_git.add_git_exclusion(entry, git_dir)
            console.print(f"  [green]Configured[/green] .git/info/exclude: {entry}")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] git exclusion for {entry}: {exc}")
            errors.append(f"git exclude: {entry}")

    # --- Section: Setting up environment ---
    console.rule("[bold cyan]Setting up environment[/bold cyan]")

    try:
        changed = lib_envrc.set_envrc_vault(cwd, vault_path)
        if changed:
            console.print(f"  [green]Created[/green] .envrc with MNEMOSYNE_VAULT={vault_path}")
        else:
            console.print(f"  [green]Configured[/green] .envrc already has correct MNEMOSYNE_VAULT")
    except Exception as exc:
        error_console.print(f"  [red]Error[/red] .envrc: {exc}")
        errors.append(".envrc")

    # --- Section: Registering merge drivers ---
    console.rule("[bold cyan]Registering merge drivers[/bold cyan]")

    try:
        lib_git.register_merge_drivers(vault_path)
        console.print("  [green]Configured[/green] gsd-state merge driver")
        console.print("  [green]Configured[/green] gsd-roadmap merge driver")
    except Exception as exc:
        error_console.print(f"  [red]Error[/red] merge driver registration: {exc}")
        errors.append("merge drivers")

    # --- Section: Installing git hooks ---
    console.rule("[bold cyan]Installing git hooks[/bold cyan]")

    _hook_script = "#!/bin/sh\nmnemosyne hook post-change\n"
    _hooks_dir = git_dir / "hooks"
    _hooks_dir.mkdir(parents=True, exist_ok=True)
    for _hook_name in ("post-commit", "post-merge"):
        _hook_path = _hooks_dir / _hook_name
        try:
            _hook_path.write_text(_hook_script)
            _hook_path.chmod(0o755)
            console.print(f"  [green]Installed[/green] .git/hooks/{_hook_name}")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] .git/hooks/{_hook_name}: {exc}")
            errors.append(f".git/hooks/{_hook_name}")

    # --- Summary panel ---
    console.print()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Vault", str(vault_path))
    table.add_row("Project", project)
    table.add_row("Symlinks", ", ".join(created_symlinks) if created_symlinks else "none")
    table.add_row("Git exclusions", f"{len(all_to_exclude)} entries added")
    if errors:
        table.add_row("[red]Errors[/red]", f"{len(errors)} step(s) failed")

    console.print(Panel(table, title="[bold]Setup Summary[/bold]", border_style="cyan"))

    if errors:
        console.print(
            "[yellow]Setup completed with errors. "
            "Run [bold]mnemosyne doctor[/bold] to check remaining issues.[/yellow]"
        )
    else:
        console.print("[green]Setup complete.[/green]")
