"""mnemosyne work — manage worktree-based work sessions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib import git as lib_git
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

app = typer.Typer(no_args_is_help=True, help="Manage worktree-based work sessions.")
console = Console()
error_console = Console(stderr=True, style="bold red")


def _repo_root() -> Path:
    """Return the current git repository root (the main worktree)."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or "Not inside a git repository."
        error_console.print(msg)
        raise typer.Exit(1)
    return Path(result.stdout.strip())


def _current_branch(repo_root: Path) -> str | None:
    """Return the branch checked out in the main worktree, or None if detached."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def _replicate_assume_unchanged(main_root: Path, worktree_path: Path) -> None:
    """Copy assume-unchanged flags from the main checkout to a worktree.

    Worktrees don't inherit assume-unchanged flags, so symlinks replacing
    tracked files (e.g. CLAUDE.md) show as typechanges without this.
    """
    result = subprocess.run(
        ["git", "ls-files", "-v"],
        cwd=main_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    # Lines starting with lowercase letter have assume-unchanged set
    unchanged = [
        line[2:] for line in result.stdout.splitlines()
        if line and line[0].islower()
    ]
    if unchanged:
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", *unchanged],
            cwd=worktree_path,
            capture_output=True,
        )


def _setup_worktree_symlinks(
    worktree_path: Path,
    vault_path: Path,
    vault_project_path: Path,
    repo_root: Path,
) -> None:
    """Replicate vault symlinks from the main checkout into a worktree.

    Creates the same symlink structure that ``mnemosyne doctor --fix`` would
    create for the main checkout.  Uses ``force=True`` because git checks out
    tracked files (e.g. CLAUDE.md) as regular files that need to be replaced
    by symlinks.
    """
    # .planning → main planning dir (GSD scopes by phase number)
    planning_dir = vault_project_path / "gsd-planning"
    lib_symlinks.create_symlink(worktree_path / ".planning", planning_dir, force=True)
    console.print("  .planning -> gsd-planning/")

    # AGENTS.md → vault project AGENTS.md
    agents_target = vault_project_path / "AGENTS.md"
    if agents_target.exists():
        lib_symlinks.create_symlink(
            worktree_path / "AGENTS.md", agents_target, force=True
        )
        # CLAUDE.md → AGENTS.md (relative symlink)
        lib_symlinks.create_symlink(
            worktree_path / "CLAUDE.md", Path("AGENTS.md"), force=True
        )
        console.print("  CLAUDE.md -> AGENTS.md -> vault")

    # Replicate assume-unchanged flags from the main checkout so symlinks
    # replacing tracked files don't show as dirty in the worktree.
    _replicate_assume_unchanged(repo_root, worktree_path)

    # .claude/settings.json
    claude_config = vault_project_path / "claude-config"
    settings_target = claude_config / "settings.json"
    if settings_target.exists():
        lib_symlinks.create_symlink(
            worktree_path / ".claude" / "settings.json", settings_target, force=True
        )

    # .claude/rules — per-file symlinks from embed notes
    rules_embed_dir = claude_config / "rules"
    if rules_embed_dir.is_dir():
        for filename, target_rel in read_embed_targets(rules_embed_dir).items():
            lib_symlinks.create_symlink(
                worktree_path / ".claude" / "rules" / filename,
                vault_path / target_rel,
                force=True,
            )

    # .claude/rules — tech stack auto-rules
    if agents_target.exists():
        for tech in parse_tech_stack(agents_target):
            for filename, target_abs in discover_tech_rules(vault_path, tech).items():
                lib_symlinks.create_symlink(
                    worktree_path / ".claude" / "rules" / filename,
                    target_abs,
                    force=True,
                )

    # .claude/skills — directory symlinks from skills.yaml
    skills_yaml = claude_config / SKILLS_YAML_FILENAME
    if skills_yaml.exists():
        try:
            raw_names = parse_skills_list(skills_yaml)
            skill_names = expand_skill_names(raw_names, vault_path)
        except ValueError as exc:
            error_console.print(f"  [yellow]Warning[/yellow] skills.yaml error: {exc} (skipping skills)")
            skill_names = []
        for name in skill_names:
            try:
                create_skill_symlink(worktree_path, name, vault_path)
                console.print(f"  .claude/skills/{name}/ -> agents/skills/{name}/")
            except Exception as exc:
                error_console.print(f"  [yellow]Warning[/yellow] .claude/skills/{name}: {exc}")


def _ensure_worktree(branch: str) -> Path:
    """Ensure a worktree exists for the given branch. Returns the worktree path."""
    vault_path = lib_vault.resolve_vault_path()
    repo_root = _repo_root()

    # If the requested branch is already checked out in the main worktree, use it directly
    if _current_branch(repo_root) == branch:
        console.print(f"Branch {branch} is checked out at {repo_root}")
        return repo_root

    vault_project = lib_vault.resolve_vault_project(repo_root, vault_path)
    worktree_path = repo_root / "worktrees" / branch

    if worktree_path.exists():
        console.print(f"Worktree worktrees/{branch}/ already exists")
    else:
        try:
            lib_git.worktree_add(repo_root, worktree_path, branch, new_branch=True)
        except subprocess.CalledProcessError:
            try:
                lib_git.worktree_add(repo_root, worktree_path, branch, new_branch=False)
            except subprocess.CalledProcessError as exc2:
                error_console.print(f"Failed to create worktree: {exc2}")
                raise typer.Exit(1) from exc2

        if vault_project is not None:
            vault_project_path = vault_path / vault_project
            _setup_worktree_symlinks(
                worktree_path, vault_path, vault_project_path, repo_root
            )

        console.print(f"Created worktree: worktrees/{branch}/")

    return worktree_path


@app.command("setup")
def setup(
    branch: str = typer.Argument(..., help="Branch name for the worktree."),
) -> None:
    """Ensure a worktree exists for a branch (no Zellij session)."""
    worktree_path = _ensure_worktree(branch)
    # Machine-readable line for callers (e.g. agent.py _do_attach)
    print(f"WORKTREE_PATH={worktree_path}")


@app.command("start")
def start(
    branch: str = typer.Argument(..., help="Branch name for the work session."),
) -> None:
    """Start a worktree-based work session for a branch."""
    worktree_path = _ensure_worktree(branch)

    if os.environ.get("MNEMOSYNE_CONTAINER"):
        os.chdir(worktree_path)
        zellij_sessions = subprocess.run(
            ["zellij", "list-sessions", "--no-formatting", "--short"],
            capture_output=True, text=True,
        )
        if branch in zellij_sessions.stdout.splitlines():
            os.execvp("zellij", ["zellij", "attach", branch])
        else:
            os.execvp("zellij", ["zellij", "-s", branch])
    else:
        console.print(f"cd {worktree_path} && claude")


@app.command("finish")
def finish(
    branch: str = typer.Argument(..., help="Branch name of the work session to finish."),
    force: bool = typer.Option(False, "--force", "-f", help="Force removal even with uncommitted changes."),
) -> None:
    """Finish a work session and remove the worktree."""
    repo_root = _repo_root()

    worktree_path = repo_root / "worktrees" / branch

    if not worktree_path.exists():
        error_console.print(f"Worktree worktrees/{branch}/ does not exist.")
        raise typer.Exit(1)

    try:
        lib_git.worktree_remove(repo_root, worktree_path, force=force)
    except subprocess.CalledProcessError as exc:
        error_console.print(f"Failed to remove worktree: {exc}")
        error_console.print("Use --force to remove despite uncommitted changes.")
        raise typer.Exit(1) from exc

    console.print(f"Finished: {branch}")


@app.command("list")
def list_worktrees() -> None:
    """List active worktrees for the current repo."""
    repo_root = _repo_root()

    try:
        all_worktrees = lib_git.list_worktrees(repo_root)
    except subprocess.CalledProcessError as exc:
        error_console.print(f"Failed to list worktrees: {exc}")
        raise typer.Exit(1) from exc

    # Filter to only worktrees under worktrees/ subdir (skip main worktree)
    worktrees_dir = repo_root / "worktrees"
    work_worktrees = [
        wt for wt in all_worktrees
        if Path(wt["worktree"]).is_relative_to(worktrees_dir)
    ]

    if work_worktrees:
        table = Table(title="Active Worktrees", show_lines=False)
        table.add_column("Branch", style="cyan", no_wrap=True)
        table.add_column("Path", style="dim")

        for wt in work_worktrees:
            branch = wt.get("branch", "(detached)")
            path = wt["worktree"]
            table.add_row(branch, path)

        console.print(table)

    # Also show vault worktrees
    vault_wts: list[dict[str, str]] = []
    try:
        vault_path = lib_vault.resolve_vault_path()
        vault_all = lib_git.list_worktrees(vault_path)
        vault_wt_dir = vault_path / "worktrees"
        vault_wts = [
            wt for wt in vault_all
            if Path(wt["worktree"]).is_relative_to(vault_wt_dir)
        ]
        if vault_wts:
            console.print()
            vt = Table(title="Vault Worktrees", show_lines=False)
            vt.add_column("Branch", style="cyan", no_wrap=True)
            vt.add_column("Path", style="dim")
            for wt in vault_wts:
                vt.add_row(wt.get("branch", "(detached)"), wt["worktree"])
            console.print(vt)
    except (subprocess.CalledProcessError, SystemExit):
        pass  # Vault not available or not a git repo — skip

    if not work_worktrees and not vault_wts:
        console.print("[dim]No active worktrees[/dim]")
