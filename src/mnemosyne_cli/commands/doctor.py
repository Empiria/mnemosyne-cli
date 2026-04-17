"""mnemosyne doctor — validate project setup with optional --fix repair."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console

from mnemosyne_cli.lib import envrc as lib_envrc
from mnemosyne_cli.lib import git as lib_git
from mnemosyne_cli.lib import symlinks as lib_symlinks
from mnemosyne_cli.lib import vault as lib_vault
from mnemosyne_cli.lib.embeds import read_embed_targets
from mnemosyne_cli.lib.symlinks import (
    CheckResult,
    SKILLS_YAML_FILENAME,
    parse_skills_list,
    expand_skill_names,
    create_skill_symlink,
    check_skill_symlink,
)
from mnemosyne_cli.lib.techstack import discover_tech_rules, parse_tech_stack

console = Console()
error_console = Console(stderr=True, style="bold red")


def _cli_repo_root() -> Path:
    """Return the CLI repo root (this file lives at src/mnemosyne_cli/commands/doctor.py)."""
    return Path(__file__).resolve().parent.parent.parent.parent


@dataclass
class Check:
    """A single doctor check with optional fix function."""

    name: str
    category: str
    _check_fn: Callable[[], CheckResult] = field(repr=False)
    _fix_fn: Callable[[], None] | None = field(default=None, repr=False)
    fix_description: str = ""

    def check(self) -> CheckResult:
        return self._check_fn()

    def has_fix(self) -> bool:
        return self._fix_fn is not None

    def apply_fix(self) -> None:
        if self._fix_fn is not None:
            self._fix_fn()


def _build_checks(cwd: Path, vault_path: Path, git_dir: Path) -> list[Check]:
    """Build the full list of checks for the current directory."""
    checks: list[Check] = []

    # --- Category: Environment ---

    def check_vault_configured() -> CheckResult:
        import os

        v = os.environ.get("MNEMOSYNE_VAULT", "")
        if v:
            p = Path(v).expanduser().resolve()
            if not p.is_dir():
                return CheckResult(
                    ok=False,
                    message=f"MNEMOSYNE_VAULT={v} is not a valid directory",
                    fix_cmd=None,
                )
            return CheckResult(ok=True, message=f"MNEMOSYNE_VAULT={v}")

        config_path = lib_vault._read_config_vault_path()
        if config_path:
            if not config_path.is_dir():
                return CheckResult(
                    ok=False,
                    message=f"vault_path in config.toml does not exist: {config_path}",
                    fix_cmd=None,
                )
            return CheckResult(ok=True, message=f"vault_path={config_path} (config.toml)")

        return CheckResult(
            ok=False,
            message="Vault path not configured",
            fix_cmd=f'mnemosyne config set vault_path "$HOME/projects/empiria/mnemosyne"',
        )

    checks.append(
        Check(
            name="Vault path configured",
            category="Environment",
            _check_fn=check_vault_configured,
        )
    )

    def check_git_repo() -> CheckResult:
        try:
            lib_git.get_git_dir(cwd)
            return CheckResult(ok=True, message="Inside a git repository")
        except Exception:
            return CheckResult(
                ok=False,
                message="Not inside a git repository",
                fix_cmd=None,
            )

    checks.append(
        Check(
            name="Inside a git repository",
            category="Environment",
            _check_fn=check_git_repo,
        )
    )

    # --- Detect if we're inside the vault itself ---
    is_vault = cwd.resolve() == vault_path.resolve()

    # --- Derive vault project from .planning symlink ---
    vault_project = lib_vault.resolve_vault_project(cwd, vault_path) if not is_vault else None
    vault_project_path = vault_path / vault_project if vault_project else None

    # --- Client-codebase-only checks (skipped when running from the vault) ---

    if not is_vault:

        def _symlink_check(link_name: str, target: Path) -> Callable[[], CheckResult]:
            def _check() -> CheckResult:
                return lib_symlinks.check_symlink(cwd / link_name, target)

            return _check

        def _symlink_fix(link_name: str, target: Path) -> Callable[[], None]:
            def _fix() -> None:
                lib_symlinks.create_symlink(cwd / link_name, target)

            return _fix

        def check_claude_md_local() -> CheckResult:
            """CLAUDE.md should be a local symlink to AGENTS.md (not absolute)."""
            claude = cwd / "CLAUDE.md"
            if not claude.exists() and not claude.is_symlink():
                return CheckResult(
                    ok=False,
                    message="CLAUDE.md — missing",
                    fix_cmd="ln -sfn AGENTS.md CLAUDE.md",
                )
            if not claude.is_symlink():
                return CheckResult(
                    ok=False,
                    message="CLAUDE.md — exists but is not a symlink",
                    fix_cmd=None,
                )
            link_target = claude.readlink()
            if str(link_target) != "AGENTS.md":
                return CheckResult(
                    ok=False,
                    message=f"CLAUDE.md — points to {link_target}, expected AGENTS.md",
                    fix_cmd="ln -sfn AGENTS.md CLAUDE.md",
                )
            return CheckResult(ok=True, message="CLAUDE.md -> AGENTS.md")

        def fix_claude_md_local() -> None:
            lib_symlinks.create_symlink(cwd / "CLAUDE.md", Path("AGENTS.md"))

        if vault_project_path is not None:
            # .planning
            planning_target = vault_project_path / "gsd-planning"
            checks.append(
                Check(
                    name=".planning symlink",
                    category="Symlinks",
                    _check_fn=_symlink_check(".planning", planning_target),
                    _fix_fn=_symlink_fix(".planning", planning_target),
                    fix_description=f"Create .planning -> {planning_target}",
                )
            )

            # AGENTS.md
            agents_target = vault_project_path / "AGENTS.md"
            checks.append(
                Check(
                    name="AGENTS.md symlink",
                    category="Symlinks",
                    _check_fn=_symlink_check("AGENTS.md", agents_target),
                    _fix_fn=_symlink_fix("AGENTS.md", agents_target),
                    fix_description=f"Create AGENTS.md -> {agents_target}",
                )
            )

            # CLAUDE.md (local symlink)
            checks.append(
                Check(
                    name="CLAUDE.md local symlink",
                    category="Symlinks",
                    _check_fn=check_claude_md_local,
                    _fix_fn=fix_claude_md_local,
                    fix_description="Create CLAUDE.md -> AGENTS.md",
                )
            )

            # mnemosyne_scripts should not exist — replaced by CLI subcommands
            def check_no_mnemosyne_scripts() -> CheckResult:
                scripts_link = cwd / "mnemosyne_scripts"
                if scripts_link.is_symlink() or scripts_link.exists():
                    return CheckResult(
                        ok=False,
                        message="mnemosyne_scripts should not exist (scripts are now CLI subcommands)",
                        fix_cmd="rm mnemosyne_scripts",
                    )
                return CheckResult(ok=True, message="mnemosyne_scripts absent")

            def fix_no_mnemosyne_scripts() -> None:
                scripts_link = cwd / "mnemosyne_scripts"
                if scripts_link.is_symlink():
                    scripts_link.unlink()
                elif scripts_link.is_dir():
                    scripts_link.rmdir()  # only removes if empty — safe

            checks.append(
                Check(
                    name="mnemosyne_scripts absent",
                    category="Symlinks",
                    _check_fn=check_no_mnemosyne_scripts,
                    _fix_fn=fix_no_mnemosyne_scripts,
                    fix_description="Remove mnemosyne_scripts",
                )
            )

            # Optional .claude/ per-file symlinks — derived from embed notes
            claude_config = vault_project_path / "claude-config"

            def _perfile_symlink_check(link_path: str, target: Path) -> Callable[[], CheckResult]:
                def _check() -> CheckResult:
                    return lib_symlinks.check_symlink(cwd / link_path, target)
                return _check

            def _perfile_symlink_fix(link_path: str, target: Path) -> Callable[[], None]:
                def _fix() -> None:
                    full_link = cwd / link_path
                    full_link.parent.mkdir(parents=True, exist_ok=True)
                    lib_symlinks.create_symlink(full_link, target)
                return _fix

            # .claude/rules — check per-file symlinks derived from embed notes
            rules_embed_dir = claude_config / "rules"
            if rules_embed_dir.is_dir():
                rules_targets = read_embed_targets(rules_embed_dir)
                client_rules = cwd / ".claude" / "rules"

                # Detect stale directory symlink from pre-Phase-10 setup
                if client_rules.is_symlink() and rules_targets:
                    def _check_stale_rules_symlink(_path: Path = client_rules) -> CheckResult:
                        if _path.is_symlink():
                            return CheckResult(
                                ok=False,
                                message=".claude/rules is a directory symlink (pre-Phase-10 setup) — needs migration to per-file symlinks",
                                fix_cmd="rm .claude/rules && mkdir -p .claude/rules && mnemosyne doctor --fix",
                            )
                        return CheckResult(ok=True, message=".claude/rules is a real directory (migration done)")

                    def _fix_stale_rules_symlink(_path: Path = client_rules) -> None:
                        _path.unlink()
                        _path.mkdir(parents=True, exist_ok=True)

                    checks.append(
                        Check(
                            name=".claude/rules directory migration",
                            category="Symlinks",
                            _check_fn=_check_stale_rules_symlink,
                            _fix_fn=_fix_stale_rules_symlink,
                            fix_description="Remove stale directory symlink and create real directory",
                        )
                    )
                    # Skip per-file checks when stale directory symlink is present
                else:
                    for filename, target_rel in rules_targets.items():
                        target_abs = vault_path / target_rel
                        link_path = f".claude/rules/{filename}"
                        checks.append(
                            Check(
                                name=f".claude/rules/{filename} symlink",
                                category="Symlinks",
                                _check_fn=_perfile_symlink_check(link_path, target_abs),
                                _fix_fn=_perfile_symlink_fix(link_path, target_abs),
                                fix_description=f"Create {link_path} -> {target_rel}",
                            )
                        )

            # --- Category: Skills (.claude/skills/<name> directory symlinks) ---

            skills_yaml = claude_config / SKILLS_YAML_FILENAME
            legacy_commands_dir = claude_config / "commands"
            client_commands_dir = cwd / ".claude" / "commands"

            def _is_legacy_layout() -> bool:
                """Detect projects still using .claude/commands/*.md file symlinks."""
                if not legacy_commands_dir.is_dir():
                    return False
                targets = read_embed_targets(legacy_commands_dir)
                if not targets:
                    return False
                if not client_commands_dir.exists() and not client_commands_dir.is_symlink():
                    return False
                # Check for any .md file symlinks in .claude/commands/
                if client_commands_dir.is_dir() and not client_commands_dir.is_symlink():
                    for f in client_commands_dir.iterdir():
                        if f.suffix == ".md" and f.is_symlink():
                            return True
                return False

            if _is_legacy_layout():
                # Scenario A — legacy layout: single check that reports failure and
                # offers a 7-step atomic migration as the fix.

                def _check_legacy_layout(
                    _is_legacy: Callable[[], bool] = _is_legacy_layout,
                ) -> CheckResult:
                    # Re-evaluate dynamically so the re-check after --fix succeeds.
                    if _is_legacy():
                        return CheckResult(
                            ok=False,
                            message=(
                                "Legacy .claude/commands/*.md file symlinks detected — "
                                "run mnemosyne doctor --fix to migrate to .claude/skills/ layout"
                            ),
                            fix_cmd="mnemosyne doctor --fix",
                        )
                    return CheckResult(
                        ok=True,
                        message="Migration complete — .claude/skills/ layout in place",
                    )

                def _fix_legacy_layout(
                    _cwd: Path = cwd,
                    _vault_path: Path = vault_path,
                    _legacy_commands_dir: Path = legacy_commands_dir,
                    _client_commands_dir: Path = client_commands_dir,
                    _git_dir: Path = git_dir,
                ) -> None:
                    """7-step atomic migration from legacy .claude/commands layout."""
                    # Step 1: Parse legacy embed notes → collect skill names
                    legacy_targets = read_embed_targets(_legacy_commands_dir)
                    # Strip .md suffix from filenames to get skill names
                    skill_names_raw = [
                        fname[:-3] if fname.endswith(".md") else fname
                        for fname in legacy_targets
                    ]

                    # Step 2: Write claude-config/skills.yaml BEFORE any deletions
                    skills_yaml_path = _legacy_commands_dir.parent / SKILLS_YAML_FILENAME
                    tmp_path_yaml = skills_yaml_path.with_suffix(".yaml.tmp")
                    lines = ["skills:\n"] + [f"  - {name}\n" for name in skill_names_raw]
                    tmp_path_yaml.write_text("".join(lines), encoding="utf-8")
                    tmp_path_yaml.rename(skills_yaml_path)

                    # Step 3: Delete legacy .claude/commands/*.md file symlinks in project
                    if _client_commands_dir.is_dir() and not _client_commands_dir.is_symlink():
                        for f in list(_client_commands_dir.iterdir()):
                            if f.suffix == ".md" and f.is_symlink():
                                f.unlink()

                    # Step 4: Create new .claude/skills/<name>/ directory symlinks
                    expanded_names = expand_skill_names(skill_names_raw, _vault_path)
                    for name in expanded_names:
                        create_skill_symlink(_cwd, name, _vault_path)

                    # Step 5: Delete legacy embed note files and claude-config/commands/ dir
                    # Remove the embed .md files that were the source of truth before skills.yaml
                    if _legacy_commands_dir.is_dir():
                        for f in list(_legacy_commands_dir.iterdir()):
                            if f.suffix == ".md" and f.is_file():
                                f.unlink()
                    try:
                        _legacy_commands_dir.rmdir()
                    except OSError:
                        console.print(
                            f"    [yellow]Warning[/yellow]: {_legacy_commands_dir} is not empty — "
                            "leaving it in place. Remove manually after reviewing contents."
                        )

                    # Step 6: Update .git/info/exclude — add .claude/skills, remove .claude/commands
                    lib_git.add_git_exclusion(".claude/skills", _git_dir)
                    exclude_file = _git_dir / "info" / "exclude"
                    if exclude_file.exists():
                        lines_ex = exclude_file.read_text().splitlines()
                        filtered = [ln for ln in lines_ex if ln.strip() != ".claude/commands"]
                        exclude_file.write_text("\n".join(filtered) + ("\n" if filtered else ""))

                    # Step 7: Commit vault-side changes (skills.yaml + commands/ removal)
                    vault_git_dir = _vault_path  # pass vault root for git -C
                    # Determine the vault-relative path for the claude-config dir
                    try:
                        rel_config = str(
                            (_legacy_commands_dir.parent).relative_to(_vault_path)
                        )
                    except ValueError:
                        rel_config = str(_legacy_commands_dir.parent)
                    # Derive project name for commit message
                    project_rel = lib_vault.resolve_vault_project(_cwd, _vault_path) or rel_config
                    project_name = project_rel.split("/")[-1]
                    import subprocess as _sp
                    _sp.run(
                        ["git", "-C", str(vault_git_dir), "add", rel_config],
                        check=True,
                    )
                    _sp.run(
                        ["git", "-C", str(vault_git_dir), "commit",
                         "-m", f"♻️ {project_name}: migrate claude-config/commands/ → skills.yaml"],
                        check=True,
                    )

                checks.append(
                    Check(
                        name=".claude/skills layout (legacy project — migration needed)",
                        category="Skills",
                        _check_fn=_check_legacy_layout,
                        _fix_fn=_fix_legacy_layout,
                        fix_description=(
                            "Migrate .claude/commands/*.md → skills.yaml + .claude/skills/<name>/ "
                            "(7-step atomic migration)"
                        ),
                    )
                )

            elif skills_yaml.exists():
                # Scenario B — new layout: one check per skill in skills.yaml
                try:
                    raw_names = parse_skills_list(skills_yaml)
                    skill_names = expand_skill_names(raw_names, vault_path)
                except ValueError as exc:
                    checks.append(
                        Check(
                            name="skills.yaml parseable",
                            category="Skills",
                            _check_fn=lambda _e=exc: CheckResult(
                                ok=False,
                                message=f"skills.yaml error: {_e}",
                                fix_cmd=None,
                            ),
                        )
                    )
                    skill_names = []

                for name in skill_names:
                    checks.append(
                        Check(
                            name=f".claude/skills/{name} symlink",
                            category="Skills",
                            _check_fn=lambda _n=name: check_skill_symlink(cwd, _n, vault_path),
                            _fix_fn=lambda _n=name: create_skill_symlink(cwd, _n, vault_path),
                            fix_description=f"Create .claude/skills/{name}/ -> agents/skills/{name}/",
                        )
                    )

            else:
                # Scenario C — neither skills.yaml nor legacy commands exist
                checks.append(
                    Check(
                        name="skills.yaml not configured",
                        category="Skills",
                        _check_fn=lambda: CheckResult(
                            ok=True,
                            message="No skills.yaml in claude-config/ (no skills configured for this project)",
                        ),
                    )
                )

            # Tech stack auto-rules — derived from AGENTS.md Tech stack: line
            agents_target = vault_project_path / "AGENTS.md"
            if agents_target.exists():
                tech_stack = parse_tech_stack(agents_target)
                if tech_stack:
                    for tech in tech_stack:
                        tech_rules = discover_tech_rules(vault_path, tech)
                        for filename, target_abs in tech_rules.items():
                            link_path = f".claude/rules/{filename}"
                            checks.append(
                                Check(
                                    name=f".claude/rules/{filename} (tech stack)",
                                    category="Tech Stack Rules",
                                    _check_fn=_perfile_symlink_check(link_path, target_abs),
                                    _fix_fn=_perfile_symlink_fix(link_path, target_abs),
                                    fix_description=f"Create {link_path} -> {target_abs.relative_to(vault_path)}",
                                )
                            )

            settings_src = claude_config / "settings.json"
            if settings_src.exists():
                checks.append(
                    Check(
                        name=".claude/settings.json symlink",
                        category="Symlinks",
                        _check_fn=_symlink_check(".claude/settings.json", settings_src),
                        _fix_fn=_symlink_fix(".claude/settings.json", settings_src),
                        fix_description=f"Create .claude/settings.json -> {settings_src}",
                    )
                )
        else:
            # No .planning symlink yet — report as a single check failure
            checks.append(
                Check(
                    name=".planning symlink",
                    category="Symlinks",
                    _check_fn=lambda: CheckResult(
                        ok=False,
                        message=".planning symlink missing — cannot derive vault project path",
                        fix_cmd="mnemosyne init projects/<org>/<project>",
                    ),
                )
            )

        # --- Category: Git Exclusions ---

        def _exclusion_check(entry: str) -> Callable[[], CheckResult]:
            def _check() -> CheckResult:
                if lib_git.check_git_exclusion(entry, git_dir):
                    return CheckResult(ok=True, message=f"{entry} in .git/info/exclude")
                return CheckResult(
                    ok=False,
                    message=f"{entry} not in .git/info/exclude",
                    fix_cmd=f'echo "{entry}" >> {git_dir}/info/exclude',
                )

            return _check

        def _exclusion_fix(entry: str) -> Callable[[], None]:
            def _fix() -> None:
                lib_git.add_git_exclusion(entry, git_dir)

            return _fix

        # Determine which exclusions to check based on vault project
        exclusion_entries = [".planning", "AGENTS.md", "CLAUDE.md", ".envrc", "worktrees"]
        if vault_project_path is not None:
            claude_config = vault_project_path / "claude-config"
            if (claude_config / "rules").is_dir():
                exclusion_entries.append(".claude/rules")
            if (claude_config / SKILLS_YAML_FILENAME).exists():
                exclusion_entries.append(".claude/skills")
            if (claude_config / "settings.json").exists():
                exclusion_entries.append(".claude/settings.json")

        for entry in exclusion_entries:
            checks.append(
                Check(
                    name=f"Git exclusion: {entry}",
                    category="Git Exclusions",
                    _check_fn=_exclusion_check(entry),
                    _fix_fn=_exclusion_fix(entry),
                    fix_description=f"Add {entry} to .git/info/exclude",
                )
            )

        # --- Category: Environment File ---

        def check_envrc() -> CheckResult:
            envrc = cwd / ".envrc"
            if not envrc.exists():
                # .envrc is optional — config.toml is the recommended approach
                return CheckResult(
                    ok=True,
                    message=".envrc not present (optional — vault path configured via config.toml)",
                )
            return lib_envrc.check_envrc_vault(cwd, vault_path)

        def fix_envrc() -> None:
            lib_envrc.set_envrc_vault(cwd, vault_path)

        checks.append(
            Check(
                name=".envrc has correct MNEMOSYNE_VAULT",
                category="Environment File",
                _check_fn=check_envrc,
                _fix_fn=fix_envrc,
                fix_description=f"Write MNEMOSYNE_VAULT={vault_path} to .envrc",
            )
        )

    # --- Category: Merge Drivers ---

    def _merge_driver_check(key: str, expected_driver: str) -> Callable[[], CheckResult]:
        def _check() -> CheckResult:
            result = subprocess.run(
                ["git", "config", f"merge.{key}.driver"],
                cwd=vault_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return CheckResult(
                    ok=False,
                    message=f"merge.{key}.driver not configured in vault repo",
                    fix_cmd="mnemosyne doctor --fix",
                )
            actual = result.stdout.strip()
            if actual == expected_driver:
                return CheckResult(
                    ok=True,
                    message=f"merge.{key}.driver = {actual}",
                )
            return CheckResult(
                ok=False,
                message=f"merge.{key}.driver = {actual} (expected: {expected_driver})",
                fix_cmd="mnemosyne doctor --fix",
            )

        return _check

    def _merge_driver_fix(key: str, name: str, driver: str) -> Callable[[], None]:
        def _fix() -> None:
            subprocess.run(
                ["git", "config", f"merge.{key}.name", name],
                cwd=vault_path,
                check=True,
            )
            subprocess.run(
                ["git", "config", f"merge.{key}.driver", driver],
                cwd=vault_path,
                check=True,
            )

        return _fix

    _expected_state_driver = "mnemosyne merge-driver state %O %A %B"
    _expected_roadmap_driver = "mnemosyne merge-driver roadmap %O %A %B"

    checks.append(
        Check(
            name="Merge driver: gsd-state",
            category="Merge Drivers",
            _check_fn=_merge_driver_check("gsd-state", _expected_state_driver),
            _fix_fn=_merge_driver_fix(
                "gsd-state",
                "GSD STATE.md merge driver",
                _expected_state_driver,
            ),
            fix_description="Register gsd-state merge driver in vault repo",
        )
    )

    checks.append(
        Check(
            name="Merge driver: gsd-roadmap",
            category="Merge Drivers",
            _check_fn=_merge_driver_check("gsd-roadmap", _expected_roadmap_driver),
            _fix_fn=_merge_driver_fix(
                "gsd-roadmap",
                "GSD ROADMAP.md merge driver",
                _expected_roadmap_driver,
            ),
            fix_description="Register gsd-roadmap merge driver in vault repo",
        )
    )

    # --- Category: Worktrees ---

    def check_stale_vault_worktrees() -> CheckResult:
        worktrees_dir = vault_path / "worktrees"
        if not worktrees_dir.is_dir():
            return CheckResult(ok=True, message="No worktrees/ directory (nothing to check)")
        try:
            active_wts = lib_git.list_worktrees(vault_path)
        except Exception:
            return CheckResult(ok=True, message="Could not enumerate git worktrees (skipped)")
        active_paths = {Path(wt["worktree"]).resolve() for wt in active_wts}
        stale = [
            d.name for d in worktrees_dir.iterdir()
            if d.is_dir() and not any(
                d.resolve() == ap or d.resolve() in ap.parents
                for ap in active_paths
            )
        ]
        if stale:
            return CheckResult(
                ok=False,
                message=f"Stale worktree dir(s) not registered in git: {stale}",
                fix_cmd="git worktree prune  # then manually remove leftover dirs",
            )
        return CheckResult(ok=True, message=f"No stale vault worktrees ({len(active_paths) - 1} active)")

    checks.append(Check(
        name="No stale vault worktrees",
        category="Worktrees",
        _check_fn=check_stale_vault_worktrees,
    ))

    def check_orphaned_planning_dirs() -> CheckResult:
        projects_root = vault_path / "projects"
        if not projects_root.is_dir():
            return CheckResult(ok=True, message="No projects/ directory (skipped)")
        orphaned = []
        for project_dir in projects_root.glob("*/*"):
            if not project_dir.is_dir():
                continue
            for d in project_dir.iterdir():
                if d.is_dir() and d.name.startswith("gsd-planning-"):
                    branch = d.name[len("gsd-planning-"):]
                    result = subprocess.run(
                        ["git", "branch", "--list", branch],
                        cwd=vault_path, capture_output=True, text=True,
                    )
                    if not result.stdout.strip():
                        orphaned.append(str(d.relative_to(vault_path)))
        if orphaned:
            return CheckResult(
                ok=False,
                message=f"Orphaned planning dir(s) (branch gone): {orphaned}",
                fix_cmd="mnemosyne work finish <branch>  # or delete manually after verifying work is safe",
            )
        return CheckResult(ok=True, message="No orphaned planning dirs")

    checks.append(Check(
        name="No orphaned per-worktree planning dirs",
        category="Worktrees",
        _check_fn=check_orphaned_planning_dirs,
    ))

    if not is_vault:
        def check_worktree_planning_symlinks() -> CheckResult:
            worktrees_dir = cwd / "worktrees"
            if not worktrees_dir.is_dir():
                return CheckResult(ok=True, message="No worktrees/ directory in project (nothing to check)")
            broken = []
            for wt_dir in worktrees_dir.iterdir():
                if not wt_dir.is_dir():
                    continue
                planning_link = wt_dir / ".planning"
                if planning_link.is_symlink():
                    target = planning_link.resolve()
                    if not target.exists():
                        broken.append(str(wt_dir.name))
            if broken:
                return CheckResult(
                    ok=False,
                    message=f"Broken .planning symlinks in worktree(s): {broken}",
                    fix_cmd="mnemosyne work finish <branch> && mnemosyne work start <branch>",
                )
            return CheckResult(ok=True, message="No broken .planning symlinks in worktrees/")

        checks.append(Check(
            name="No broken .planning symlinks in project worktrees",
            category="Worktrees",
            _check_fn=check_worktree_planning_symlinks,
        ))

    # --- Category: Freshness ---

    def check_images_fresh() -> CheckResult:
        """Check if local container images match the latest registry digest."""
        import shutil

        if not shutil.which("podman"):
            return CheckResult(ok=True, message="podman not found (skipped)")

        if not shutil.which("skopeo"):
            return CheckResult(ok=True, message="skopeo not found -- cannot check registry freshness (skipped)")

        stale = []
        for name in ["mnemosyne-base", "mnemosyne-claude"]:
            # Get local digest from the localhost/ tag that refresh creates
            local_result = subprocess.run(
                ["podman", "image", "inspect", f"localhost/{name}:latest",
                 "--format", "{{index .RepoDigests 0}}"],
                capture_output=True, text=True,
            )
            if local_result.returncode != 0:
                stale.append(f"{name} (not pulled)")
                continue
            local_digest = local_result.stdout.strip() or None

            # Get remote digest from registry
            remote_result = subprocess.run(
                ["skopeo", "inspect", "--format", "{{.Digest}}",
                 f"docker://ghcr.io/empiria/{name}:latest"],
                capture_output=True, text=True,
                timeout=15,
            )
            if remote_result.returncode != 0:
                # Registry unreachable — skip, don't fail
                continue
            remote_digest = remote_result.stdout.strip()

            if local_digest is None or remote_digest not in local_digest:
                stale.append(f"{name} (out of date with registry)")

        if stale:
            return CheckResult(
                ok=False,
                message=f"Container image(s) stale: {', '.join(stale)}",
                fix_cmd="mnemosyne refresh --skip-qmd",
            )
        return CheckResult(ok=True, message="Container images up to date with registry")

    checks.append(Check(
        name="Container images up to date",
        category="Freshness",
        _check_fn=check_images_fresh,
    ))

    def check_qmd_fresh() -> CheckResult:
        """Check if qmd index is older than vault content."""
        import shutil

        if not shutil.which("qmd"):
            return CheckResult(ok=True, message="qmd not found (skipped)")

        # Get qmd index timestamp from status
        result = subprocess.run(
            ["qmd", "status"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            return CheckResult(
                ok=False,
                message="qmd status failed — index may not exist",
                fix_cmd="mnemosyne refresh --skip-images",
            )

        # Find most recently modified .md file in the vault
        latest_md = max(
            (f.stat().st_mtime for f in vault_path.rglob("*.md")
             if ".git" not in f.parts and ".planning" not in f.parts
             and "node_modules" not in f.parts),
            default=0,
        )
        if latest_md == 0:
            return CheckResult(ok=True, message="No markdown files found (skipped)")

        # Get index timestamp from qmd ls output modification time
        # Simpler: check if qmd's own index file is older than latest vault content
        index_candidates = list(Path("~/.cache/qmd").expanduser().glob("**/index.*"))
        if not index_candidates:
            return CheckResult(
                ok=False,
                message="qmd index not found",
                fix_cmd="mnemosyne refresh --skip-images",
            )

        latest_index = max(f.stat().st_mtime for f in index_candidates)
        if latest_md > latest_index:
            return CheckResult(
                ok=False,
                message="Vault content modified since last qmd index update",
                fix_cmd="mnemosyne refresh --skip-images",
            )
        return CheckResult(ok=True, message="qmd index up to date")

    checks.append(Check(
        name="qmd index up to date",
        category="Freshness",
        _check_fn=check_qmd_fresh,
    ))

    # --- Category: Hooks ---

    if is_vault:
        _hook_script_content = "#!/bin/sh\nmnemosyne hook post-change\n"

        def _hook_check(hook_name: str) -> Callable[[], CheckResult]:
            def _check() -> CheckResult:
                hook_path = git_dir / "hooks" / hook_name
                if not hook_path.exists() and not hook_path.is_symlink():
                    return CheckResult(
                        ok=False,
                        message=f"{hook_name} hook not installed",
                        fix_cmd=f"mnemosyne doctor --fix",
                    )
                try:
                    content = hook_path.read_text()
                except OSError:
                    return CheckResult(
                        ok=False,
                        message=f"{hook_name} hook unreadable",
                        fix_cmd="mnemosyne doctor --fix",
                    )
                if "mnemosyne hook post-change" in content:
                    return CheckResult(ok=True, message=f"{hook_name} hook installed")
                return CheckResult(
                    ok=False,
                    message=f"{hook_name} hook exists but does not call 'mnemosyne hook post-change'",
                    fix_cmd="mnemosyne doctor --fix",
                )
            return _check

        def _hook_fix(hook_name: str) -> Callable[[], None]:
            def _fix(_name: str = hook_name) -> None:
                hook_path = git_dir / "hooks" / _name
                hook_path.parent.mkdir(parents=True, exist_ok=True)
                hook_path.write_text(_hook_script_content)
                hook_path.chmod(0o755)
            return _fix

        for hook_name in ("post-commit", "post-merge"):
            checks.append(Check(
                name=f"Git {hook_name} hook",
                category="Hooks",
                _check_fn=_hook_check(hook_name),
                _fix_fn=_hook_fix(hook_name),
                fix_description=f"Write .git/hooks/{hook_name} calling mnemosyne hook post-change",
            ))

    # --- Category: CLI ---

    def check_cli_version() -> CheckResult:
        try:
            installed = importlib.metadata.version("mnemosyne-cli")
        except importlib.metadata.PackageNotFoundError:
            return CheckResult(
                ok=False,
                message="mnemosyne-cli package not found (not installed via uv tool)",
                fix_cmd="uv tool install --editable $MNEMOSYNE_CLI_REPO",
            )

        # pyproject.toml lives in the CLI repo, not the vault
        pyproject = _cli_repo_root() / "pyproject.toml"
        if not pyproject.exists():
            return CheckResult(
                ok=True,
                message=f"CLI version {installed} (pyproject.toml not found, skipping repo check)",
            )

        with open(pyproject, "rb") as f:
            data = tomllib.load(f)

        repo_version = data.get("project", {}).get("version")
        if not repo_version:
            return CheckResult(
                ok=True,
                message=f"CLI version {installed} (repo version not in pyproject.toml)",
            )

        if installed == repo_version:
            return CheckResult(ok=True, message=f"CLI version {installed} matches repo")

        return CheckResult(
            ok=False,
            message=f"CLI version {installed} does not match repo version {repo_version}",
            fix_cmd="uv tool install --editable $MNEMOSYNE_CLI_REPO",
        )

    checks.append(
        Check(
            name="CLI version matches pyproject.toml",
            category="CLI",
            _check_fn=check_cli_version,
        )
    )

    return checks


def run(
    fix: bool = typer.Option(False, "--fix", help="Apply fixes with per-fix confirmation"),
) -> None:
    """Validate project setup and report issues."""
    cwd = Path.cwd()

    # Resolve vault path
    vault_path = lib_vault.resolve_vault_path()

    # Get git dir (needed for exclusion checks)
    try:
        git_dir = lib_git.get_git_dir(cwd)
    except Exception:
        # If not in a git repo, git_dir won't be usable — use a sentinel
        git_dir = cwd / ".git"

    checks = _build_checks(cwd, vault_path, git_dir)

    # Group checks by category
    categories: dict[str, list[Check]] = {}
    for check in checks:
        categories.setdefault(check.category, []).append(check)

    total_pass = 0
    total_fail = 0
    any_unfixed_failures = False

    for category, cat_checks in categories.items():
        console.rule(f"[bold]{category}[/bold]")

        for check in cat_checks:
            result = check.check()
            if result.ok:
                console.print(f"  [green]  pass[/green] {check.name}")
                total_pass += 1
            else:
                console.print(f"  [red]  FAIL[/red] {check.name}")
                console.print(f"         {result.message}")
                if result.fix_cmd:
                    console.print(f"    Fix: {result.fix_cmd}")
                total_fail += 1

                if fix and check.has_fix():
                    console.print(f"    [dim]{check.fix_description}[/dim]")
                    try:
                        confirmed = typer.confirm("    Apply fix?", default=True)
                    except typer.Abort:
                        console.print("    [yellow]Skipped[/yellow]")
                        any_unfixed_failures = True
                        continue

                    if confirmed:
                        try:
                            check.apply_fix()
                            # Re-check after fix
                            recheck = check.check()
                            if recheck.ok:
                                console.print(f"    [green]Fixed[/green] {check.name}")
                                total_fail -= 1
                                total_pass += 1
                            else:
                                console.print(f"    [red]Fix failed[/red]: {recheck.message}")
                                any_unfixed_failures = True
                        except Exception as exc:
                            console.print(f"    [red]Fix error[/red]: {exc}")
                            any_unfixed_failures = True
                    else:
                        console.print("    [yellow]Skipped[/yellow]")
                        any_unfixed_failures = True
                else:
                    any_unfixed_failures = True

    # Summary
    console.print()
    total = total_pass + total_fail
    if total_fail == 0:
        console.print(f"[green]All {total} checks passed.[/green]")
    else:
        console.print(
            f"[yellow]{total_pass}/{total} checks passed,[/yellow] "
            f"[red]{total_fail} failed.[/red]"
        )

    if any_unfixed_failures:
        raise typer.Exit(1)
