"""mnemosyne init — wire a client codebase (host) or container workspace to the vault."""

from __future__ import annotations

import os
import subprocess
import sys
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
from mnemosyne_cli.lib import vault_worktree as lib_worktree
from mnemosyne_cli.lib.checks import run_container_checks
from mnemosyne_cli.lib.setup import setup_claude_overlay, setup_worktree_symlinks
from mnemosyne_cli.lib.skills import sync_user_skills

console = Console()
error_console = Console(stderr=True, style="bold red")


def run(
    project: str = typer.Argument(
        None,
        help="Vault project path (e.g. projects/org/project). "
        "In --container mode defaults to $MNEMOSYNE_PROJECT.",
    ),
    container: bool = typer.Option(
        False,
        "--container",
        help="Container mode: non-interactive, idempotent, skips .envrc. "
        "Reads --target / $MNEMOSYNE_WORKSPACE as the symlink target.",
    ),
    target: Path = typer.Option(
        None,
        "--target",
        help="Target directory to wire (container mode). "
        "Defaults to $MNEMOSYNE_WORKSPACE. Ignored in host mode.",
    ),
) -> None:
    """Wire a client codebase (host) or container workspace to the Mnemosyne vault."""
    # When called directly (not via typer's CLI plumbing), default values are
    # still the typer.OptionInfo / typer.ArgumentInfo sentinels rather than
    # the user-facing defaults. Normalise here so tests and programmatic
    # callers see plain Python values.
    if isinstance(project, (typer.models.OptionInfo, typer.models.ArgumentInfo)):
        project = None
    if isinstance(container, (typer.models.OptionInfo, typer.models.ArgumentInfo)):
        container = False
    if isinstance(target, (typer.models.OptionInfo, typer.models.ArgumentInfo)):
        target = None

    if container:
        _run_container(project=project, target=target)
    else:
        _run_host(project=project)


def _sync_skill_surface(vault_path: Path) -> None:
    """Reconcile ~/.claude/skills/ against the vault (D-09, D-10).

    Every vault skill becomes ~/.claude/skills/<name>/ so a Claude session can
    invoke it regardless of the project-level skills.yaml. This is the
    agent-wide surface; the project-scoped surface created by
    setup_worktree_symlinks (skills.yaml-driven) is untouched.

    Runs in both host and container mode — a host checkout drifts as skills are
    added to the vault or moved between its skill roots, and until now nothing
    reconciled it.
    """
    done = sync_user_skills(vault_path)
    linked, repointed, removed = done["linked"], done["repointed"], done["removed"]
    if linked or repointed or removed:
        console.print(
            f"  [green]Synced[/green] ~/.claude/skills/ "
            f"({len(linked)} linked, {len(repointed)} repointed, {len(removed)} removed)"
        )
    else:
        console.print("  [green]Checked[/green] ~/.claude/skills/ — already in sync")
    for skipped in done["skipped"]:
        error_console.print(f"  [yellow]Warning[/yellow] vault skill {skipped}")


# ---------------------------------------------------------------------------
# Container mode (D-08): non-interactive, idempotent, no .envrc
# ---------------------------------------------------------------------------


def _run_container(project: str | None, target: Path | None) -> None:
    """Container-mode init. Non-interactive, idempotent, skips .envrc (D-08)."""
    # 1. Resolve target
    if target is None:
        workspace = os.environ.get("MNEMOSYNE_WORKSPACE")
        if not workspace:
            error_console.print(
                "no vault project configured — skipping container bootstrap "
                "(no --target and MNEMOSYNE_WORKSPACE unset)"
            )
            raise typer.Exit(0)
        target = Path(workspace)
    target = target.resolve()
    if not target.exists() or not target.is_dir():
        error_console.print(
            f"no vault project configured — skipping container bootstrap "
            f"(target {target} does not exist)"
        )
        raise typer.Exit(0)

    # 2. Resolve project
    if project is None:
        project = os.environ.get("MNEMOSYNE_PROJECT")
    if not project:
        error_console.print(
            "no vault project configured — skipping container bootstrap "
            "(no --project and MNEMOSYNE_PROJECT unset)"
        )
        raise typer.Exit(0)
    project = project.strip().strip("/")

    # 3. Resolve vault
    try:
        vault_path = lib_vault.resolve_vault_path()
    except typer.Exit:
        error_console.print(
            "no vault project configured — skipping container bootstrap "
            "(MNEMOSYNE_VAULT unresolvable)"
        )
        raise typer.Exit(0)

    vault_project_path = vault_path / project
    if not vault_project_path.is_dir():
        error_console.print(
            f"no vault project configured — skipping container bootstrap "
            f"(project not in vault: {vault_project_path})"
        )
        raise typer.Exit(0)

    # 4. Resolve git dir for exclusions / hooks
    try:
        git_dir = lib_git.get_git_dir(target)
    except Exception:
        error_console.print(
            f"no vault project configured — skipping container bootstrap "
            f"(target {target} is not inside a git repository)"
        )
        raise typer.Exit(0)

    console.rule("[bold cyan]Wiring container workspace to vault[/bold cyan]")
    console.print(f"  target:  {target}")
    console.print(f"  vault:   {vault_path}")
    console.print(f"  project: {project}")

    # --- Vault agent worktree (branch isolation) ---
    # Agents must never run git against the vault main checkout — it is the
    # host operator's live working tree, bind-mounted into every container.
    # Project content (.planning, AGENTS.md, settings) is wired through a
    # project-scoped vault worktree instead; falls back to the main checkout
    # (pre-worktree behaviour) when the vault is not a git repo or worktree
    # creation fails — bootstrap must not die on this.
    project_source = vault_project_path
    try:
        wt = lib_worktree.ensure_vault_worktree(vault_path, project)
        wt_project = wt / project
        if wt_project.is_dir():
            project_source = wt_project
            branch = lib_worktree.worktree_branch(lib_worktree.project_slug(project))
            console.print(
                f"  [green]Vault worktree[/green] {wt} (branch {branch})"
            )
        else:
            error_console.print(
                f"  [yellow]Warning[/yellow] project missing from vault worktree "
                f"({wt_project}) — wiring to main checkout. Merge the worktree "
                f"branch and remove the worktree to refresh it."
            )
    except Exception as exc:
        error_console.print(
            f"  [yellow]Warning[/yellow] vault worktree unavailable ({exc}) — "
            "wiring to main checkout"
        )

    # --- Symlinks (shared path with host init) ---
    try:
        setup_worktree_symlinks(target, vault_path, project_source)
        console.print("  [green]Wired[/green] .planning, AGENTS.md, CLAUDE.md, .claude/*")
    except Exception as exc:
        error_console.print(f"  [red]Error[/red] symlink setup: {exc}")
        # Non-fatal: keep going so we still register merge drivers / hooks if possible

    # --- Vault-wide skill surface (D-09, D-10) ---
    _sync_skill_surface(vault_path)

    # --- Git exclusions (no .envrc per D-08) ---
    always_exclude = [".planning", "AGENTS.md", "CLAUDE.md"]
    optional_excludes = [".claude/rules", ".claude/skills", ".claude/settings.json"]
    for entry in always_exclude + optional_excludes:
        try:
            lib_git.add_git_exclusion(entry, git_dir)
        except Exception as exc:
            error_console.print(f"  [yellow]Warning[/yellow] git exclusion {entry}: {exc}")

    # --- Merge drivers ---
    try:
        lib_git.register_merge_drivers(vault_path)
        console.print("  [green]Registered[/green] gsd-state + gsd-roadmap merge drivers")
    except Exception as exc:
        error_console.print(f"  [yellow]Warning[/yellow] merge driver registration: {exc}")

    # --- Git hooks ---
    hook_script = "#!/bin/sh\nmnemosyne hook post-change\n"
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook_name in ("post-commit", "post-merge"):
        hook_path = hooks_dir / hook_name
        try:
            hook_path.write_text(hook_script)
            hook_path.chmod(0o755)
        except Exception as exc:
            error_console.print(f"  [yellow]Warning[/yellow] .git/hooks/{hook_name}: {exc}")

    console.print("[green]Container bootstrap complete.[/green]")

    # --- Self-verify summary (D-23) ---
    # Run the same checks `mnemosyne doctor --container` runs (lib/checks.py).
    # Failures are non-fatal — D-06 carry-forward: bootstrap failure must not
    # kill the agent. Output goes to stderr so it is visible in container logs
    # without polluting stdout for any scripted callers.
    error_console.print()
    error_console.print("[bold]Self-verify[/bold] (mnemosyne doctor --container subset):")
    results = run_container_checks(target, vault_path)
    passed = 0
    failed = 0
    for result in results:
        if result.ok:
            error_console.print(f"  PASS: {result.message}")
            passed += 1
        else:
            error_console.print(f"  FAIL: {result.message}")
            failed += 1
    error_console.print(f"[bold]{passed}/{passed + failed} checks passed[/bold]")


# ---------------------------------------------------------------------------
# Host mode: interactive prompt allowed, writes .envrc, partial-setup guard
# ---------------------------------------------------------------------------


def _run_host(project: str | None) -> None:
    """Host-mode init. Interactive prompt allowed, writes .envrc, partial-setup guard."""
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

    errors: list[str] = []

    # --- Operational home branch (D-D2) ---
    # Read the operational_home field from the empiria-side engagement record.
    # If present-but-malformed, read_operational_home raises ValueError (Open Q2 lock).
    try:
        oh = lib_vault.read_operational_home(vault_path, project)
    except ValueError as exc:
        error_console.print(
            f"[red]Error[/red] operational_home in vault record is malformed: {exc}\n"
            "Fix the frontmatter in the engagement record and re-run."
        )
        raise typer.Exit(1)

    if oh is not None:
        # --- D-D2: operational_home-set branch ---

        # Step 1: Resolve the OH vault by name
        vc = lib_vault.vault_by_name(oh.vault)
        if vc is None:
            error_console.print(
                f"[red]Error[/red] operational_home.vault '{oh.vault}' is not registered "
                f"in config.toml.\n"
                f"Register it with: [bold]mnemosyne vault add {oh.vault} <path>[/bold]"
            )
            raise typer.Exit(1)

        # Step 2: Compute the wire-codebase.py path and enforce containment (V5/V12)
        # Resolve once so the same path is used for both the is_within guard and
        # subprocess execution — eliminates the TOCTOU gap (WR-03).
        oh_root = vc.path.resolve()
        wire = (vc.path / oh.path / "wire-codebase.py").resolve()
        if not lib_vault.is_within(oh_root, wire):
            error_console.print(
                f"[red]Error[/red] operational_home.path '{oh.path}' escapes the OH vault "
                f"root ({oh_root}). Path traversal rejected."
            )
            raise typer.Exit(1)

        # Step 3: Verify the script exists
        if not wire.is_file():
            error_console.print(
                f"[red]Error[/red] wire-codebase.py not found at expected path:\n"
                f"  {wire}\n"
                "Create the script in the operational-home vault project directory."
            )
            raise typer.Exit(1)

        # Step 4: Run the in-vault wire-codebase.py to create .planning, AGENTS.md, CLAUDE.md
        console.rule("[bold cyan]Wiring universal symlinks (operational-home vault)[/bold cyan]")
        try:
            result = subprocess.run(
                [sys.executable, str(wire), str(cwd)],
                check=True,
                capture_output=True,
                text=True,
            )
            console.print(
                f"  [green]Wired[/green] .planning, AGENTS.md, CLAUDE.md "
                f"(via {wire.name})"
            )
        except subprocess.CalledProcessError as exc:
            error_console.print(
                f"[red]Error[/red] wire-codebase.py failed (exit {exc.returncode}):\n"
                f"{exc.stderr}"
            )
            raise typer.Exit(1)

        # Step 5: Layer the Empiria-only .claude/* overlay against the empiria vault
        console.rule("[bold cyan]Layering Empiria overlay[/bold cyan]")
        try:
            setup_claude_overlay(cwd, vault_path, vault_project_path)
            console.print("  [green]Wired[/green] .claude/* (settings, rules, skills)")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] overlay setup: {exc}")
            errors.append("overlay")

        # Step 6: Split exclusions (D-C5)
        # .planning and AGENTS.md → tracked .gitignore (public convention)
        # .claude/* → .git/info/exclude (per-clone, never tracked)
        console.rule("[bold cyan]Configuring git exclusions (D-C5 split)[/bold cyan]")
        for entry in (".planning", "AGENTS.md"):
            try:
                lib_git.add_gitignore_entry(entry, cwd)
                console.print(f"  [green]Configured[/green] .gitignore: {entry}")
            except Exception as exc:
                error_console.print(f"  [red]Error[/red] .gitignore entry for {entry}: {exc}")
                errors.append(f"gitignore: {entry}")
        for entry in (".claude/rules", ".claude/skills", ".claude/settings.json"):
            try:
                lib_git.add_git_exclusion(entry, git_dir)
                console.print(f"  [green]Configured[/green] .git/info/exclude: {entry}")
            except Exception as exc:
                error_console.print(f"  [red]Error[/red] git exclusion for {entry}: {exc}")
                errors.append(f"git exclude: {entry}")

    else:
        # --- D-D1: operational_home unset — today's path, byte-identical ---

        # --- Symlinks ---
        console.rule("[bold cyan]Creating symlinks[/bold cyan]")
        try:
            setup_worktree_symlinks(cwd, vault_path, vault_project_path)
            console.print("  [green]Wired[/green] .planning, AGENTS.md, CLAUDE.md, .claude/*")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] symlink setup: {exc}")
            errors.append("symlinks")

        # CLAUDE.md upstream-tracked override (only matters when CLAUDE.md is
        # tracked in the client's upstream repo — apply the sparse-checkout +
        # assume-unchanged pattern so the local symlink doesn't show as a
        # typechange and can't be staged accidentally). setup_worktree_symlinks
        # has already created the symlink with force=True; this just hardens it.
        if lib_overrides.is_tracked(cwd, "CLAUDE.md"):
            try:
                lib_overrides.apply_claude_md_override(cwd, git_dir)
                console.print(
                    "  [green]Hardened[/green] CLAUDE.md override "
                    "(sparse-checkout + assume-unchanged for upstream-tracked CLAUDE.md)"
                )
            except Exception as exc:
                error_console.print(f"  [red]Error[/red] CLAUDE.md override: {exc}")
                errors.append("CLAUDE.md override")

        # --- Git exclusions ---
        console.rule("[bold cyan]Configuring git exclusions[/bold cyan]")
        all_to_exclude = [
            ".planning",
            "AGENTS.md",
            "CLAUDE.md",
            ".envrc",
            ".claude/rules",
            ".claude/skills",
            ".claude/settings.json",
        ]
        for entry in all_to_exclude:
            try:
                lib_git.add_git_exclusion(entry, git_dir)
                console.print(f"  [green]Configured[/green] .git/info/exclude: {entry}")
            except Exception as exc:
                error_console.print(f"  [red]Error[/red] git exclusion for {entry}: {exc}")
                errors.append(f"git exclude: {entry}")

    # --- Vault-wide skill surface (D-09, D-10) ---
    console.rule("[bold cyan]Syncing vault skills[/bold cyan]")
    try:
        _sync_skill_surface(vault_path)
    except Exception as exc:
        error_console.print(f"  [red]Error[/red] skill surface: {exc}")
        errors.append("skills")

    # --- .envrc ---
    console.rule("[bold cyan]Setting up environment[/bold cyan]")
    try:
        changed = lib_envrc.set_envrc_vault(cwd, vault_path)
        if changed:
            console.print(f"  [green]Created[/green] .envrc with MNEMOSYNE_VAULT={vault_path}")
        else:
            console.print("  [green]Configured[/green] .envrc already has correct MNEMOSYNE_VAULT")
    except Exception as exc:
        error_console.print(f"  [red]Error[/red] .envrc: {exc}")
        errors.append(".envrc")
    console.print("  Run [bold]direnv allow[/bold] to activate the environment.")

    # --- Merge drivers ---
    console.rule("[bold cyan]Registering merge drivers[/bold cyan]")
    try:
        lib_git.register_merge_drivers(vault_path)
        console.print("  [green]Configured[/green] gsd-state merge driver")
        console.print("  [green]Configured[/green] gsd-roadmap merge driver")
    except Exception as exc:
        error_console.print(f"  [red]Error[/red] merge driver registration: {exc}")
        errors.append("merge drivers")

    # --- Git hooks ---
    console.rule("[bold cyan]Installing git hooks[/bold cyan]")
    hook_script = "#!/bin/sh\nmnemosyne hook post-change\n"
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook_name in ("post-commit", "post-merge"):
        hook_path = hooks_dir / hook_name
        try:
            hook_path.write_text(hook_script)
            hook_path.chmod(0o755)
            console.print(f"  [green]Installed[/green] .git/hooks/{hook_name}")
        except Exception as exc:
            error_console.print(f"  [red]Error[/red] .git/hooks/{hook_name}: {exc}")
            errors.append(f".git/hooks/{hook_name}")

    # --- Summary panel ---
    console.print()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Vault", str(vault_path))
    table.add_row("Project", project)
    if oh is not None:
        table.add_row("Mode", f"operational-home ({oh.vault})")
    if errors:
        table.add_row("[red]Errors[/red]", f"{len(errors)} step(s) failed")
    console.print(Panel(table, title="[bold]Setup Summary[/bold]", border_style="cyan"))

    if errors:
        console.print(
            "[yellow]Setup completed with errors. "
            "Run [bold]mnemosyne doctor[/bold] to check remaining issues.[/yellow]"
        )
    else:
        console.print(
            "[green]Setup complete.[/green] Run [bold]direnv allow[/bold] to activate the environment."
        )


# lib_symlinks is imported for backward-compat with monkeypatches in test_init.py
# (e.g. mnemosyne_cli.commands.init.lib_symlinks.*). Keep the binding.
_ = lib_symlinks
