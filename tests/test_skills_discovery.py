"""RED tests for mnemosyne_cli.lib.skills.discover_vault_skills (SBR-04 D-10).

Module lib/skills.py does not exist yet — these tests are scaffolded in Wave 0
(Plan 33.1-00) and fail until Plan 33.1-03 implements the helper.

Fixture layout (see tests/fixtures/vault_skills/agents/skills/):

    clio/SKILL.md                                       (flat skill)
    mnemosyne-search/{SKILL.md, README.md}              (flat skill with supplementary file)
    obsidian-skills/                                    (collection wrapper, NO top-level SKILL.md)
        README.md
        skills/
            defuddle/SKILL.md                           (nested)
            json-canvas/SKILL.md                        (nested)

The helper is called as `discover_vault_skills(vault_path)` and walks
`vault_path / "agents" / "skills" /`. Returns sorted list of (name, dir) tuples.
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "vault_skills"


def test_discover_returns_flat_skills():
    """Top-level dirs with SKILL.md become (name, dir) tuples."""
    from mnemosyne_cli.lib.skills import discover_vault_skills

    skills = discover_vault_skills(FIXTURE_ROOT)
    names = [n for n, _ in skills]
    assert "clio" in names
    assert "mnemosyne-search" in names


def test_discover_walks_nested_collection_one_level():
    """obsidian-skills/skills/<name>/SKILL.md yields each <name>, NOT the wrapper."""
    from mnemosyne_cli.lib.skills import discover_vault_skills

    skills = discover_vault_skills(FIXTURE_ROOT)
    names = {n for n, _ in skills}
    assert "defuddle" in names
    assert "json-canvas" in names
    # The wrapper itself must NOT appear as a discovered skill
    assert "obsidian-skills" not in names


def test_discover_returns_correct_skill_dir_for_nested():
    """Discovered (name, dir) pair points at the inner SKILL.md-bearing dir."""
    from mnemosyne_cli.lib.skills import discover_vault_skills

    skills = dict(discover_vault_skills(FIXTURE_ROOT))
    defuddle_dir = skills["defuddle"]
    assert defuddle_dir.is_dir()
    assert (defuddle_dir / "SKILL.md").exists()
    # The dir is the INNER one, not the obsidian-skills/ wrapper
    assert defuddle_dir.name == "defuddle"
    assert defuddle_dir.parent.name == "skills"


def test_discover_returns_sorted_for_determinism():
    """Output order is sorted by skill name for predictable symlink creation."""
    from mnemosyne_cli.lib.skills import discover_vault_skills

    skills = discover_vault_skills(FIXTURE_ROOT)
    names = [n for n, _ in skills]
    assert names == sorted(names)


def test_discover_returns_empty_for_missing_agents_skills_dir(tmp_path):
    """Vault with no agents/skills/ at all returns []."""
    from mnemosyne_cli.lib.skills import discover_vault_skills

    empty_vault = tmp_path / "empty_vault"
    empty_vault.mkdir()
    assert discover_vault_skills(empty_vault) == []
