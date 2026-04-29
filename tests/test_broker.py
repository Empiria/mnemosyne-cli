"""Tests for broker service-file generation and patching."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from mnemosyne_cli.lib import broker


@pytest.fixture
def fake_scion(tmp_path: Path) -> Path:
    p = tmp_path / "scion"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_systemd_unit_includes_vault_host(fake_scion: Path) -> None:
    unit = broker.render_systemd_unit(
        vault_host=Path("/srv/vault"), scion_bin=fake_scion
    )
    assert "Environment=MNEMOSYNE_VAULT_HOST=/srv/vault" in unit
    assert f"ExecStart={fake_scion} broker start -p local" in unit
    assert "[Unit]" in unit
    assert "[Install]" in unit


def test_render_systemd_unit_optional_extras(fake_scion: Path) -> None:
    unit = broker.render_systemd_unit(
        vault_host=Path("/v"),
        scion_bin=fake_scion,
        ssh_auth_sock="/run/user/1000/ssh",
        extra_path="/usr/bin:/bin",
    )
    assert "Environment=SSH_AUTH_SOCK=/run/user/1000/ssh" in unit
    assert "Environment=PATH=/usr/bin:/bin" in unit


def test_render_launchd_plist_roundtrip(fake_scion: Path, tmp_path: Path) -> None:
    raw = broker.render_launchd_plist(
        vault_host=Path("/Users/joe/vault"),
        scion_bin=fake_scion,
        home=tmp_path,
    )
    pl = plistlib.loads(raw)
    assert pl["Label"] == broker.LAUNCHD_LABEL
    assert pl["EnvironmentVariables"]["MNEMOSYNE_VAULT_HOST"] == "/Users/joe/vault"
    assert pl["ProgramArguments"][0] == str(fake_scion)
    assert "--foreground" in pl["ProgramArguments"]
    assert pl["RunAtLoad"] is True


# ---------------------------------------------------------------------------
# Sync — systemd
# ---------------------------------------------------------------------------


def _write_systemd(path: Path, host: str, *, ssh: str = "/run/user/1000/ssh") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""[Unit]
Description=SCION Broker

[Service]
Type=forking
Environment=MNEMOSYNE_VAULT_HOST={host}
Environment=SSH_AUTH_SOCK={ssh}
ExecStart=/usr/local/bin/scion broker start -p local

[Install]
WantedBy=default.target
"""
    )


def test_sync_systemd_replaces_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit = tmp_path / "scion-broker.service"
    _write_systemd(unit, "/old/path")
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: unit)

    changed = broker.sync_vault_host(Path("/new/path"))
    text = unit.read_text()

    assert changed is True
    assert "Environment=MNEMOSYNE_VAULT_HOST=/new/path" in text
    assert "Environment=MNEMOSYNE_VAULT_HOST=/old/path" not in text
    # User customisation survives
    assert "Environment=SSH_AUTH_SOCK=/run/user/1000/ssh" in text


def test_sync_systemd_noop_when_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit = tmp_path / "scion-broker.service"
    _write_systemd(unit, "/same/path")
    mtime_before = unit.stat().st_mtime_ns
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: unit)

    changed = broker.sync_vault_host(Path("/same/path"))

    assert changed is False
    # No write happened
    assert unit.stat().st_mtime_ns == mtime_before


def test_sync_systemd_inserts_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit = tmp_path / "scion-broker.service"
    unit.write_text(
        """[Unit]
Description=SCION Broker

[Service]
Type=forking
ExecStart=/usr/local/bin/scion broker start -p local
"""
    )
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: unit)

    changed = broker.sync_vault_host(Path("/v"))

    assert changed is True
    assert "Environment=MNEMOSYNE_VAULT_HOST=/v" in unit.read_text()


def test_sync_returns_false_when_file_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit = tmp_path / "missing.service"
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: unit)

    assert broker.sync_vault_host(Path("/v")) is False


# ---------------------------------------------------------------------------
# Sync — launchd
# ---------------------------------------------------------------------------


def _write_plist(path: Path, host: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl = {
        "Label": broker.LAUNCHD_LABEL,
        "ProgramArguments": ["/usr/local/bin/scion", "broker", "start"],
        "EnvironmentVariables": {
            "MNEMOSYNE_VAULT_HOST": host,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
        "RunAtLoad": True,
    }
    with path.open("wb") as f:
        plistlib.dump(pl, f)


def test_sync_launchd_replaces_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist = tmp_path / "scion-broker.plist"
    _write_plist(plist, "/Users/joe/old")
    monkeypatch.setattr(broker, "detect_platform", lambda: "macos")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: plist)

    changed = broker.sync_vault_host(Path("/Users/joe/new"))

    assert changed is True
    with plist.open("rb") as f:
        loaded = plistlib.load(f)
    assert loaded["EnvironmentVariables"]["MNEMOSYNE_VAULT_HOST"] == "/Users/joe/new"
    # PATH preserved
    assert loaded["EnvironmentVariables"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_sync_launchd_noop_when_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist = tmp_path / "scion-broker.plist"
    _write_plist(plist, "/Users/joe/same")
    monkeypatch.setattr(broker, "detect_platform", lambda: "macos")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: plist)

    assert broker.sync_vault_host(Path("/Users/joe/same")) is False


# ---------------------------------------------------------------------------
# install_service
# ---------------------------------------------------------------------------


def test_install_creates_when_absent(
    tmp_path: Path, fake_scion: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "user" / "scion-broker.service"
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: target)

    result = broker.install_service(Path("/v"), scion_bin=fake_scion)

    assert result.created is True
    assert result.changed is True
    assert result.path == target
    assert target.exists()
    assert "Environment=MNEMOSYNE_VAULT_HOST=/v" in target.read_text()


def test_install_patches_when_existing(
    tmp_path: Path, fake_scion: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "scion-broker.service"
    _write_systemd(target, "/old", ssh="/custom/ssh")
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: target)

    result = broker.install_service(Path("/new"), scion_bin=fake_scion)

    text = target.read_text()
    assert result.created is False
    assert result.changed is True
    assert "Environment=MNEMOSYNE_VAULT_HOST=/new" in text
    # User's SSH_AUTH_SOCK customisation must survive a sync
    assert "Environment=SSH_AUTH_SOCK=/custom/ssh" in text


def test_install_force_rewrites_existing(
    tmp_path: Path, fake_scion: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "scion-broker.service"
    _write_systemd(target, "/old", ssh="/custom/ssh")
    monkeypatch.setattr(broker, "detect_platform", lambda: "linux")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: target)

    result = broker.install_service(Path("/new"), scion_bin=fake_scion, force=True)

    text = target.read_text()
    assert result.created is True
    assert "Environment=MNEMOSYNE_VAULT_HOST=/new" in text
    # Force regen drops user customisations (documented behaviour)
    assert "/custom/ssh" not in text


def test_install_macos_writes_valid_plist(
    tmp_path: Path, fake_scion: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "scion-broker.plist"
    monkeypatch.setattr(broker, "detect_platform", lambda: "macos")
    monkeypatch.setattr(broker, "service_file_path", lambda p=None: target)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = broker.install_service(Path("/Users/joe/vault"), scion_bin=fake_scion)

    assert result.created is True
    with target.open("rb") as f:
        pl = plistlib.load(f)
    assert pl["Label"] == broker.LAUNCHD_LABEL
    assert pl["EnvironmentVariables"]["MNEMOSYNE_VAULT_HOST"] == "/Users/joe/vault"


# ---------------------------------------------------------------------------
# reload_command
# ---------------------------------------------------------------------------


def test_reload_command_linux() -> None:
    assert broker.reload_command("linux") == (
        "systemctl --user daemon-reload && systemctl --user restart scion-broker"
    )


def test_reload_command_macos() -> None:
    assert (
        broker.reload_command("macos")
        == f"launchctl kickstart -k gui/$UID/{broker.LAUNCHD_LABEL}"
    )
