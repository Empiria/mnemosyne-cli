"""Unit tests for lib/setup.py::setup_worktree_symlinks — Phase 33 Plan 01."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mnemosyne_cli.lib.setup import setup_worktree_symlinks


# ---------------------------------------------------------------------------
# Helpers (mirrored from tests/test_init.py)
# ---------------------------------------------------------------------------


def _make_skill_dir(vault: Path, name: str) -> Path:
    skill_dir = vault / "agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    return skill_dir


def _write_skills_yaml(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["skills:\n"] + [f"  - {name}\n" for name in names]
    path.write_text("".join(lines), encoding="utf-8")


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(vault_path, target, vault_project_path) minimal setup."""
    vault_path = tmp_path / "vault"
    target = tmp_path / "worktree"
    target.mkdir(parents=True)

    # Vault project
    vault_project_path = vault_path / "projects" / "org" / "proj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)

    # AGENTS.md in the project (enables AGENTS/CLAUDE symlinks + tech-stack)
    (vault_project_path / "AGENTS.md").write_text(
        "# Project AGENTS\n\nTech stack: python\n", encoding="utf-8"
    )

    # Minimal claude-config/
    claude_config = vault_project_path / "claude-config"
    claude_config.mkdir()
    (claude_config / "settings.json").write_text("{}", encoding="utf-8")

    # One skill
    _make_skill_dir(vault_path, "mnemosyne-plan")
    _write_skills_yaml(claude_config / "skills.yaml", ["mnemosyne-plan"])

    return vault_path, target, vault_project_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_creates_planning_symlink(fixture: tuple[Path, Path, Path]) -> None:
    vault_path, target, vault_project_path = fixture
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    link = target / ".planning"
    assert link.is_symlink()
    assert link.resolve() == (vault_project_path / "gsd-planning").resolve()


def test_creates_agents_and_claude_symlinks(fixture: tuple[Path, Path, Path]) -> None:
    vault_path, target, vault_project_path = fixture
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    agents_link = target / "AGENTS.md"
    claude_link = target / "CLAUDE.md"
    assert agents_link.is_symlink()
    assert agents_link.resolve() == (vault_project_path / "AGENTS.md").resolve()
    assert claude_link.is_symlink()
    # CLAUDE.md is a *relative* symlink to AGENTS.md (readable via readlink)
    assert os.readlink(claude_link) == "AGENTS.md"


def test_omits_agents_when_vault_project_has_no_agents_md(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    target = tmp_path / "worktree"
    target.mkdir()
    vault_project_path = vault_path / "projects" / "org" / "proj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)
    # No AGENTS.md in the vault project
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    assert not (target / "AGENTS.md").exists()
    assert not (target / "CLAUDE.md").exists()
    # But .planning still created
    assert (target / ".planning").is_symlink()


def test_creates_settings_json_when_present(fixture: tuple[Path, Path, Path]) -> None:
    vault_path, target, vault_project_path = fixture
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    link = target / ".claude" / "settings.json"
    assert link.is_symlink()
    assert link.resolve() == (
        vault_project_path / "claude-config" / "settings.json"
    ).resolve()


def test_creates_skill_directory_symlinks(fixture: tuple[Path, Path, Path]) -> None:
    vault_path, target, vault_project_path = fixture
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    skill = target / ".claude" / "skills" / "mnemosyne-plan"
    assert skill.is_symlink()
    assert skill.resolve() == (
        vault_path / "agents" / "skills" / "mnemosyne-plan"
    ).resolve()


def test_is_idempotent_on_repeat_invocation(fixture: tuple[Path, Path, Path]) -> None:
    """Second call must not raise and must leave the same final state."""
    vault_path, target, vault_project_path = fixture
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    # All canonical symlinks still present
    assert (target / ".planning").is_symlink()
    assert (target / "AGENTS.md").is_symlink()
    assert (target / "CLAUDE.md").is_symlink()


def test_overwrites_regular_file_at_claude_md_path(fixture: tuple[Path, Path, Path]) -> None:
    """Simulates a fresh worktree: CLAUDE.md is a tracked file; must be replaced."""
    vault_path, target, vault_project_path = fixture
    (target / "CLAUDE.md").write_text("tracked content", encoding="utf-8")
    setup_worktree_symlinks(target, vault_path, vault_project_path)
    assert (target / "CLAUDE.md").is_symlink()


def test_rules_embed_targets_produce_per_file_symlinks(tmp_path: Path) -> None:
    """If claude-config/rules/ exists with embed notes, per-file symlinks are created."""
    vault_path = tmp_path / "vault"
    target = tmp_path / "worktree"
    target.mkdir()
    vault_project_path = vault_path / "projects" / "org" / "proj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)
    (vault_project_path / "AGENTS.md").write_text("# x\n", encoding="utf-8")

    # Embed-note directory: each file is a stub embedding a technology note
    claude_config = vault_project_path / "claude-config"
    rules = claude_config / "rules"
    rules.mkdir(parents=True)
    (rules / "python-style.md").write_text(
        "![[technologies/python/standard/code-style.md]]\n", encoding="utf-8"
    )
    # Ensure the target the embed points to exists
    (vault_path / "technologies" / "python" / "standard").mkdir(parents=True)
    (vault_path / "technologies" / "python" / "standard" / "code-style.md").write_text(
        "# code style\n", encoding="utf-8"
    )

    setup_worktree_symlinks(target, vault_path, vault_project_path)
    link = target / ".claude" / "rules" / "python-style.md"
    assert link.is_symlink()
    assert link.resolve() == (
        vault_path / "technologies" / "python" / "standard" / "code-style.md"
    ).resolve()


def test_tech_stack_rules_are_wired_from_agents_md(tmp_path: Path) -> None:
    """When AGENTS.md declares tech stack, per-tech rule symlinks are created."""
    vault_path = tmp_path / "vault"
    target = tmp_path / "worktree"
    target.mkdir()
    vault_project_path = vault_path / "projects" / "org" / "proj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)
    (vault_project_path / "AGENTS.md").write_text(
        "# Project\n\nTech stack: python\n", encoding="utf-8"
    )

    # Vault technology directory with index.md
    tech_dir = vault_path / "technologies" / "python"
    tech_dir.mkdir(parents=True)
    index = tech_dir / "index.md"
    index.write_text("# Python\n", encoding="utf-8")

    setup_worktree_symlinks(target, vault_path, vault_project_path)
    link = target / ".claude" / "rules" / "python.md"
    assert link.is_symlink()
    assert link.resolve() == index.resolve()


def test_source_checkout_none_skips_assume_unchanged(
    fixture: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When source_checkout is None, _replicate_assume_unchanged is not called.

    Patches _replicate_assume_unchanged and asserts it was not invoked when
    source_checkout is omitted.
    """
    vault_path, target, vault_project_path = fixture
    from mnemosyne_cli.lib import setup as setup_mod
    called: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        setup_mod, "_replicate_assume_unchanged",
        lambda a, b: called.append((a, b)),
    )
    setup_worktree_symlinks(
        target, vault_path, vault_project_path, source_checkout=None
    )
    assert called == []


def test_source_checkout_given_calls_assume_unchanged(
    fixture: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path, target, vault_project_path = fixture
    main_checkout = target.parent / "main"
    main_checkout.mkdir()
    from mnemosyne_cli.lib import setup as setup_mod
    called: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        setup_mod, "_replicate_assume_unchanged",
        lambda a, b: called.append((a, b)),
    )
    setup_worktree_symlinks(
        target, vault_path, vault_project_path, source_checkout=main_checkout
    )
    assert called == [(main_checkout, target)]
