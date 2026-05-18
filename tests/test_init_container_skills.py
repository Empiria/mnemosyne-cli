"""RED tests for vault-skill wiring (D-10) and init self-verify (D-23).

Plan 33.1-03 implements these. Tests are scaffolded RED in Wave 0 (Plan 33.1-00).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mnemosyne_cli.commands import init


def _make_vault_project(vault_root: Path, project_rel: str) -> Path:
    vpp = vault_root / project_rel
    (vpp / "gsd-planning").mkdir(parents=True)
    (vpp / "AGENTS.md").write_text("# x\n")
    (vpp / "claude-config").mkdir()
    return vpp


def _make_vault_skills(vault_root: Path) -> None:
    """Create the flat + nested fixture under vault/agents/skills/."""
    base = vault_root / "agents" / "skills"
    (base / "clio").mkdir(parents=True)
    (base / "clio" / "SKILL.md").write_text("# clio\n")
    (base / "obsidian-skills" / "skills" / "defuddle").mkdir(parents=True)
    (base / "obsidian-skills" / "skills" / "defuddle" / "SKILL.md").write_text(
        "# defuddle\n"
    )


def _make_git_target(tmp_path: Path) -> Path:
    target = tmp_path / "workspace"
    (target / ".git" / "info").mkdir(parents=True)
    return target


def test_vault_skills_symlinked_into_user_home(tmp_path, monkeypatch):
    """D-10: every SKILL.md-bearing dir under vault/agents/skills/ is symlinked
    into ~/.claude/skills/<name>/."""
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    _make_vault_skills(vault)
    target = _make_git_target(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=target)

    skills_home = fake_home / ".claude" / "skills"
    assert (skills_home / "clio").is_symlink()
    # Two-level discovery: nested skill also linked, at flat path
    assert (skills_home / "defuddle").is_symlink()
    # Wrapper NOT linked
    assert not (skills_home / "obsidian-skills").exists()


def test_vault_skill_symlink_points_inside_vault(tmp_path, monkeypatch):
    """SBR-04 threat: symlink target must resolve under vault/agents/skills/."""
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    _make_vault_skills(vault)
    target = _make_git_target(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=target)

    clio_link = fake_home / ".claude" / "skills" / "clio"
    resolved = clio_link.resolve(strict=True)
    assert vault in resolved.parents
    assert "agents/skills" in str(resolved)


def test_self_verify_summary_runs_at_end_of_container_init(tmp_path, monkeypatch, capsys):
    """D-23: init --container prints PASS/WARN/FAIL summary to stderr after wiring."""
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    _make_vault_skills(vault)
    target = _make_git_target(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=target)

    captured = capsys.readouterr()
    # Summary appears on stderr per D-23 spec
    assert "self-verify" in captured.err.lower() or "verify" in captured.err.lower()
    # Status keywords surface
    text = captured.err.lower() + captured.out.lower()
    assert any(token in text for token in ["pass", "fail", "warn"])


def test_self_verify_failure_is_non_fatal(tmp_path, monkeypatch):
    """D-23 + D-06: bootstrap failure must not kill the agent.

    Init returns normally even with failed checks."""
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    # No vault skills — check_user_skills_populated will fail
    target = _make_git_target(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        # Must NOT raise — D-23 self-verify failures are non-fatal
        init.run(project="projects/org/proj", container=True, target=target)
