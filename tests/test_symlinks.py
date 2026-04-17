"""Unit tests for mnemosyne_cli.lib.symlinks skill primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.lib.symlinks import (
    CheckResult,
    check_skill_symlink,
    check_symlink,
    create_skill_symlink,
    expand_skill_names,
    parse_skills_list,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skills_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


# ---------------------------------------------------------------------------
# parse_skills_list tests
# ---------------------------------------------------------------------------


def test_parse_skills_list_valid(tmp_path: Path) -> None:
    """A well-formed skills.yaml returns the correct list of skill names."""
    yaml_path = tmp_path / "skills.yaml"
    _write_skills_yaml(
        yaml_path,
        "# comment\nskills:\n  - mnemosyne-plan\n  - mnemosyne-execute\n  - obsidian-skills\n",
    )
    result = parse_skills_list(yaml_path)
    assert result == ["mnemosyne-plan", "mnemosyne-execute", "obsidian-skills"]


def test_parse_skills_list_missing_file(tmp_path: Path) -> None:
    """Returns an empty list when the file does not exist."""
    result = parse_skills_list(tmp_path / "nonexistent.yaml")
    assert result == []


def test_parse_skills_list_missing_key(tmp_path: Path) -> None:
    """Raises ValueError when the skills: key is absent."""
    yaml_path = tmp_path / "skills.yaml"
    _write_skills_yaml(yaml_path, "other_key:\n  - something\n")
    with pytest.raises(ValueError, match="missing required 'skills:'"):
        parse_skills_list(yaml_path)


def test_parse_skills_list_empty_list(tmp_path: Path) -> None:
    """Returns an empty list when skills: [] is present."""
    yaml_path = tmp_path / "skills.yaml"
    _write_skills_yaml(yaml_path, "skills: []\n")
    result = parse_skills_list(yaml_path)
    assert result == []


def test_parse_skills_list_non_string_entry(tmp_path: Path) -> None:
    """Raises ValueError when a list entry is a number, not a string."""
    yaml_path = tmp_path / "skills.yaml"
    _write_skills_yaml(yaml_path, "skills:\n  - mnemosyne-plan\n  - 42\n")
    with pytest.raises(ValueError, match="number"):
        parse_skills_list(yaml_path)


# ---------------------------------------------------------------------------
# expand_skill_names tests
# ---------------------------------------------------------------------------


def test_expand_flat_skill(tmp_path: Path) -> None:
    """A flat skill (has SKILL.md, no skills/ subdir) passes through unchanged."""
    vault = tmp_path / "vault"
    _make_skill_dir(vault, "prepare-commits")
    result = expand_skill_names(["prepare-commits"], vault)
    assert result == ["prepare-commits"]


def test_expand_bundle(tmp_path: Path) -> None:
    """A bundle expands to its sub-skill names; the bundle name is not in output."""
    vault = tmp_path / "vault"
    _make_bundle_dir(vault, "obsidian-skills", ["sub-a", "sub-b"])
    result = expand_skill_names(["obsidian-skills"], vault)
    assert result == ["sub-a", "sub-b"]


def test_expand_bundle_no_skill_md(tmp_path: Path) -> None:
    """A skills/ subdir with no SKILL.md children is treated as a flat skill."""
    vault = tmp_path / "vault"
    bundle_dir = vault / "agents" / "skills" / "pseudo-bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    # Create skills/ subdir but children have no SKILL.md
    (bundle_dir / "skills" / "orphan").mkdir(parents=True, exist_ok=True)
    result = expand_skill_names(["pseudo-bundle"], vault)
    assert result == ["pseudo-bundle"]


def test_expand_collision(tmp_path: Path) -> None:
    """Two entries producing the same resolved name raise ValueError."""
    vault = tmp_path / "vault"
    # bundle-a expands to ["shared-skill"]
    _make_bundle_dir(vault, "bundle-a", ["shared-skill"])
    # bundle-b also expands to ["shared-skill"]
    _make_bundle_dir(vault, "bundle-b", ["shared-skill"])
    with pytest.raises(ValueError, match="collision"):
        expand_skill_names(["bundle-a", "bundle-b"], vault)


def test_expand_nonexistent_skill_dir(tmp_path: Path) -> None:
    """A skill name whose vault dir doesn't exist passes through unchanged."""
    vault = tmp_path / "vault"
    (vault / "agents" / "skills").mkdir(parents=True, exist_ok=True)
    result = expand_skill_names(["ghost-skill"], vault)
    assert result == ["ghost-skill"]


# ---------------------------------------------------------------------------
# create_skill_symlink / check_skill_symlink tests
# ---------------------------------------------------------------------------


def test_create_skill_symlink_creates_link(tmp_path: Path) -> None:
    """create_skill_symlink creates .claude/skills/<name> as a directory symlink."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    vault = tmp_path / "vault"
    skill_dir = _make_skill_dir(vault, "mnemosyne-plan")

    create_skill_symlink(cwd, "mnemosyne-plan", vault)

    link = cwd / ".claude" / "skills" / "mnemosyne-plan"
    assert link.is_symlink(), "expected a symlink"
    assert link.resolve() == skill_dir.resolve()


def test_create_skill_symlink_idempotent(tmp_path: Path) -> None:
    """Calling create_skill_symlink twice does not raise."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    vault = tmp_path / "vault"
    _make_skill_dir(vault, "mnemosyne-plan")

    create_skill_symlink(cwd, "mnemosyne-plan", vault)
    # Second call must not raise
    create_skill_symlink(cwd, "mnemosyne-plan", vault)

    link = cwd / ".claude" / "skills" / "mnemosyne-plan"
    assert link.is_symlink()


def test_create_skill_symlink_real_dir_raises(tmp_path: Path) -> None:
    """A real (non-symlink) directory at the destination raises FileExistsError."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    vault = tmp_path / "vault"
    _make_skill_dir(vault, "mnemosyne-plan")

    # Pre-create a real directory at the symlink destination
    real_dir = cwd / ".claude" / "skills" / "mnemosyne-plan"
    real_dir.mkdir(parents=True)

    with pytest.raises(FileExistsError):
        create_skill_symlink(cwd, "mnemosyne-plan", vault)


def test_check_skill_symlink_ok(tmp_path: Path) -> None:
    """check_skill_symlink returns ok=True after a valid symlink is created."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    vault = tmp_path / "vault"
    _make_skill_dir(vault, "mnemosyne-plan")

    create_skill_symlink(cwd, "mnemosyne-plan", vault)
    result = check_skill_symlink(cwd, "mnemosyne-plan", vault)

    assert isinstance(result, CheckResult)
    assert result.ok is True


def test_check_skill_symlink_missing(tmp_path: Path) -> None:
    """check_skill_symlink returns ok=False when the symlink does not exist."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    vault = tmp_path / "vault"

    result = check_skill_symlink(cwd, "mnemosyne-plan", vault)

    assert isinstance(result, CheckResult)
    assert result.ok is False
