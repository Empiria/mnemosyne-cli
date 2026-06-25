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


# ---------------------------------------------------------------------------
# Phase 54 Plan 03 — Vendored skills regression (Pitfall 3)
#
# After obsidian-skills moves from agents/skills/obsidian-skills/ to
# agents/vendored/obsidian-skills/ (D-13), and after anvil-agent-references
# lands at agents/vendored/anvil-agent-references/, discover_vault_skills must
# find skills in BOTH vendored subtrees.
#
# These tests are RED until Plan 54-05 extends the discover_vault_skills walk
# to include agents/vendored/ as a second root.
# ---------------------------------------------------------------------------


def test_discover_finds_vendored_obsidian_skills(tmp_path):
    """Skills under agents/vendored/obsidian-skills/skills/ are discovered (Pitfall 3 regression).

    After the D-13 submodule retirement, obsidian-skills moves from
    agents/skills/obsidian-skills/ to agents/vendored/obsidian-skills/.
    discover_vault_skills must still find the nested skills inside it.
    """
    from mnemosyne_cli.lib.skills import discover_vault_skills  # lazy import

    # Seed agents/vendored/obsidian-skills/skills/foo/SKILL.md
    skill_dir = tmp_path / "agents" / "vendored" / "obsidian-skills" / "skills" / "foo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# foo skill from obsidian-skills\n")

    skills = discover_vault_skills(tmp_path)
    names = {n for n, _ in skills}
    assert "foo" in names, (
        f"discover_vault_skills must find 'foo' from agents/vendored/obsidian-skills/skills/; "
        f"got: {names}"
    )


def test_discover_finds_vendored_anvil_agent_references_skills(tmp_path):
    """Skills under agents/vendored/anvil-agent-references/skills/ are discovered (Pitfall 3 regression).

    After vendoring, agents/vendored/anvil-agent-references/skills/* must be
    picked up by discover_vault_skills so they surface under ~/.claude/skills/*.
    """
    from mnemosyne_cli.lib.skills import discover_vault_skills  # lazy import

    # Seed agents/vendored/anvil-agent-references/skills/form-code/SKILL.md
    skill_dir = (
        tmp_path / "agents" / "vendored" / "anvil-agent-references" / "skills" / "form-code"
    )
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# form-code skill from anvil-agent-references\n")

    skills = discover_vault_skills(tmp_path)
    names = {n for n, _ in skills}
    assert "form-code" in names, (
        f"discover_vault_skills must find 'form-code' from "
        f"agents/vendored/anvil-agent-references/skills/; got: {names}"
    )


def test_discover_finds_both_vendored_skill_sets(tmp_path):
    """Both obsidian-skills and anvil-agent-references vendored skills appear (Pitfall 3).

    This is the critical regression test: both vendored skill namespaces must be
    discovered simultaneously, confirming that the extended walk covers BOTH entries
    under agents/vendored/ in a single discover_vault_skills(vault_path) call.
    """
    from mnemosyne_cli.lib.skills import discover_vault_skills  # lazy import

    # Seed agents/vendored/obsidian-skills/skills/defuddle/SKILL.md
    obsidian_skill = (
        tmp_path / "agents" / "vendored" / "obsidian-skills" / "skills" / "defuddle"
    )
    obsidian_skill.mkdir(parents=True, exist_ok=True)
    (obsidian_skill / "SKILL.md").write_text("# defuddle skill\n")

    # Seed agents/vendored/anvil-agent-references/skills/form-code/SKILL.md
    anvil_skill = (
        tmp_path / "agents" / "vendored" / "anvil-agent-references" / "skills" / "form-code"
    )
    anvil_skill.mkdir(parents=True, exist_ok=True)
    (anvil_skill / "SKILL.md").write_text("# form-code skill\n")

    skills = discover_vault_skills(tmp_path)
    names = {n for n, _ in skills}
    assert "defuddle" in names, (
        f"discover_vault_skills must find 'defuddle' from obsidian-skills; got: {names}"
    )
    assert "form-code" in names, (
        f"discover_vault_skills must find 'form-code' from anvil-agent-references; got: {names}"
    )
