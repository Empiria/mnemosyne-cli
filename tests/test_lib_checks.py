"""RED tests for mnemosyne_cli.lib.checks (D-22 individual check helpers).

Module lib/checks.py does not exist yet — Plan 33.1-03 implements it.
Lazy in-function imports keep `--collect-only` green during Wave 0.
"""

from __future__ import annotations

from pathlib import Path


def test_check_mnemosyne_on_path_passes_when_present(monkeypatch):
    from mnemosyne_cli.lib.checks import check_mnemosyne_on_path

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/mnemosyne" if name == "mnemosyne" else None,
    )
    result = check_mnemosyne_on_path()
    assert result.ok is True


def test_check_mnemosyne_on_path_fails_when_missing(monkeypatch):
    from mnemosyne_cli.lib.checks import check_mnemosyne_on_path

    monkeypatch.setattr("shutil.which", lambda name: None)
    result = check_mnemosyne_on_path()
    assert result.ok is False
    assert "mnemosyne" in result.message.lower()


def test_check_gsd_tools_on_path_passes_when_present(monkeypatch):
    from mnemosyne_cli.lib.checks import check_gsd_tools_on_path

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/share/npm-global/bin/gsd-tools"
        if name == "gsd-tools"
        else None,
    )
    result = check_gsd_tools_on_path()
    assert result.ok is True


def test_check_gsd_tools_on_path_fails_when_missing(monkeypatch):
    from mnemosyne_cli.lib.checks import check_gsd_tools_on_path

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert check_gsd_tools_on_path().ok is False


def test_check_user_skills_populated_passes_with_resolving_symlink(tmp_path, monkeypatch):
    from mnemosyne_cli.lib.checks import check_user_skills_populated

    vault = tmp_path / "vault"
    skill_target = vault / "agents" / "skills" / "clio"
    skill_target.mkdir(parents=True)
    (skill_target / "SKILL.md").write_text("# clio\n")

    fake_home = tmp_path / "home"
    skills_dir = fake_home / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "clio").symlink_to(skill_target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = check_user_skills_populated(vault)
    assert result.ok is True


def test_check_user_skills_populated_fails_when_dir_missing(tmp_path, monkeypatch):
    from mnemosyne_cli.lib.checks import check_user_skills_populated

    vault = tmp_path / "vault"
    vault.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert check_user_skills_populated(vault).ok is False


def test_check_workspace_planning_passes_with_valid_symlink(tmp_path):
    from mnemosyne_cli.lib.checks import check_workspace_planning

    vault = tmp_path / "vault"
    project = vault / "projects" / "org" / "proj"
    (project / "gsd-planning").mkdir(parents=True)
    target = tmp_path / "workspace"
    target.mkdir()
    (target / ".planning").symlink_to(project / "gsd-planning")
    result = check_workspace_planning(target, vault)
    assert result.ok is True


def test_check_workspace_planning_fails_for_non_symlink(tmp_path):
    from mnemosyne_cli.lib.checks import check_workspace_planning

    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    target = tmp_path / "workspace"
    target.mkdir()
    (target / ".planning").mkdir()  # real dir, not a symlink
    assert check_workspace_planning(target, vault).ok is False


def test_check_required_env_vars_passes_when_both_set(monkeypatch):
    from mnemosyne_cli.lib.checks import check_required_env_vars

    monkeypatch.setenv("MNEMOSYNE_WORKSPACE", "/workspace")
    monkeypatch.setenv("MNEMOSYNE_PROJECT", "projects/org/proj")
    assert check_required_env_vars().ok is True


def test_check_required_env_vars_fails_when_either_missing(monkeypatch):
    from mnemosyne_cli.lib.checks import check_required_env_vars

    monkeypatch.delenv("MNEMOSYNE_WORKSPACE", raising=False)
    monkeypatch.setenv("MNEMOSYNE_PROJECT", "projects/org/proj")
    assert check_required_env_vars().ok is False


def test_check_init_status_file_passes_on_zero(tmp_path, monkeypatch):
    from mnemosyne_cli.lib.checks import check_init_status_file

    status_file = tmp_path / "mnemosyne-init.status"
    status_file.write_text("0\n")
    monkeypatch.setattr("mnemosyne_cli.lib.checks._INIT_STATUS_PATH", status_file)
    assert check_init_status_file().ok is True


def test_check_init_status_file_fails_on_nonzero(tmp_path, monkeypatch):
    from mnemosyne_cli.lib.checks import check_init_status_file

    status_file = tmp_path / "mnemosyne-init.status"
    status_file.write_text("2\n")
    monkeypatch.setattr("mnemosyne_cli.lib.checks._INIT_STATUS_PATH", status_file)
    result = check_init_status_file()
    assert result.ok is False
    assert "2" in result.message


def test_check_init_status_file_fails_when_missing(tmp_path, monkeypatch):
    from mnemosyne_cli.lib.checks import check_init_status_file

    monkeypatch.setattr(
        "mnemosyne_cli.lib.checks._INIT_STATUS_PATH", tmp_path / "absent"
    )
    assert check_init_status_file().ok is False


def test_run_container_checks_returns_six_results(tmp_path):
    from mnemosyne_cli.lib.checks import run_container_checks

    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "workspace"
    target.mkdir()
    results = run_container_checks(target, vault)
    # D-22 acceptance: 6 checks
    assert len(results) == 6
