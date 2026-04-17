"""Integration tests for mnemosyne doctor — Skills checks and legacy migration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from mnemosyne_cli.commands import doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill_dir(vault: Path, name: str) -> Path:
    """Create a flat skill dir with a SKILL.md."""
    skill_dir = vault / "agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    return skill_dir


def _write_skills_yaml(path: Path, names: list[str]) -> None:
    """Write a minimal skills.yaml to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["skills:\n"] + [f"  - {name}\n" for name in names]
    path.write_text("".join(lines), encoding="utf-8")


def _write_embed_note(path: Path, vault_rel_target: str) -> None:
    """Write a legacy embed note (.md) at *path* pointing to *vault_rel_target*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"![[{vault_rel_target}]]\n", encoding="utf-8")


def _populate_git_exclude(git_dir: Path, entries: list[str]) -> None:
    """Pre-populate .git/info/exclude with the given entries."""
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join(entries) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared environment factory
# ---------------------------------------------------------------------------


def _setup_env(tmp_path: Path):
    """Build a minimal vault + project under tmp_path.

    Returns (vault_path, project_path, vault_project_path).

    The project has all required symlinks and exclusions already wired so that
    non-Skills checks pass, letting tests assert on Skills checks in isolation.
    """
    vault_path = tmp_path / "vault"
    project_path = tmp_path / "project"

    # Vault skill dirs
    _make_skill_dir(vault_path, "mnemosyne-plan")
    _make_skill_dir(vault_path, "mnemosyne-search")

    # Project filesystem
    project_path.mkdir(parents=True)
    git_dir = project_path / ".git"
    (git_dir / "info").mkdir(parents=True)

    # Vault project dir
    vault_project_path = vault_path / "projects" / "testorg" / "testproj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)
    (vault_project_path / "claude-config").mkdir(parents=True)

    # AGENTS.md in vault project (required by Symlinks checks)
    (vault_project_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    # Vault git dir (needed for vault-side commit in migration fix)
    (vault_path / ".git" / "info").mkdir(parents=True)

    return vault_path, project_path, vault_project_path


def _wire_full_env(
    project_path: Path,
    vault_path: Path,
    vault_project_path: Path,
    extra_excludes: list[str] | None = None,
) -> None:
    """Wire all symlinks and git exclusions so non-Skills checks pass.

    Creates: .planning, AGENTS.md, CLAUDE.md symlinks.
    Populates .git/info/exclude with the standard entries.
    """
    git_dir = project_path / ".git"

    # .planning symlink
    planning = project_path / ".planning"
    if not planning.exists() and not planning.is_symlink():
        planning.symlink_to(vault_project_path / "gsd-planning")

    # AGENTS.md symlink
    agents_link = project_path / "AGENTS.md"
    if not agents_link.exists() and not agents_link.is_symlink():
        agents_link.symlink_to(vault_project_path / "AGENTS.md")

    # CLAUDE.md -> AGENTS.md (local relative symlink)
    claude_link = project_path / "CLAUDE.md"
    if not claude_link.exists() and not claude_link.is_symlink():
        claude_link.symlink_to(Path("AGENTS.md"))

    # .envrc (so envrc check passes)
    envrc = project_path / ".envrc"
    if not envrc.exists():
        envrc.write_text(f'export MNEMOSYNE_VAULT="{vault_path}"\n', encoding="utf-8")

    # Standard git exclusions
    excludes = [".planning", "AGENTS.md", "CLAUDE.md", ".envrc", "worktrees"]
    if extra_excludes:
        excludes.extend(extra_excludes)
    _populate_git_exclude(git_dir, excludes)


def _fake_subprocess_stub(args, **kwargs):
    """Stub subprocess.run for doctor — returns success for all calls.

    - git config merge.*.driver → returns expected driver string
    - git -C <path> add/commit  → no-op success (used in migration fix)
    - everything else            → success with empty output
    """
    if not isinstance(args, (list, tuple)):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    args_list = list(args)

    # Merge driver checks
    if args_list[:2] == ["git", "config"] and len(args_list) == 3:
        key = args_list[2]
        drivers = {
            "merge.gsd-state.driver": "mnemosyne merge-driver state %O %A %B",
            "merge.gsd-roadmap.driver": "mnemosyne merge-driver roadmap %O %A %B",
        }
        stdout = drivers.get(key, "")
        return subprocess.CompletedProcess(args, 0 if stdout else 1, stdout=stdout, stderr="")

    # git -C <path> operations (branch list, add, commit etc.)
    if args_list[0] == "git" and "-C" in args_list:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    # git branch --list
    if args_list[:3] == ["git", "branch", "--list"]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _run_doctor(
    project_path: Path,
    vault_path: Path,
    fix: bool = False,
    confirm: bool = True,
    subprocess_side_effect=None,
) -> int:
    """Run doctor.run() with mocked external dependencies.

    Returns the exit code (0 = all pass, 1 = failures remain).
    Patches: resolve_vault_path, get_git_dir, list_worktrees, subprocess.run,
    Path.cwd, shutil.which (→ None to skip Freshness), typer.confirm, typer.Exit.
    """
    git_dir = project_path / ".git"
    exit_code = 0

    def _fake_exit(code: int = 0) -> None:
        nonlocal exit_code
        exit_code = int(code) if code is not None else 0
        raise SystemExit(code)

    sub_effect = subprocess_side_effect or _fake_subprocess_stub

    with (
        patch("mnemosyne_cli.commands.doctor.lib_vault.resolve_vault_path", return_value=vault_path),
        patch("mnemosyne_cli.commands.doctor.lib_git.get_git_dir", return_value=git_dir),
        patch("mnemosyne_cli.commands.doctor.lib_git.list_worktrees", return_value=[]),
        patch("mnemosyne_cli.commands.doctor.subprocess.run", side_effect=sub_effect),
        patch("mnemosyne_cli.commands.doctor.Path.cwd", return_value=project_path),
        # Stub shutil.which → None so Freshness checks skip (podman/skopeo/qmd absent)
        patch("shutil.which", return_value=None),
        patch("typer.confirm", return_value=confirm),
        patch("typer.Exit", side_effect=_fake_exit),
    ):
        try:
            doctor.run(fix=fix)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0

    return exit_code


# ---------------------------------------------------------------------------
# Test 1: new layout reports green
# ---------------------------------------------------------------------------


def test_doctor_new_layout_reports_green(tmp_path: Path) -> None:
    """doctor on a project with skills.yaml + directory symlinks reports all checks green."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Wire skills.yaml in vault
    _write_skills_yaml(
        vault_project_path / "claude-config" / "skills.yaml",
        ["mnemosyne-plan", "mnemosyne-search"],
    )

    # Create directory symlinks as init would
    skills_dir = project_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "mnemosyne-plan").symlink_to(vault_path / "agents" / "skills" / "mnemosyne-plan")
    (skills_dir / "mnemosyne-search").symlink_to(vault_path / "agents" / "skills" / "mnemosyne-search")

    # Wire all other checks so they pass
    _wire_full_env(
        project_path, vault_path, vault_project_path,
        extra_excludes=[".claude/skills"],
    )

    exit_code = _run_doctor(project_path, vault_path)

    assert exit_code == 0, "Expected all checks to pass (exit 0) on new-layout project"


# ---------------------------------------------------------------------------
# Test 2: legacy layout reports failure
# ---------------------------------------------------------------------------


def test_doctor_legacy_layout_reports_fail(tmp_path: Path) -> None:
    """doctor on a legacy project reports failure in the Skills category."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Wire .planning only (other checks can fail — we only need Skills to fail)
    planning = project_path / ".planning"
    planning.symlink_to(vault_project_path / "gsd-planning")

    # Legacy layout: embed note in vault
    legacy_cmd_dir = vault_project_path / "claude-config" / "commands"
    _write_embed_note(
        legacy_cmd_dir / "mnemosyne-search.md",
        "agents/skills/mnemosyne-search/SKILL.md",
    )

    # .claude/commands/ with a .md file symlink in the project
    client_cmds = project_path / ".claude" / "commands"
    client_cmds.mkdir(parents=True, exist_ok=True)
    (client_cmds / "mnemosyne-search.md").symlink_to(
        vault_path / "agents" / "skills" / "mnemosyne-search" / "SKILL.md"
    )

    exit_code = _run_doctor(project_path, vault_path)

    assert exit_code != 0, "Expected non-zero exit code on legacy layout project"


# ---------------------------------------------------------------------------
# Test 3: --fix migrates legacy to new layout
# ---------------------------------------------------------------------------


def test_doctor_fix_migrates_legacy_to_new_layout(tmp_path: Path) -> None:
    """doctor --fix on a legacy project performs the 7-step migration cleanly."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Wire all non-Skills checks so they pass
    _wire_full_env(project_path, vault_path, vault_project_path)

    # Initialise a real git repo in the vault so the commit step works
    subprocess.run(["git", "init"], cwd=vault_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=vault_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=vault_path, check=True, capture_output=True,
    )

    # Create initial commit
    (vault_path / "README.md").write_text("vault\n")
    subprocess.run(["git", "add", "README.md"], cwd=vault_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=vault_path, check=True, capture_output=True,
    )

    # Set up legacy layout
    legacy_cmd_dir = vault_project_path / "claude-config" / "commands"
    _write_embed_note(
        legacy_cmd_dir / "mnemosyne-search.md",
        "agents/skills/mnemosyne-search/SKILL.md",
    )

    client_cmds = project_path / ".claude" / "commands"
    client_cmds.mkdir(parents=True, exist_ok=True)
    legacy_link = client_cmds / "mnemosyne-search.md"
    legacy_link.symlink_to(
        vault_path / "agents" / "skills" / "mnemosyne-search" / "SKILL.md"
    )

    # Stage the embed note in the vault git repo
    subprocess.run(["git", "add", "--all"], cwd=vault_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add legacy commands"],
        cwd=vault_path, check=True, capture_output=True,
    )

    git_dir = project_path / ".git"
    exit_code_fix = 0

    # Capture the real subprocess.run BEFORE the patch is applied so the stub
    # can call it without triggering infinite recursion.
    _real_subprocess_run = subprocess.run

    def _fake_exit_fix(code: int = 0) -> None:
        nonlocal exit_code_fix
        exit_code_fix = int(code) if code is not None else 0
        raise SystemExit(code)

    def _real_or_stub(args, **kwargs):
        """Allow vault git -C commands to run for real; stub everything else."""
        if not isinstance(args, (list, tuple)):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        args_list = list(args)

        # Let vault-side git operations run for real (git -C <vault> add/commit)
        if args_list[0] == "git" and "-C" in args_list:
            idx = args_list.index("-C")
            git_cwd = Path(args_list[idx + 1])
            if git_cwd.resolve() == vault_path.resolve():
                return _real_subprocess_run(
                    args,
                    capture_output=kwargs.get("capture_output", False),
                    check=kwargs.get("check", False),
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        # Merge driver checks
        if args_list[:2] == ["git", "config"] and len(args_list) == 3:
            key = args_list[2]
            drivers = {
                "merge.gsd-state.driver": "mnemosyne merge-driver state %O %A %B",
                "merge.gsd-roadmap.driver": "mnemosyne merge-driver roadmap %O %A %B",
            }
            stdout = drivers.get(key, "")
            return subprocess.CompletedProcess(args, 0 if stdout else 1, stdout=stdout, stderr="")

        if args_list[:3] == ["git", "branch", "--list"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with (
        patch("mnemosyne_cli.commands.doctor.lib_vault.resolve_vault_path", return_value=vault_path),
        patch("mnemosyne_cli.commands.doctor.lib_git.get_git_dir", return_value=git_dir),
        patch("mnemosyne_cli.commands.doctor.lib_git.list_worktrees", return_value=[]),
        patch("mnemosyne_cli.commands.doctor.subprocess.run", side_effect=_real_or_stub),
        patch("mnemosyne_cli.commands.doctor.Path.cwd", return_value=project_path),
        patch("shutil.which", return_value=None),
        patch("typer.confirm", return_value=True),
        patch("typer.Exit", side_effect=_fake_exit_fix),
    ):
        try:
            doctor.run(fix=True)
        except SystemExit:
            pass

    # Assert: skills.yaml created in vault
    skills_yaml = vault_project_path / "claude-config" / "skills.yaml"
    assert skills_yaml.exists(), "skills.yaml should have been created by migration"
    content = skills_yaml.read_text()
    assert "mnemosyne-search" in content, "skills.yaml should list mnemosyne-search"

    # Assert: new .claude/skills/mnemosyne-search directory symlink created
    new_skill_link = project_path / ".claude" / "skills" / "mnemosyne-search"
    assert new_skill_link.is_symlink(), ".claude/skills/mnemosyne-search should be a directory symlink"

    # Assert: legacy .claude/commands/mnemosyne-search.md removed
    assert not legacy_link.is_symlink(), "Legacy .claude/commands/mnemosyne-search.md symlink should be gone"

    # Assert: legacy claude-config/commands/ directory removed from vault
    assert not legacy_cmd_dir.exists(), "claude-config/commands/ should have been removed from vault"


# ---------------------------------------------------------------------------
# Test 4: already-migrated project reports green (success criterion 2)
# ---------------------------------------------------------------------------


def test_doctor_already_migrated_reports_green(tmp_path: Path) -> None:
    """doctor on a post-migration project reports green — success criterion 2.

    A second run after migration (new layout fully in place) exits 0 with no
    --fix needed.
    """
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Post-migration state: skills.yaml + directory symlinks
    _write_skills_yaml(
        vault_project_path / "claude-config" / "skills.yaml",
        ["mnemosyne-search"],
    )

    skills_dir = project_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "mnemosyne-search").symlink_to(
        vault_path / "agents" / "skills" / "mnemosyne-search"
    )

    # No legacy commands/ — migration is complete
    assert not (vault_project_path / "claude-config" / "commands").exists()
    assert not (project_path / ".claude" / "commands").exists()

    # Wire all other checks so they pass
    _wire_full_env(
        project_path, vault_path, vault_project_path,
        extra_excludes=[".claude/skills"],
    )

    exit_code = _run_doctor(project_path, vault_path)

    assert exit_code == 0, (
        "Expected exit 0 on already-migrated project (success criterion 2)"
    )
