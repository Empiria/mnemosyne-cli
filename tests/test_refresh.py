"""Tests for `mnemosyne refresh` — SCION agent image pull restoration.

The legacy-image pull was stripped in a4a7d49 along with the retired
mnemosyne-base/mnemosyne-claude images; these tests cover its return,
repointed at the SCION empiria-claude image stack.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mnemosyne_cli.commands import refresh
from mnemosyne_cli.lib import vault
from mnemosyne_cli.main import app


runner = CliRunner()


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Point refresh at a tmp vault and capture subprocess invocations."""
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: tmp_path)
    monkeypatch.setattr(refresh.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    return calls


def test_refresh_pulls_scion_images(fake_env):
    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 0, result.output
    pulls = [c for c in fake_env if c[:2] == ["podman", "pull"]]
    assert [c[2] for c in pulls] == [
        "ghcr.io/empiria/empiria-claude:latest",
        "ghcr.io/empiria/empiria-claude-anvil:latest",
    ]


def test_refresh_skip_images(fake_env):
    result = runner.invoke(app, ["refresh", "--skip-images"])
    assert result.exit_code == 0, result.output
    pulls = [c for c in fake_env if c[:2] == ["podman", "pull"]]
    assert pulls == []


def test_refresh_pull_failure_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: tmp_path)
    monkeypatch.setattr(refresh.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, *a, **k):
        rc = 1 if args[:2] == ["podman", "pull"] else 0
        return MagicMock(returncode=rc, stdout="", stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)

    result = runner.invoke(app, ["refresh", "--skip-qmd"])
    assert result.exit_code == 1


def test_refresh_skips_pull_when_podman_missing(tmp_path, monkeypatch):
    """No podman on PATH (e.g. inside a container) must not fail the refresh."""
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: tmp_path)
    monkeypatch.setattr(
        refresh.shutil, "which", lambda name: None if name == "podman" else f"/usr/bin/{name}"
    )

    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)

    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 0, result.output
    assert not any(c[:2] == ["podman", "pull"] for c in calls)
