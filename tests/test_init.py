"""Integration tests for mnemosyne init — skills.yaml → .claude/skills/ directory symlinks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mnemosyne_cli.commands import init


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill_dir(vault: Path, name: str) -> Path:
    """Create a flat skill dir with a SKILL.md."""
    skill_dir = vault / "agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    return skill_dir


def _make_bundle_dir(vault: Path, bundle_name: str, sub_names: list[str]) -> Path:
    """Create a bundle skill dir with sub-skill dirs each containing SKILL.md."""
    bundle_dir = vault / "agents" / "skills" / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "SKILL.md").write_text(f"# {bundle_name}", encoding="utf-8")
    for sub in sub_names:
        sub_dir = bundle_dir / "skills" / sub
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "SKILL.md").write_text(f"# {sub}", encoding="utf-8")
    return bundle_dir


def _write_skills_yaml(path: Path, names: list[str]) -> None:
    """Write a minimal skills.yaml to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["skills:\n"] + [f"  - {name}\n" for name in names]
    path.write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def setup_env(tmp_path: Path):
    """Build a minimal vault + project filesystem under tmp_path.

    Returns (vault_path, project_path, vault_project_path).
    """
    vault_path = tmp_path / "vault"
    project_path = tmp_path / "project"

    # Vault skill dirs
    _make_skill_dir(vault_path, "mnemosyne-plan")
    _make_skill_dir(vault_path, "mnemosyne-execute")
    _make_bundle_dir(vault_path, "obsidian-skills", ["defuddle", "obsidian-cli"])

    # Project filesystem (fake cwd)
    project_path.mkdir(parents=True)
    (project_path / ".git" / "info").mkdir(parents=True)

    # Vault project directory with gsd-planning and claude-config
    vault_project_path = vault_path / "projects" / "testorg" / "testproj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)
    claude_config = vault_project_path / "claude-config"
    claude_config.mkdir(parents=True)

    _write_skills_yaml(
        claude_config / "skills.yaml",
        ["mnemosyne-plan", "mnemosyne-execute", "obsidian-skills"],
    )

    return vault_path, project_path, vault_project_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_init_creates_skill_directory_symlinks(tmp_path: Path) -> None:
    """Fresh project init produces only .claude/skills/<name>/ directory symlinks.

    Asserts:
    - .claude/skills/mnemosyne-plan is a symlink
    - .claude/skills/mnemosyne-execute is a symlink
    - obsidian-skills bundle is NOT present as a symlink (it was expanded)
    - .claude/skills/defuddle is a symlink (bundle expansion)
    - .claude/skills/obsidian-cli is a symlink (bundle expansion)
    - .claude/commands/ directory does not exist at all
    - No .claude/commands/*.md file symlinks exist
    """
    vault_path, project_path, vault_project_path = setup_env(tmp_path)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault_path),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=project_path / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=project_path),
    ):
        init.run(project="projects/testorg/testproj")

    skills_dir = project_path / ".claude" / "skills"

    # Flat skills installed as directory symlinks
    assert (skills_dir / "mnemosyne-plan").is_symlink(), "mnemosyne-plan should be a symlink"
    assert (skills_dir / "mnemosyne-execute").is_symlink(), "mnemosyne-execute should be a symlink"

    # Bundle name itself must NOT be present
    assert not (skills_dir / "obsidian-skills").exists(), "obsidian-skills bundle should be expanded, not linked directly"

    # Bundle sub-skills installed as directory symlinks
    assert (skills_dir / "defuddle").is_symlink(), "defuddle (bundle sub-skill) should be a symlink"
    assert (skills_dir / "obsidian-cli").is_symlink(), "obsidian-cli (bundle sub-skill) should be a symlink"

    # Legacy .claude/commands/ must not exist at all
    commands_dir = project_path / ".claude" / "commands"
    assert not commands_dir.exists(), ".claude/commands/ must not be created by init"

    # No .md file symlinks under .claude/ (verify no file-symlink pattern)
    for item in (project_path / ".claude").rglob("*.md"):
        assert not item.is_symlink(), f"No .md file symlinks expected, found: {item}"


def test_init_no_skills_yaml_skips_gracefully(tmp_path: Path) -> None:
    """Init with no skills.yaml skips .claude/skills/ without error.

    Asserts init completes and no .claude/skills/ directory is created.
    """
    vault_path, project_path, vault_project_path = setup_env(tmp_path)

    # Remove the skills.yaml that setup_env created
    skills_yaml = vault_project_path / "claude-config" / "skills.yaml"
    skills_yaml.unlink()

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault_path),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=project_path / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=project_path),
    ):
        # Should not raise
        init.run(project="projects/testorg/testproj")

    skills_dir = project_path / ".claude" / "skills"
    assert not skills_dir.exists(), ".claude/skills/ must not be created when no skills.yaml"
