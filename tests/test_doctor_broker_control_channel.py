"""RED tests for SBR-3.3 broker control-channel health doctor check.

Plan 33.3-04 (Wave 3) implements `_check_broker_control_channel_health` in
`mnemosyne_cli.commands.doctor`. The check shells out to
`scion hub brokers --json`, parses connectionState / status / lastHeartbeat,
and reports stale / disconnected brokers.

Each test uses lazy imports inside the function body (Phase 38 P02 convention)
so `pytest --collect-only` succeeds even before implementation lands.
"""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock


FIXTURES = Path(__file__).parent / "fixtures" / "scion_hub"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hub_run_returning(json_payload: str, *, returncode: int = 0):
    """Build a fake subprocess.run that returns the given hub-broker JSON."""

    def fake_run(args, *a, **k):
        return MagicMock(returncode=returncode, stdout=json_payload, stderr="")

    return fake_run


def test_broker_healthy(tmp_path, monkeypatch):
    payload = FIXTURES.joinpath("brokers_healthy.json").read_text().replace(
        "REPLACE_WITH_NOW_ISO", _now_iso()
    )
    # Patch hostname so the broker check matches "appa.local"
    monkeypatch.setattr(socket, "gethostname", lambda: "appa.local")
    monkeypatch.setattr(subprocess, "run", _hub_run_returning(payload))

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is True


def test_broker_stale_heartbeat(tmp_path, monkeypatch):
    payload = FIXTURES.joinpath("brokers_stale.json").read_text()
    monkeypatch.setattr(socket, "gethostname", lambda: "appa.local")
    monkeypatch.setattr(subprocess, "run", _hub_run_returning(payload))

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is False
    assert result.fix_cmd == "systemctl --user restart scion-broker"


def test_broker_disconnected(tmp_path, monkeypatch):
    payload = FIXTURES.joinpath("brokers_offline.json").read_text().replace(
        "REPLACE_WITH_NOW_ISO", _now_iso()
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "appa.local")
    monkeypatch.setattr(subprocess, "run", _hub_run_returning(payload))

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is False


def test_broker_not_registered(tmp_path, monkeypatch):
    """Empty hub broker list → ok=False, message references this host's hostname."""
    monkeypatch.setattr(socket, "gethostname", lambda: "appa.local")
    monkeypatch.setattr(subprocess, "run", _hub_run_returning("[]"))

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is False
    assert "appa.local" in result.message


def test_scion_cli_missing(tmp_path, monkeypatch):
    """When scion is not on PATH (FileNotFoundError), check is skipped (ok=True per RESEARCH ref impl)."""

    def fake_run(args, *a, **k):
        raise FileNotFoundError("scion")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is True


def test_scion_timeout(tmp_path, monkeypatch):
    def fake_run(args, *a, **k):
        raise subprocess.TimeoutExpired(args, timeout=5)

    monkeypatch.setattr(subprocess, "run", fake_run)

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is False


def test_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "appa.local")
    monkeypatch.setattr(subprocess, "run", _hub_run_returning("not-json-at-all"))

    from mnemosyne_cli.commands.doctor import _check_broker_control_channel_health
    result = _check_broker_control_channel_health()
    assert result.ok is False
