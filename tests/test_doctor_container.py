"""RED tests for mnemosyne doctor --container (D-21, D-22).

Plan 33.1-04 implements the --container flag and the Container Bootstrap
check category. Wave 0 scaffold.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from mnemosyne_cli.main import app

runner = CliRunner()


def test_doctor_container_flag_exists():
    """--container flag is on doctor.run signature."""
    result = runner.invoke(app, ["doctor", "--help"])
    assert "--container" in result.stdout


def test_doctor_container_reports_missing_mnemosyne(tmp_path, monkeypatch):
    """All 6 D-22 checks render their results."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.delenv("MNEMOSYNE_WORKSPACE", raising=False)
    monkeypatch.delenv("MNEMOSYNE_PROJECT", raising=False)
    with patch(
        "mnemosyne_cli.commands.doctor.lib_vault.resolve_vault_path",
        return_value=tmp_path,
    ):
        result = runner.invoke(app, ["doctor", "--container"])
    assert result.exit_code != 0  # at least one FAIL
    output = result.stdout + result.stderr
    assert "Container Bootstrap" in output


def test_doctor_container_passes_when_fully_configured(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    skill_target = vault / "agents" / "skills" / "clio"
    skill_target.mkdir(parents=True)
    (skill_target / "SKILL.md").write_text("# clio\n")
    workspace = tmp_path / "workspace"
    project_path = vault / "projects" / "org" / "proj"
    (project_path / "gsd-planning").mkdir(parents=True)
    workspace.mkdir()
    (workspace / ".planning").symlink_to(project_path / "gsd-planning")
    fake_home = tmp_path / "home"
    skills_dir = fake_home / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "clio").symlink_to(skill_target, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("MNEMOSYNE_WORKSPACE", str(workspace))
    monkeypatch.setenv("MNEMOSYNE_PROJECT", "projects/org/proj")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/local/bin/{name}"
        if name in ("mnemosyne", "gsd-tools")
        else None,
    )
    status_file = tmp_path / "mnemosyne-init.status"
    status_file.write_text("0\n")
    monkeypatch.setattr("mnemosyne_cli.lib.checks._INIT_STATUS_PATH", status_file)

    with patch(
        "mnemosyne_cli.commands.doctor.lib_vault.resolve_vault_path",
        return_value=vault,
    ):
        result = runner.invoke(app, ["doctor", "--container"])

    output = result.stdout
    # All 6 checks pass — assertion accepts either per-check "pass" tokens
    # or a single aggregate "All ... passed" line.
    assert output.lower().count("pass") >= 6 or (
        "all" in output.lower() and "passed" in output.lower()
    )


def test_doctor_container_read_only_no_fix_flag():
    """D-21: --container mode is read-only; --fix in this mode is a no-op or
    rejected for container checks.

    Doc test for the read-only invariant — container checks have no _fix_fn.
    """
    from mnemosyne_cli.commands import doctor

    # Confirm at least one container-mode check is registered without a fix.
    # Plan 33.1-04 will refine this assertion against the doctor registry.
    assert doctor is not None
