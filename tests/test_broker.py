"""Tests for broker service-file generation and patching."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
import typer

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


def test_render_systemd_unit_includes_vault_host(fake_scion: Path, tmp_path: Path) -> None:
    # Phase 33.3 D-04: ExecStart now runs `mnemosyne broker start`, not scion.
    fake_mnemosyne = tmp_path / "mnemosyne"
    fake_mnemosyne.write_text("#!/bin/sh\n")
    fake_mnemosyne.chmod(0o755)
    unit = broker.render_systemd_unit(
        vault_host=Path("/srv/vault"),
        scion_bin=fake_scion,
        mnemosyne_bin=fake_mnemosyne,
    )
    assert "Environment=MNEMOSYNE_VAULT_HOST=/srv/vault" in unit
    assert f"ExecStart={fake_mnemosyne} broker start" in unit
    assert f"ExecStop={fake_scion} broker stop" in unit
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


# ---------------------------------------------------------------------------
# Phase 33.3 — SBR-3.1 / SBR-3.7 verbs (start, restore-config, apply-empiria-defaults)
# ---------------------------------------------------------------------------


def test_start_calls_overlay_then_execvp(monkeypatch: pytest.MonkeyPatch) -> None:
    """SBR-3.1 D-04: start verb applies overlay then execs scion."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib

    calls: list[tuple] = []
    monkeypatch.setattr(
        broker_lib, "apply_harness_config_overlay", lambda *a, **k: broker_lib.OverlayResult()
    )
    monkeypatch.setattr("os.execvp", lambda *args: calls.append(args))
    broker_cmd.start()
    assert calls and calls[0][0] == "scion"
    assert calls[0][1] == ["scion", "broker", "start", "-p", "local"]


def test_start_execs_even_when_overlay_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Overlay failure must NOT prevent broker startup (RESEARCH Pattern 2)."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib

    calls: list[tuple] = []

    def _boom(*a, **k):
        raise RuntimeError("overlay broke")

    monkeypatch.setattr(broker_lib, "apply_harness_config_overlay", _boom)
    monkeypatch.setattr("os.execvp", lambda *args: calls.append(args))
    broker_cmd.start()
    assert calls, "execvp must run even when overlay raises"
    err = capsys.readouterr().err
    assert "overlay broke" in err  # logged to stderr


def test_restore_config_calls_apply_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SBR-3.1 D-33: restore-config delegates to lib helper."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib

    called_with: dict = {}

    def _spy(seed_dir=None):
        called_with["seed_dir"] = seed_dir
        return broker_lib.OverlayResult(written=[tmp_path / "out"])

    monkeypatch.setattr(broker_lib, "apply_harness_config_overlay", _spy)
    broker_cmd.restore_config(seed_dir=None)
    assert "seed_dir" in called_with


def test_restore_config_exits_on_missing_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """restore-config maps FileNotFoundError (bad --seed-dir) to exit 1."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib

    def _missing(seed_dir=None):
        raise FileNotFoundError("Seed dir does not exist")

    monkeypatch.setattr(broker_lib, "apply_harness_config_overlay", _missing)
    with pytest.raises(typer.Exit) as exc:
        broker_cmd.restore_config(seed_dir=Path("/nonexistent"))
    assert exc.value.exit_code == 1


def test_apply_empiria_defaults_cmd_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """--dry-run flag passthrough; output uses [would write] prefix."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib

    monkeypatch.setattr(
        broker_lib,
        "apply_empiria_defaults",
        lambda dry_run=False: broker_lib.OverlayResult(written=[Path("/tmp/x")]),
    )
    broker_cmd.apply_empiria_defaults_cmd(dry_run=True)
    out = capsys.readouterr().out
    assert "[would write]" in out and "Would apply" in out


def test_apply_empiria_defaults_cmd_exits_when_settings_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pitfall 5: missing settings.yaml -> exit 1."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib

    def _missing(dry_run=False):
        raise FileNotFoundError("Run `scion init` first")

    monkeypatch.setattr(broker_lib, "apply_empiria_defaults", _missing)
    with pytest.raises(typer.Exit) as exc:
        broker_cmd.apply_empiria_defaults_cmd(dry_run=False)
    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# Phase 33.3 — Task 04.1 check-control-channel verb exit-code matrix (D-38)
# ---------------------------------------------------------------------------


def test_check_control_channel_verb_passes(monkeypatch, capsys):
    """PASS check -> exit 0, no restart attempted."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.symlinks import CheckResult

    monkeypatch.setattr(
        broker_lib,
        "check_control_channel",
        lambda: CheckResult(ok=True, message="healthy"),
    )
    # Should NOT raise typer.Exit.
    broker_cmd.check_control_channel_cmd(restart_if_stale=False)
    out = capsys.readouterr().out
    assert "ok" in out and "healthy" in out


def test_check_control_channel_verb_fail_diagnostic_mode_exits_1(monkeypatch):
    """FAIL check without --restart-if-stale -> exit 1 (diagnostic mode)."""
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.symlinks import CheckResult

    monkeypatch.setattr(
        broker_lib,
        "check_control_channel",
        lambda: CheckResult(ok=False, message="stale"),
    )
    with pytest.raises(typer.Exit) as exc:
        broker_cmd.check_control_channel_cmd(restart_if_stale=False)
    assert exc.value.exit_code == 1


def test_check_control_channel_verb_successful_restart_exits_0(monkeypatch, capsys):
    """FAIL check + --restart-if-stale + restart returncode 0 -> exit 0 (per D-38).

    This is the CRITICAL test: successful auto-recovery MUST NOT raise typer.Exit,
    otherwise the systemd Path-unit would record a failed trigger and burn a
    StartLimitBurst slot on every successful recovery, deactivating the watchdog
    after 5 recoveries in 10 minutes.
    """
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.symlinks import CheckResult

    monkeypatch.setattr(
        broker_lib,
        "check_control_channel",
        lambda: CheckResult(ok=False, message="stale", fix_cmd=""),
    )

    class _OkProc:
        returncode = 0

    calls = []

    def _spy(cmd, *a, **k):
        calls.append(cmd)
        return _OkProc()

    monkeypatch.setattr("subprocess.run", _spy)

    # Should NOT raise typer.Exit — exit 0 path.
    broker_cmd.check_control_channel_cmd(restart_if_stale=True)
    assert any(
        "systemctl" in c and "restart" in c for c in calls
    ), "restart subprocess must be invoked"
    out = capsys.readouterr().out
    assert "successfully" in out


def test_check_control_channel_verb_failed_restart_exits_1(monkeypatch):
    """FAIL check + --restart-if-stale + restart returncode != 0 -> exit 1.

    Genuine restart failure (e.g., systemd refuses, unit masked) legitimately
    consumes a StartLimitBurst slot.
    """
    from mnemosyne_cli.commands import broker as broker_cmd
    from mnemosyne_cli.lib import broker as broker_lib
    from mnemosyne_cli.lib.symlinks import CheckResult

    monkeypatch.setattr(
        broker_lib,
        "check_control_channel",
        lambda: CheckResult(ok=False, message="stale", fix_cmd=""),
    )

    class _FailProc:
        returncode = 5

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FailProc())

    with pytest.raises(typer.Exit) as exc:
        broker_cmd.check_control_channel_cmd(restart_if_stale=True)
    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# Phase 33.3 — Task 04.2a Path-unit watchdog emission
# ---------------------------------------------------------------------------


def test_install_writes_pathunit(tmp_path, monkeypatch):
    """SBR-3.3 D-17: install_path_unit_watchdog writes both unit files."""
    from mnemosyne_cli.lib import broker

    mb = tmp_path / "mnemosyne"
    mb.write_text("#!/bin/sh\n")
    mb.chmod(0o755)
    monkeypatch.setattr(broker, "_find_mnemosyne_bin", lambda: mb)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Suppress systemctl calls.
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: type("X", (), {"returncode": 0})()
    )
    units = broker.install_path_unit_watchdog()
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / "scion-broker-control-channel-watchdog.path").exists()
    assert (unit_dir / "scion-broker-restart-on-broken-pipe.service").exists()
    svc_text = (
        unit_dir / "scion-broker-restart-on-broken-pipe.service"
    ).read_text()
    assert "StartLimitBurst=5" in svc_text
    assert "StartLimitIntervalSec=600" in svc_text
    assert (
        f"ExecStart={mb} broker check-control-channel --restart-if-stale"
        in svc_text
    )
    assert set(units) == {
        "scion-broker-control-channel-watchdog.path",
        "scion-broker-restart-on-broken-pipe.service",
    }


# ---------------------------------------------------------------------------
# Phase 33.3 — Task 04.2b install verb extension tests
# ---------------------------------------------------------------------------


def test_render_systemd_unit_phase33_3_shape(fake_scion: Path, tmp_path: Path):
    """Greenfield render carries the D-04 ExecStart shim + D-20 TimeoutStopSec."""
    from mnemosyne_cli.lib import broker

    fake_mnemosyne = tmp_path / "bin" / "mnemosyne"
    fake_mnemosyne.parent.mkdir()
    fake_mnemosyne.write_text("#!/bin/sh\n")
    fake_mnemosyne.chmod(0o755)
    unit = broker.render_systemd_unit(
        vault_host=Path("/srv/vault"),
        scion_bin=fake_scion,
        mnemosyne_bin=fake_mnemosyne,
    )
    assert f"ExecStart={fake_mnemosyne} broker start" in unit
    assert "TimeoutStopSec=120" in unit
    assert f"ExecStop={fake_scion} broker stop" in unit


def test_patch_existing_unit_inserts_timeoutstopsec(tmp_path):
    """SBR-3.1 D-04 + SBR-3.4 D-20: patch existing service file in place."""
    from mnemosyne_cli.lib import broker

    unit = tmp_path / "scion-broker.service"
    unit.write_text(
        "[Unit]\nDescription=SCION Broker\n\n[Service]\n"
        "Type=forking\nExecStart=/old/scion broker start -p local\n"
        "[Install]\nWantedBy=default.target\n"
    )
    mb = tmp_path / "mnemosyne"
    mb.write_text("#!/bin/sh\n")
    mb.chmod(0o755)
    changed = broker.patch_systemd_unit_phase33_3(unit, mb)
    assert changed
    text = unit.read_text()
    assert "TimeoutStopSec=120" in text
    assert f"ExecStart={mb} broker start" in text
    assert "/old/scion broker start" not in text
    # Idempotent — second run finds nothing to change.
    assert broker.patch_systemd_unit_phase33_3(unit, mb) is False


def test_install_prewarm_calls_podman(monkeypatch):
    """SBR-3.4 D-19: pre-warm invokes podman with the keep-id userns mapping."""
    from mnemosyne_cli.lib import broker

    calls = []

    def _spy(cmd, *a, **k):
        calls.append(cmd)
        return type("X", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", _spy)
    ok = broker.prewarm_empiria_claude()
    assert ok
    assert any(
        "podman" in c[0] and "empiria-claude:latest" in c for c in calls
    )
    assert any("--userns=keep-id:uid=1000,gid=1000" in c for c in calls)
