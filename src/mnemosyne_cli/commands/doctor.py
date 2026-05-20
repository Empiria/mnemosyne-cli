"""mnemosyne doctor — validate project setup with optional --fix repair."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import typer
from packaging.version import Version
from rich.console import Console

from mnemosyne_cli.lib import checks as lib_checks
from mnemosyne_cli.lib import envrc as lib_envrc
from mnemosyne_cli.lib import git as lib_git
from mnemosyne_cli.lib import overrides as lib_overrides
from mnemosyne_cli.lib import scion_cache as lib_scion_cache
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
    find_orphan_skill_symlinks,
    remove_skill_symlink,
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


def _build_checks(
    cwd: Path,
    vault_path: Path,
    git_dir: Path,
    container: bool = False,
) -> list[Check]:
    """Build the full list of checks for the current directory."""
    checks: list[Check] = []

    if container:
        # D-21 / D-22: in-container bootstrap checks only.
        # Host-codebase categories (Symlinks, Skills, .claude/rules, etc.) are
        # designed for client-codebase host runs and would FAIL inside a
        # container where cwd is /workspace. Skip them entirely.
        #
        # Workspace resolution mirrors the post-start hook: prefer the
        # SCION-derived path /repo-root/.scion/agents/$SCION_AGENT_SLUG/workspace
        # when it exists (the real git worktree), then fall back to
        # MNEMOSYNE_WORKSPACE, then /workspace. The template-level
        # MNEMOSYNE_WORKSPACE=/workspace points at an empty placeholder dir
        # inside the image — not the agent's actual workspace.
        scion_slug = os.environ.get("SCION_AGENT_SLUG", "")
        scion_workspace = (
            Path(f"/repo-root/.scion/agents/{scion_slug}/workspace")
            if scion_slug
            else None
        )
        if scion_workspace is not None and scion_workspace.is_dir():
            target = scion_workspace
        else:
            target_str = os.environ.get("MNEMOSYNE_WORKSPACE", "/workspace")
            target = Path(target_str)

        def _wrap(check_fn: Callable[[], CheckResult], name: str) -> Check:
            # Container checks are READ-ONLY per D-21 — no _fix_fn attached.
            return Check(
                name=name,
                category="Container Bootstrap",
                _check_fn=check_fn,
            )

        checks.append(_wrap(lib_checks.check_mnemosyne_on_path, "mnemosyne on PATH"))
        checks.append(_wrap(lib_checks.check_gsd_tools_on_path, "gsd-tools on PATH"))
        checks.append(
            _wrap(
                lambda: lib_checks.check_user_skills_populated(vault_path),
                "~/.claude/skills populated",
            )
        )
        checks.append(
            _wrap(
                lambda: lib_checks.check_workspace_planning(target, vault_path),
                "/workspace/.planning resolves into vault",
            )
        )
        checks.append(
            _wrap(
                lib_checks.check_required_env_vars,
                "MNEMOSYNE_WORKSPACE + MNEMOSYNE_PROJECT set",
            )
        )
        checks.append(
            _wrap(
                lib_checks.check_init_status_file,
                "post-start hook last run succeeded",
            )
        )

        return checks

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

            # CLAUDE.md (local symlink) — only when upstream doesn't track
            # CLAUDE.md. When it does, the Local Overrides category covers
            # both symlink existence AND the sparse-checkout/assume-unchanged
            # plumbing needed to keep the symlink from leaking upstream.
            if not lib_overrides.is_tracked(cwd, "CLAUDE.md"):
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

                # Orphan / dangling skill symlinks — symlinks in .claude/skills/
                # not listed in skills.yaml, or pointing at a vault skill that no
                # longer exists.  Surfaces stale entries after a project's
                # allowlist shrinks (e.g. the GSD-wrapper skill removal).
                _allowed = list(skill_names)

                def _check_orphans(_a: list[str] = _allowed) -> CheckResult:
                    orphans = find_orphan_skill_symlinks(cwd, _a)
                    if not orphans:
                        return CheckResult(ok=True, message="no orphan .claude/skills/ entries")
                    return CheckResult(
                        ok=False,
                        message=(
                            f"orphan .claude/skills/ symlinks: {', '.join(orphans)} — "
                            "not in skills.yaml or target missing"
                        ),
                        fix_cmd=None,
                    )

                def _fix_orphans(_a: list[str] = _allowed) -> None:
                    for n in find_orphan_skill_symlinks(cwd, _a):
                        remove_skill_symlink(cwd, n)

                checks.append(
                    Check(
                        name=".claude/skills/ has no orphan symlinks",
                        category="Skills",
                        _check_fn=_check_orphans,
                        _fix_fn=_fix_orphans,
                        fix_description="Remove orphan .claude/skills/<name> symlinks",
                    )
                )

            else:
                # Scenario C — neither skills.yaml nor legacy commands exist.
                # Flag as a failure: without skills.yaml, `mnemosyne init` silently
                # skips .claude/skills/ population. New projects scaffolded by
                # `mnemosyne add` get one for free; older projects need it added.
                _skills_yaml_path = skills_yaml

                def _check_skills_yaml_exists(
                    _path: Path = _skills_yaml_path,
                ) -> CheckResult:
                    if _path.exists():
                        return CheckResult(
                            ok=True,
                            message=f"skills.yaml present at {_path}",
                        )
                    return CheckResult(
                        ok=False,
                        message=(
                            f"Missing {_path} — without it, .claude/skills/ stays empty. "
                            "Add a skills: list to that file in the vault "
                            "(see projects/friendly-fox/infinite-worlds/claude-config/skills.yaml "
                            "for a template), commit, and re-run mnemosyne doctor."
                        ),
                        fix_cmd=None,
                    )

                checks.append(
                    Check(
                        name="claude-config/skills.yaml exists",
                        category="Skills",
                        _check_fn=_check_skills_yaml_exists,
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

        # --- Category: Local Overrides ---
        # CLAUDE.md override — only relevant when AGENTS.md is our vault symlink
        # AND upstream has begun tracking CLAUDE.md. Implementation lives in
        # lib/overrides.py and is shared with `mnemosyne init`.
        if (cwd / "AGENTS.md").is_symlink() and lib_overrides.is_tracked(cwd, "CLAUDE.md"):

            def check_claude_md_override(
                _cwd: Path = cwd,
                _git_dir: Path = git_dir,
            ) -> CheckResult:
                problems = lib_overrides.diagnose_claude_md_override(_cwd, _git_dir)
                if problems:
                    return CheckResult(
                        ok=False,
                        message="CLAUDE.md override broken: " + "; ".join(problems),
                        fix_cmd="mnemosyne doctor --fix",
                    )
                return CheckResult(ok=True, message="CLAUDE.md override active")

            def fix_claude_md_override(
                _cwd: Path = cwd,
                _git_dir: Path = git_dir,
            ) -> None:
                lib_overrides.apply_claude_md_override(_cwd, _git_dir)

            checks.append(
                Check(
                    name="CLAUDE.md override (upstream-tracked, locally redirected)",
                    category="Local Overrides",
                    _check_fn=check_claude_md_override,
                    _fix_fn=fix_claude_md_override,
                    fix_description=(
                        "Configure sparse-checkout exclusion + assume-unchanged "
                        "and link CLAUDE.md -> AGENTS.md"
                    ),
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
            # Get local digests from the localhost/ tag that refresh creates.
            # Podman stores multiple RepoDigests per image (e.g. manifest-list
            # vs. image-manifest variants of the same content), so iterate the
            # whole slice and substring-match below — picking only index 0
            # produces false positives when the freshly-pulled digest lands at
            # a higher index.
            local_result = subprocess.run(
                ["podman", "image", "inspect", f"localhost/{name}:latest",
                 "--format", "{{range .RepoDigests}}{{.}} {{end}}"],
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
                if hook_path.is_symlink() and not hook_path.exists():
                    return CheckResult(
                        ok=False,
                        message=(
                            f"{hook_name} hook is a broken symlink "
                            f"(-> {os.readlink(hook_path)})"
                        ),
                        fix_cmd="mnemosyne doctor --fix",
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
                # If the path is an existing symlink (possibly broken) or a stale
                # file, unlink it first so write_text() doesn't follow the symlink
                # and try to write through to a non-existent target.
                if hook_path.is_symlink() or hook_path.is_file():
                    hook_path.unlink()
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

    def check_model_profile_deprecated() -> CheckResult:
        """Warn if the dead ``model_profile`` key lingers in config.toml.

        The ``mnemosyne model`` command was removed when the GSD-wrapper skills
        were retired (vault Phase 36).  The config key is no longer consulted,
        and an unset key here is the desired state; we report-only so users can
        remove it manually next time they edit the file.
        """
        cfg = lib_vault._read_config()
        if "model_profile" in cfg:
            return CheckResult(
                ok=False,
                message=(
                    "config.toml still sets 'model_profile' — the key is no longer "
                    "consulted (the 'mnemosyne model' command was removed). "
                    "Remove it next time you edit ~/.config/mnemosyne/config.toml."
                ),
                fix_cmd=None,
            )
        return CheckResult(ok=True, message="no deprecated keys in config.toml")

    checks.append(
        Check(
            name="config.toml has no deprecated keys",
            category="CLI",
            _check_fn=check_model_profile_deprecated,
        )
    )

    # --- Category: Components (mnemosyne project only) ---

    if _components_apply_here(cwd, vault_path):
        from mnemosyne_cli.commands.component import _read_declared_components
        from mnemosyne_cli.lib.components import (
            ComponentNotCloned,
            ComponentNotConfigured,
            resolve_component_path,
        )

        declared = _read_declared_components(vault_path, "projects/empiria/mnemosyne")

        for component_name in [c for c in declared if c != "mnemosyne"]:
            def _make_check(name: str) -> Callable[[], CheckResult]:
                def _check() -> CheckResult:
                    try:
                        path = resolve_component_path(name)
                        return CheckResult(
                            ok=True,
                            message=f"{name} configured at {path}",
                        )
                    except ComponentNotConfigured:
                        return CheckResult(
                            ok=False,
                            message=f"{name} not configured in ~/.config/mnemosyne/config.toml",
                            fix_cmd=(
                                f'echo "[components.{name}]\\nlocal_path = '
                                f'\\"~/projects/<org>/{name}\\"" '
                                f">> ~/.config/mnemosyne/config.toml"
                            ),
                        )
                    except ComponentNotCloned as exc:
                        return CheckResult(
                            ok=False,
                            message=f"{name} configured at {exc.path} but not cloned",
                            fix_cmd=f"git clone <repo-url> {exc.path}",
                        )
                return _check

            checks.append(Check(
                name=f"Component: {component_name}",
                category="Components",
                _check_fn=_make_check(component_name),
            ))

    # --- Category: SCION Template Freshness (SBR-06, D-18/D-19/D-20) ---
    # Detect drift between the broker's cached template and the vault's
    # agents/scion-template/. Skips silently on machines that don't run a
    # broker (D-19 graceful skip).

    cache_root = lib_scion_cache.find_broker_cache_root()
    if cache_root is None:

        def _check_no_broker() -> CheckResult:
            return CheckResult(
                ok=True,
                message="broker not on this machine — SCION template freshness skipped",
            )

        checks.append(
            Check(
                name="SCION broker cache",
                category="SCION Template Freshness",
                _check_fn=_check_no_broker,
            )
        )
    else:
        index = lib_scion_cache.read_template_index(cache_root)
        if not index or not index.get("entries"):

            def _check_no_index() -> CheckResult:
                return CheckResult(
                    ok=True,
                    message=f"broker cache empty at {cache_root} — nothing to diff",
                )

            checks.append(
                Check(
                    name="SCION broker cache index",
                    category="SCION Template Freshness",
                    _check_fn=_check_no_index,
                )
            )
        else:
            # Vault-side template-dir mapping by convention. Phase 34
            # (scion-template-mnemosyne) will add mnemosyne-agent once that
            # template ships — out of scope here per D-05.
            template_id_to_vault_dir = {
                "empiria-agent": vault_path / "agents" / "scion-template",
            }
            for tid, vdir in template_id_to_vault_dir.items():
                if tid not in index.get("entries", {}):
                    continue
                if not vdir.is_dir():
                    continue

                def _make_drift_check(
                    tid_local: str, vdir_local: Path, cache_root_local: Path
                ) -> Callable[[], CheckResult]:
                    def _check() -> CheckResult:
                        drift = lib_scion_cache.diff_template_against_vault(
                            cache_root_local, tid_local, vdir_local
                        )
                        if not drift:
                            return CheckResult(
                                ok=True,
                                message=f"{tid_local} in sync with vault",
                            )
                        return CheckResult(
                            ok=False,
                            message=(
                                f"{tid_local} drift: "
                                f"{len(drift)} file(s) differ — {', '.join(drift[:5])}"
                                f"{' …' if len(drift) > 5 else ''}"
                            ),
                            fix_cmd=(
                                f"scion template sync {tid_local} && "
                                f"rm -rf ~/.scion/cache/templates && "
                                f"systemctl --user restart scion-broker"
                            ),
                        )

                    return _check

                checks.append(
                    Check(
                        name=f"{tid} template freshness",
                        category="SCION Template Freshness",
                        _check_fn=_make_drift_check(tid, vdir, cache_root),
                    )
                )

    # --- Category: Claude Onboarding Drift (SBR-2.2, D-08/D-09/D-10) ---
    # Compare SCION harness-config template's lastOnboardingVersion against
    # the host's ~/.claude.json. Drift means fresh agents will re-run
    # onboarding. Sibling to the SCION Template Freshness category — different
    # inputs (host JSON, not broker cache), different reset command
    # (scion harness-config reset, not scion template sync). Host-side only;
    # the in-container variant of this check is deferred (CONTEXT "NOT in
    # this phase" item 6).
    if not container:
        checks.append(
            Check(
                name="Claude onboarding-version drift",
                category="Claude Onboarding Drift",
                _check_fn=_check_claude_onboarding_drift,
            )
        )

    # --- Category: Broker Reliability (SBR-3.3, D-15/D-16) ---
    # Host-side only, read-only (D-21 inherited — no _fix_fn). Queries
    # `scion hub brokers --json` (NOT log-tail grep) so the same helper backs
    # both this tier-1 doctor check and the tier-2 Path-unit watchdog verb.
    if not container:
        checks.append(
            Check(
                name="broker control-channel health",
                category="Broker Reliability",
                _check_fn=_check_broker_control_channel_health,
            )
        )

    # --- Category: Operator State Drift (SBR-3.7, D-29 a/b/c) ---
    # Host-side only, read-only (D-21 inherited — no _fix_fn). Surfaces the
    # three operator-state drift classes that 33.2 UAT discovered late;
    # Wave 2 Plan 03 ships `mnemosyne broker apply-empiria-defaults` which
    # the fix_cmd messages point at for convergence.
    if not container:
        checks.append(
            Check(
                name="user settings.yaml auth_selected_type",
                category="Operator State Drift",
                _check_fn=_check_user_settings_auth_type,
            )
        )
        checks.append(
            Check(
                name="grove settings.yaml empiria defaults",
                category="Operator State Drift",
                _check_fn=_check_grove_settings,
            )
        )
        checks.append(
            Check(
                name="user profile env no MNEMOSYNE_VAULT override",
                category="Operator State Drift",
                _check_fn=_check_user_profile_env_no_overrides,
            )
        )

    return checks


def _check_claude_onboarding_drift() -> CheckResult:
    """SBR-2.2 / D-08 / D-09 / D-10: compare SCION harness-config template's
    lastOnboardingVersion against the host's ~/.claude.json.

    Reads ~/.scion/harness-configs/claude/home/.claude.json (template) and
    ~/.claude.json (host). Drift (template < host) means fresh agents will
    re-run onboarding pages.

    Pure read; no _fix_fn attached (Phase 33.1 D-21 — doctor read-only in 33.x).
    Skipped (with context) when host has not run claude yet (D-10) or when
    the SCION harness-config template has not been seeded on this machine.
    """
    template_path = Path.home() / ".scion" / "harness-configs" / "claude" / "home" / ".claude.json"
    host_path = Path.home() / ".claude.json"

    if not template_path.exists():
        return CheckResult(
            ok=True,
            message=(
                "SCION claude harness-config template not seeded on this machine — "
                "run `scion init --machine` then apply the Empiria seed (see "
                "agents/scion-template/claude-harness-config/README.md)."
            ),
        )
    if not host_path.exists():
        return CheckResult(
            ok=True,
            message=(
                "Host Claude has not run on this machine — drift can't be assessed. "
                "Run `claude` once on the host then re-run doctor."
            ),
        )

    try:
        template_data = json.loads(template_path.read_text())
        host_data = json.loads(host_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            ok=False,
            message=f"Could not read claude config: {exc}",
        )

    template_ver = template_data.get("lastOnboardingVersion")
    host_ver = host_data.get("lastOnboardingVersion")

    if not template_ver or not host_ver:
        return CheckResult(
            ok=False,
            message=(
                f"Missing lastOnboardingVersion field — "
                f"template={template_ver!r}, host={host_ver!r}"
            ),
        )

    if Version(template_ver) < Version(host_ver):
        return CheckResult(
            ok=False,
            message=(
                f"Onboarding drift: template={template_ver}, host={host_ver}. "
                f"Fresh agents will re-run onboarding."
            ),
            fix_cmd=(
                f"Edit ~/.scion/harness-configs/claude/home/.claude.json — "
                f"set lastOnboardingVersion to {host_ver!r} and lastReleaseNotesSeen "
                f"to match, then `scion harness-config reset claude && "
                f"rm -rf ~/.scion/cache/templates && systemctl --user restart "
                f"scion-broker`. See agents/scion-template/claude-harness-config/README.md."
            ),
        )

    return CheckResult(
        ok=True,
        message=f"template={template_ver} ≥ host={host_ver}",
    )


# ---------------------------------------------------------------------------
# SBR-3.7: Operator-state drift checks (Phase 33.3)
# ---------------------------------------------------------------------------

EXPECTED_AUTH_SELECTED_TYPE = "oauth-token"
EXPECTED_GROVE_TEMPLATE = "empiria-agent"
EXPECTED_GROVE_HARNESS = "claude"


def _check_user_settings_auth_type() -> CheckResult:
    """SBR-3.7 (a): ~/.scion/settings.yaml has harness_configs.claude.auth_selected_type=oauth-token.

    Phase 33.2 Plan 01 switched documented auth from CLAUDE_AUTH file-secret to
    CLAUDE_CODE_OAUTH_TOKEN env-secret. Operators following pre-33.2 docs left
    this field at 'auth-file'; the broker keeps mounting the deprecated secret
    until this field flips to 'oauth-token'. (UAT-discovered late in 33.2.)

    Pure read; no _fix_fn attached (Phase 33.1 D-21 — doctor read-only in 33.x).
    """
    import yaml

    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.scion_paths import user_settings_path

    path = user_settings_path()
    try:
        data = broker_lib.yaml_safe_load_or_none(path)
    except yaml.YAMLError as e:
        return CheckResult(ok=False, message=f"Malformed YAML at {path}: {e}")
    if data is None:
        return CheckResult(
            ok=True,
            message=(
                f"No {path} — broker may not be initialised "
                "(run `mnemosyne broker install`)."
            ),
        )
    actual = (data.get("harness_configs") or {}).get("claude", {}).get(
        "auth_selected_type"
    )
    if actual != EXPECTED_AUTH_SELECTED_TYPE:
        return CheckResult(
            ok=False,
            message=(
                f"{path}: harness_configs.claude.auth_selected_type={actual!r}; "
                f"expected {EXPECTED_AUTH_SELECTED_TYPE!r}"
            ),
            fix_cmd="mnemosyne broker apply-empiria-defaults",
        )
    return CheckResult(
        ok=True, message=f"{path}: auth_selected_type={EXPECTED_AUTH_SELECTED_TYPE}"
    )


def _check_grove_settings() -> CheckResult:
    """SBR-3.7 (b): every grove's .scion/settings.yaml has Empiria default_template + default_harness_config.

    Drift class verified live on 2026-05-19: mnemosyne__9aa1f789 has
    default_template=default, default_harness_config=gemini (instead of
    empiria-agent + claude).

    SCION's auto-test groves (auto-*, test-*, cleanup-*, etc.) are skipped
    by lib/scion_paths.DEFAULT_SKIP_PREFIXES.

    Pure read; no _fix_fn attached (Phase 33.1 D-21 — doctor read-only in 33.x).
    """
    import yaml

    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.scion_paths import iter_grove_settings_paths

    drifted: list[str] = []
    for path in iter_grove_settings_paths():
        try:
            data = broker_lib.yaml_safe_load_or_none(path)
        except yaml.YAMLError as e:
            return CheckResult(ok=False, message=f"Malformed YAML at {path}: {e}")
        if data is None:
            continue  # iter only yields existing files — defensive
        tmpl = data.get("default_template")
        harness = data.get("default_harness_config")
        if tmpl != EXPECTED_GROVE_TEMPLATE or harness != EXPECTED_GROVE_HARNESS:
            # path is ~/.scion/grove-configs/<grove>/.scion/settings.yaml
            # grove name is parent of parent.
            drifted.append(
                f"{path.parent.parent.name}: template={tmpl!r} harness={harness!r}"
            )
    if drifted:
        joined = "\n  ".join(drifted)
        return CheckResult(
            ok=False,
            message=f"Groves with non-Empiria defaults:\n  {joined}",
            fix_cmd="mnemosyne broker apply-empiria-defaults",
        )
    return CheckResult(
        ok=True,
        message=f"All groves use {EXPECTED_GROVE_TEMPLATE} + {EXPECTED_GROVE_HARNESS} defaults",
    )


def _check_user_profile_env_no_overrides() -> CheckResult:
    """SBR-3.7 (c): no ~/.scion/settings.yaml profile sets MNEMOSYNE_VAULT.

    Drift class observed live in 33.2 UAT: profiles.local.env.MNEMOSYNE_VAULT
    shadowed the empiria-agent template's /vault mount target. The template
    provides MNEMOSYNE_VAULT=/vault inside the container — user-profile env
    is an unintended override.

    Pure read; no _fix_fn attached (Phase 33.1 D-21 — doctor read-only in 33.x).
    """
    import yaml

    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.scion_paths import user_settings_path

    path = user_settings_path()
    try:
        data = broker_lib.yaml_safe_load_or_none(path)
    except yaml.YAMLError as e:
        return CheckResult(ok=False, message=f"Malformed YAML at {path}: {e}")
    if data is None:
        return CheckResult(ok=True, message=f"No {path}.")
    overrides: list[str] = []
    for profile_name, profile in (data.get("profiles") or {}).items():
        env = (profile or {}).get("env") or {}
        if "MNEMOSYNE_VAULT" in env:
            overrides.append(
                f"profiles.{profile_name}.env.MNEMOSYNE_VAULT={env['MNEMOSYNE_VAULT']}"
            )
    if overrides:
        joined = "; ".join(overrides)
        return CheckResult(
            ok=False,
            message=(
                f"User profile env overrides container-only var: {joined}. "
                "Remove these — the empiria-agent template's /vault mount is "
                "the canonical target."
            ),
            fix_cmd="mnemosyne broker apply-empiria-defaults",
        )
    return CheckResult(
        ok=True, message="No profile env overrides for MNEMOSYNE_VAULT"
    )


def _check_broker_control_channel_health() -> CheckResult:
    """SBR-3.3 tier-1: broker control-channel staleness check.

    Delegates to `broker.check_control_channel()` (DRY — the same helper backs
    the tier-2 Path-unit watchdog verb). The helper queries
    `scion hub brokers --json` and compares lastHeartbeat against a 120s
    freshness threshold. Pure read; no _fix_fn (Phase 33.1 D-21).
    """
    from mnemosyne_cli.lib import broker as broker_lib

    return broker_lib.check_control_channel()


def _components_apply_here(cwd: Path, vault_path: Path) -> bool:
    """True when the resolved project is projects/empiria/mnemosyne.

    Module-level for testability; consumed by the Components check category in
    `_build_checks`.
    """
    rel = lib_vault.resolve_vault_project(cwd, vault_path)
    return rel == "projects/empiria/mnemosyne"


def run(
    fix: bool = typer.Option(False, "--fix", help="Apply fixes with per-fix confirmation"),
    container: bool = typer.Option(
        False,
        "--container",
        help="Run in-container bootstrap checks (D-22) instead of host-codebase checks",
    ),
) -> None:
    """Validate project setup and report issues."""
    # Normalise typer sentinels for programmatic callers (tests invoke run()
    # directly without going through the CLI plumbing, leaving these as
    # typer.OptionInfo instances rather than the documented False default).
    if isinstance(fix, typer.models.OptionInfo):
        fix = False
    if isinstance(container, typer.models.OptionInfo):
        container = False

    cwd = Path.cwd()

    # Resolve vault path
    vault_path = lib_vault.resolve_vault_path()

    if container:
        # Container mode skips the git-repo requirement; _build_checks does
        # not consume git_dir when container=True.
        git_dir = cwd / ".git"
    else:
        try:
            git_dir = lib_git.get_git_dir(cwd)
        except Exception:
            git_dir = cwd / ".git"

    checks = _build_checks(cwd, vault_path, git_dir, container=container)

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
