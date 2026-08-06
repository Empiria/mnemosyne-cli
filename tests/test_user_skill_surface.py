"""Reconciling ~/.claude/skills/ against the vault's discoverable skills.

The host surface drifts in three ways and nothing used to notice: a skill added
to the vault never appears, a skill moved between agents/skills and
agents/vendored leaves the old symlink dangling, and a removed skill leaves an
entry behind. classify_user_skills names each case; sync_user_skills repairs
them without touching anything that is not ours.
"""

from __future__ import annotations

import os
from pathlib import Path

from mnemosyne_cli.lib.skills import classify_user_skills, sync_user_skills


def _make_skill(vault: Path, name: str, root: str = "skills") -> Path:
    skill_dir = vault / "agents" / root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return skill_dir


def _make_vendored_skill(vault: Path, collection: str, name: str) -> Path:
    skill_dir = vault / "agents" / "vendored" / collection / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return skill_dir


def _skills_home(home: Path) -> Path:
    d = home / ".claude" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_discovered_skill_with_no_entry_is_missing(tmp_path):
    vault = tmp_path / "vault"
    expected = _make_skill(vault, "voice-check")
    home = tmp_path / "home"
    _skills_home(home)

    report = classify_user_skills(vault, home=home)

    assert report["missing"] == [("voice-check", str(expected))]
    assert report["ok"] == []


def test_correct_symlink_is_ok(tmp_path):
    vault = tmp_path / "vault"
    expected = _make_skill(vault, "clio")
    home = tmp_path / "home"
    (_skills_home(home) / "clio").symlink_to(expected, target_is_directory=True)

    report = classify_user_skills(vault, home=home)

    assert report["ok"] == [("clio",)]
    assert report["missing"] == []
    assert report["repoint"] == []


def test_symlink_to_retired_root_is_repointed_not_removed(tmp_path):
    """D-13 moved the obsidian collection from agents/skills to agents/vendored.

    The old symlink dangles, but the skill still exists, so the fix is to
    repoint it rather than treat it as removed.
    """
    vault = tmp_path / "vault"
    expected = _make_vendored_skill(vault, "obsidian-skills", "defuddle")
    retired = vault / "agents" / "skills" / "obsidian-skills" / "skills" / "defuddle"
    home = tmp_path / "home"
    link = _skills_home(home) / "defuddle"
    link.symlink_to(retired, target_is_directory=True)
    assert not link.exists()  # dangling

    report = classify_user_skills(vault, home=home)
    assert report["repoint"] == [("defuddle", str(retired), str(expected))]
    assert report["stale"] == []

    sync_user_skills(vault, home=home)

    assert Path(os.readlink(link)) == expected
    assert link.exists()


def test_symlink_into_vault_for_removed_skill_is_stale(tmp_path):
    vault = tmp_path / "vault"
    _make_skill(vault, "clio")
    gone = vault / "agents" / "skills" / "retired-thing"
    home = tmp_path / "home"
    link = _skills_home(home) / "retired-thing"
    link.symlink_to(gone, target_is_directory=True)

    report = classify_user_skills(vault, home=home)
    assert report["stale"] == [("retired-thing", str(gone))]

    done = sync_user_skills(vault, home=home)
    assert done["removed"] == ["retired-thing"]
    assert not link.is_symlink()


def test_symlink_outside_the_vault_is_foreign_and_left_alone(tmp_path):
    """A hand-installed skill is not ours to remove, even under --fix."""
    vault = tmp_path / "vault"
    _make_skill(vault, "clio")
    elsewhere = tmp_path / "elsewhere" / "my-skill"
    elsewhere.mkdir(parents=True)
    home = tmp_path / "home"
    link = _skills_home(home) / "my-skill"
    link.symlink_to(elsewhere, target_is_directory=True)

    report = classify_user_skills(vault, home=home)
    assert report["foreign"] == [("my-skill", str(elsewhere))]
    assert report["stale"] == []

    sync_user_skills(vault, home=home)
    assert link.is_symlink()
    assert Path(os.readlink(link)) == elsewhere


def test_real_directory_is_never_touched(tmp_path):
    """~/.claude/skills also holds locally installed skills (the gsd-* tree)."""
    vault = tmp_path / "vault"
    _make_skill(vault, "gsd-progress")
    home = tmp_path / "home"
    local = _skills_home(home) / "gsd-progress"
    local.mkdir()
    (local / "SKILL.md").write_text("# local\n", encoding="utf-8")

    report = classify_user_skills(vault, home=home)
    assert report["missing"] == []

    done = sync_user_skills(vault, home=home)
    assert done["linked"] == []
    assert local.is_dir() and not local.is_symlink()
    assert (local / "SKILL.md").read_text(encoding="utf-8") == "# local\n"


def test_sync_links_missing_skills(tmp_path):
    vault = tmp_path / "vault"
    a = _make_skill(vault, "voice-check")
    b = _make_vendored_skill(vault, "anvil-agent-references", "anvil-yaml")
    home = tmp_path / "home"

    done = sync_user_skills(vault, home=home)

    assert sorted(done["linked"]) == ["anvil-yaml", "voice-check"]
    skills = _skills_home(home)
    assert Path(os.readlink(skills / "voice-check")) == a
    assert Path(os.readlink(skills / "anvil-yaml")) == b


def test_sync_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    _make_skill(vault, "clio")
    home = tmp_path / "home"

    first = sync_user_skills(vault, home=home)
    second = sync_user_skills(vault, home=home)

    assert first["linked"] == ["clio"]
    assert second == {"linked": [], "repointed": [], "removed": [], "skipped": []}
    assert classify_user_skills(vault, home=home)["ok"] == [("clio",)]


def test_missing_skills_dir_reports_everything_missing(tmp_path):
    vault = tmp_path / "vault"
    _make_skill(vault, "clio")
    home = tmp_path / "home"  # never created

    report = classify_user_skills(vault, home=home)
    assert [name for name, *_ in report["missing"]] == ["clio"]

    sync_user_skills(vault, home=home)
    assert (home / ".claude" / "skills" / "clio").is_symlink()
