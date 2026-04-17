"""Tests for `mnemosyne component` CLI verbs — Phase 32 Plan 05."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mnemosyne_cli.commands.component import _envvar, _read_declared_components
from mnemosyne_cli.lib import vault
from mnemosyne_cli.main import app


runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config.toml"
    monkeypatch.setattr(vault, "_CONFIG_PATH", config)
    return config


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake vault with a project note declaring components."""
    vault_root = tmp_path / "vault"
    project_dir = vault_root / "projects" / "empiria" / "mnemosyne"
    project_dir.mkdir(parents=True)
    (project_dir / "mnemosyne.md").write_text(
        "---\n"
        "tags: [project]\n"
        "components:\n"
        "  - name: mnemosyne\n"
        "    repo: https://github.com/Empiria/mnemosyne\n"
        "  - name: mnemosyne-cli\n"
        "    repo: https://github.com/Empiria/mnemosyne-cli\n"
        "  - name: scion\n"
        "    repo: https://github.com/Empiria/scion\n"
        "  - name: ops\n"
        "    repo: https://github.com/owen/ops\n"
        "---\n"
        "# Mnemosyne\n"
    )
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(vault_root))
    return vault_root


# -- _envvar / _read_declared_components -----------------------------------

def test_envvar_simple_name() -> None:
    assert _envvar("scion") == "MNEMOSYNE_COMPONENT_SCION_HOST"


def test_envvar_hyphenated_name() -> None:
    assert _envvar("mnemosyne-cli") == "MNEMOSYNE_COMPONENT_MNEMOSYNE_CLI_HOST"


def test_read_declared_components_returns_all_four(tmp_vault: Path) -> None:
    names = _read_declared_components(tmp_vault, "projects/empiria/mnemosyne")
    assert names == ["mnemosyne", "mnemosyne-cli", "scion", "ops"]


def test_read_declared_components_returns_empty_for_missing_note(tmp_vault: Path) -> None:
    names = _read_declared_components(tmp_vault, "projects/empiria/no-such")
    assert names == []


# -- list ------------------------------------------------------------------

def test_list_empty_config(tmp_config: Path) -> None:
    result = runner.invoke(app, ["component", "list"])
    assert result.exit_code == 0
    assert "No components configured" in result.output


def test_list_shows_configured_components(tmp_config: Path, tmp_path: Path) -> None:
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    tmp_config.write_text(f'[components.mnemosyne-cli]\nlocal_path = "{cli_dir}"\n')
    result = runner.invoke(app, ["component", "list"])
    assert result.exit_code == 0
    assert "mnemosyne-cli" in result.output
    assert "on disk" in result.output


def test_list_marks_missing_paths(tmp_config: Path) -> None:
    tmp_config.write_text('[components.ghost]\nlocal_path = "/never/exists"\n')
    result = runner.invoke(app, ["component", "list"])
    assert result.exit_code == 0
    assert "MISSING" in result.output


# -- env -------------------------------------------------------------------

def test_env_emits_uppercase_underscore_var(tmp_config: Path, tmp_path: Path) -> None:
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    tmp_config.write_text(f'[components.mnemosyne-cli]\nlocal_path = "{cli_dir}"\n')
    result = runner.invoke(app, ["component", "env"])
    assert result.exit_code == 0
    assert f"MNEMOSYNE_COMPONENT_MNEMOSYNE_CLI_HOST={cli_dir}" in result.output


def test_env_systemd_prefix(tmp_config: Path, tmp_path: Path) -> None:
    scion_dir = tmp_path / "scion"
    scion_dir.mkdir()
    tmp_config.write_text(f'[components.scion]\nlocal_path = "{scion_dir}"\n')
    result = runner.invoke(app, ["component", "env", "--systemd"])
    assert result.exit_code == 0
    assert f"Environment=MNEMOSYNE_COMPONENT_SCION_HOST={scion_dir}" in result.output


# -- check -----------------------------------------------------------------

def test_check_passes_when_all_declared_components_present(tmp_config: Path, tmp_vault: Path, tmp_path: Path) -> None:
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    scion_dir = tmp_path / "scion"
    scion_dir.mkdir()
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    tmp_config.write_text(
        f'[components.mnemosyne-cli]\nlocal_path = "{cli_dir}"\n\n'
        f'[components.scion]\nlocal_path = "{scion_dir}"\n\n'
        f'[components.ops]\nlocal_path = "{ops_dir}"\n'
    )
    result = runner.invoke(app, ["component", "check", "--project", "projects/empiria/mnemosyne"])
    assert result.exit_code == 0
    assert "All 3 component" in result.output


def test_check_fails_with_remediation_when_unconfigured(tmp_config: Path, tmp_vault: Path) -> None:
    tmp_config.write_text("")
    result = runner.invoke(app, ["component", "check", "--project", "projects/empiria/mnemosyne"])
    assert result.exit_code == 1
    assert "mnemosyne-cli" in result.output
    assert "scion" in result.output
    assert "ops" in result.output
    assert "[components." in result.output


def test_check_fails_with_clone_hint_when_path_missing(tmp_config: Path, tmp_vault: Path) -> None:
    tmp_config.write_text(
        '[components.mnemosyne-cli]\nlocal_path = "/never/exists/cli"\n'
        '[components.scion]\nlocal_path = "/never/exists/scion"\n'
        '[components.ops]\nlocal_path = "/never/exists/ops"\n'
    )
    result = runner.invoke(app, ["component", "check", "--project", "projects/empiria/mnemosyne"])
    assert result.exit_code == 1
    assert "git clone" in result.output


def test_check_skips_when_project_has_no_components(tmp_config: Path, tmp_vault: Path) -> None:
    other_dir = tmp_vault / "projects" / "empiria" / "single-repo"
    other_dir.mkdir(parents=True)
    (other_dir / "single-repo.md").write_text("---\ntags: [project]\nrepositories: []\n---\n")
    result = runner.invoke(app, ["component", "check", "--project", "projects/empiria/single-repo"])
    assert result.exit_code == 0
    assert "not multi-repo" in result.output
