"""Tests for mnemosyne_cli.lib.components — Phase 32 Plan 02."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.lib import components, vault


@pytest.fixture
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vault._CONFIG_PATH to a tmp file for the duration of the test."""
    config = tmp_path / "config.toml"
    monkeypatch.setattr(vault, "_CONFIG_PATH", config)
    return config


def _write(config_path: Path, body: str) -> None:
    config_path.write_text(body)


# -- read_components_config -------------------------------------------------

def test_read_empty_config_returns_empty_dict(tmp_config: Path) -> None:
    assert components.read_components_config() == {}


def test_read_no_components_section_returns_empty_dict(tmp_config: Path) -> None:
    _write(tmp_config, 'vault_path = "/tmp/vault"\n')
    assert components.read_components_config() == {}


def test_read_single_component(tmp_config: Path, tmp_path: Path) -> None:
    component_dir = tmp_path / "scion"
    component_dir.mkdir()
    _write(tmp_config, f'[components.scion]\nlocal_path = "{component_dir}"\n')
    result = components.read_components_config()
    assert "scion" in result
    assert result["scion"].local_path == component_dir.resolve()


def test_read_multiple_components(tmp_config: Path, tmp_path: Path) -> None:
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    scion_dir = tmp_path / "scion"
    scion_dir.mkdir()
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    _write(
        tmp_config,
        f'[components.mnemosyne-cli]\nlocal_path = "{cli_dir}"\n\n'
        f'[components.scion]\nlocal_path = "{scion_dir}"\n\n'
        f'[components.ops]\nlocal_path = "{ops_dir}"\n',
    )
    result = components.read_components_config()
    assert set(result.keys()) == {"mnemosyne-cli", "scion", "ops"}


def test_read_expands_tilde(tmp_config: Path) -> None:
    _write(tmp_config, '[components.foo]\nlocal_path = "~/some/path"\n')
    result = components.read_components_config()
    assert str(result["foo"].local_path).startswith(str(Path.home()))


def test_read_skips_entries_without_local_path(tmp_config: Path) -> None:
    _write(tmp_config, '[components.bad]\ndescription = "no path here"\n')
    assert components.read_components_config() == {}


# -- resolve_component_path -------------------------------------------------

def test_resolve_returns_path_when_configured_and_present(tmp_config: Path, tmp_path: Path) -> None:
    component_dir = tmp_path / "real"
    component_dir.mkdir()
    _write(tmp_config, f'[components.real]\nlocal_path = "{component_dir}"\n')
    assert components.resolve_component_path("real") == component_dir.resolve()


def test_resolve_raises_not_configured_when_absent(tmp_config: Path) -> None:
    _write(tmp_config, "")
    with pytest.raises(components.ComponentNotConfigured) as exc:
        components.resolve_component_path("missing")
    assert exc.value.name == "missing"
    assert "missing" in exc.value.remediation()
    assert "[components.missing]" in exc.value.remediation()


def test_resolve_raises_not_cloned_when_path_missing(tmp_config: Path, tmp_path: Path) -> None:
    fake = tmp_path / "never-existed"
    _write(tmp_config, f'[components.ghost]\nlocal_path = "{fake}"\n')
    with pytest.raises(components.ComponentNotCloned) as exc:
        components.resolve_component_path("ghost")
    assert exc.value.name == "ghost"
    assert exc.value.path == fake.resolve()
    assert "git clone" in exc.value.remediation()


# -- write_component_to_config ---------------------------------------------

def test_write_creates_components_section(tmp_config: Path, tmp_path: Path) -> None:
    cfg = components.ComponentConfig(name="new", local_path=tmp_path / "new")
    components.write_component_to_config(cfg)
    body = tmp_config.read_text()
    assert "[components.new]" in body
    assert "local_path" in body


def test_write_preserves_other_sections(tmp_config: Path, tmp_path: Path) -> None:
    _write(tmp_config, 'vault_path = "/x/y"\nmodel_profile = "balanced"\n')
    cfg = components.ComponentConfig(name="cli", local_path=tmp_path / "cli")
    components.write_component_to_config(cfg)
    body = tmp_config.read_text()
    assert "/x/y" in body
    assert "balanced" in body
    assert "[components.cli]" in body


def test_write_round_trips_tilde_for_home_paths(tmp_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home_path = tmp_path / "projects" / "thing"
    cfg = components.ComponentConfig(name="thing", local_path=home_path)
    components.write_component_to_config(cfg)
    body = tmp_config.read_text()
    assert '"~/projects/thing"' in body
