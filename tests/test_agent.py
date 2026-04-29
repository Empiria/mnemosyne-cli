"""Unit tests for agent.py — DEP-01 through DEP-04."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mnemosyne_cli.commands.agent import _find_container_toml, app


runner = CliRunner()


# ---------------------------------------------------------------------------
# DEP-01: _find_container_toml returns path when file exists
# ---------------------------------------------------------------------------

def test_find_container_toml(vault_dir: Path) -> None:
    """_find_container_toml finds container.toml by project slug."""
    result = _find_container_toml(vault_dir, "infinite-worlds")
    assert result is not None
    assert isinstance(result, Path)
    assert result.name == "container.toml"


# ---------------------------------------------------------------------------
# DEP-02: _find_container_toml returns None when file absent
# ---------------------------------------------------------------------------

def test_find_container_toml_missing(vault_dir: Path) -> None:
    """_find_container_toml returns None for unknown project."""
    result = _find_container_toml(vault_dir, "no-such-project")
    assert result is None


# ---------------------------------------------------------------------------
# DEP-03: start() adds container.toml bind-mount when found
# ---------------------------------------------------------------------------

def test_start_adds_toml_mount(vault_dir: Path, tmp_path: Path) -> None:
    """start() extends cmd with the container.toml bind-mount when file exists."""
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with (
        patch("mnemosyne_cli.commands.agent.subprocess.run", side_effect=fake_run),
        patch("mnemosyne_cli.commands.agent.vault.resolve_vault_path", return_value=vault_dir),
        patch("mnemosyne_cli.commands.agent._container_is_running", return_value=False),
        patch("mnemosyne_cli.commands.agent._load_agent_env", return_value={"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}),
        patch("mnemosyne_cli.commands.agent.os.execvp"),
    ):
        result = runner.invoke(app, ["start", "feature-branch", "--project", "infinite-worlds", "--repo", str(tmp_path), "--detach"])

    # Find all -v argument values
    v_mounts = [captured_cmd[i + 1] for i, arg in enumerate(captured_cmd) if arg == "-v"]
    # Check that one of the mounts ends with :/config/container.toml:ro
    toml_mounts = [m for m in v_mounts if "container.toml:/config/container.toml:ro" in m]
    assert toml_mounts, f"No container.toml mount found in cmd. Mounts: {v_mounts}"


# ---------------------------------------------------------------------------
# DEP-04: start() adds dep-cache volume + three env vars unconditionally
# ---------------------------------------------------------------------------

def test_start_adds_dep_cache(vault_dir: Path, tmp_path: Path) -> None:
    """start() adds dep-cache volume and UV/Playwright/Cargo env vars unconditionally."""
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with (
        patch("mnemosyne_cli.commands.agent.subprocess.run", side_effect=fake_run),
        patch("mnemosyne_cli.commands.agent.vault.resolve_vault_path", return_value=vault_dir),
        patch("mnemosyne_cli.commands.agent._container_is_running", return_value=False),
        patch("mnemosyne_cli.commands.agent._load_agent_env", return_value={"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}),
        patch("mnemosyne_cli.commands.agent.os.execvp"),
    ):
        result = runner.invoke(app, ["start", "feature-branch", "--project", "infinite-worlds", "--repo", str(tmp_path), "--detach"])

    # Find -v mounts
    v_mounts = [captured_cmd[i + 1] for i, arg in enumerate(captured_cmd) if arg == "-v"]
    assert any("dep-cache-infinite-worlds:/home/agent/.dep-cache" in m for m in v_mounts), (
        f"dep-cache volume not found. Mounts: {v_mounts}"
    )

    # Find -e env vars
    e_vars = [captured_cmd[i + 1] for i, arg in enumerate(captured_cmd) if arg == "-e"]
    assert "UV_CACHE_DIR=/home/agent/.dep-cache/uv" in e_vars, f"UV_CACHE_DIR missing. Env vars: {e_vars}"
    assert "PLAYWRIGHT_BROWSERS_PATH=/home/agent/.dep-cache/ms-playwright" in e_vars, (
        f"PLAYWRIGHT_BROWSERS_PATH missing. Env vars: {e_vars}"
    )
    assert "CARGO_HOME=/home/agent/.dep-cache/cargo" in e_vars, f"CARGO_HOME missing. Env vars: {e_vars}"
