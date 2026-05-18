"""Tests for `mnemosyne init --container` mode — Phase 33 Plan 02."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from mnemosyne_cli.commands import init


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vault_project(vault_root: Path, project_rel: str) -> Path:
    """Build a minimal vault project under vault_root.

    Creates: gsd-planning/, AGENTS.md, claude-config/
    """
    vpp = vault_root / project_rel
    (vpp / "gsd-planning").mkdir(parents=True)
    (vpp / "AGENTS.md").write_text("# x\n", encoding="utf-8")
    (vpp / "claude-config").mkdir()
    return vpp


def _make_git_target(tmp_path: Path, name: str = "workspace") -> Path:
    """Create a directory that looks like a git worktree (has .git/info)."""
    target = tmp_path / name
    (target / ".git" / "info").mkdir(parents=True)
    return target


# ---------------------------------------------------------------------------
# Container mode — happy path
# ---------------------------------------------------------------------------


def test_container_mode_wires_target_with_args(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    target = _make_git_target(tmp_path)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=target)

    assert (target / ".planning").is_symlink()
    assert (target / "AGENTS.md").is_symlink()
    assert (target / "CLAUDE.md").is_symlink()
    # .envrc MUST NOT have been written
    assert not (target / ".envrc").exists()
    # Hooks installed
    assert (target / ".git" / "hooks" / "post-commit").exists()
    assert (target / ".git" / "hooks" / "post-merge").exists()


def test_container_mode_uses_workspace_env_var_as_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    target = _make_git_target(tmp_path)
    monkeypatch.setenv("MNEMOSYNE_WORKSPACE", str(target))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=None)

    assert (target / ".planning").is_symlink()


def test_container_mode_uses_project_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    target = _make_git_target(tmp_path)
    monkeypatch.setenv("MNEMOSYNE_PROJECT", "projects/org/proj")

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project=None, container=True, target=target)

    assert (target / ".planning").is_symlink()


def test_container_mode_is_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    target = _make_git_target(tmp_path)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=target)
        # Second run must not raise
        init.run(project="projects/org/proj", container=True, target=target)

    assert (target / ".planning").is_symlink()
    assert (target / "AGENTS.md").is_symlink()


def test_container_mode_skips_envrc(tmp_path: Path) -> None:
    """D-08: container mode must not invoke lib_envrc.set_envrc_vault."""
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    target = _make_git_target(tmp_path)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault") as envrc_mock,
    ):
        init.run(project="projects/org/proj", container=True, target=target)

    envrc_mock.assert_not_called()


def test_container_mode_registers_git_exclusions_without_envrc(tmp_path: Path) -> None:
    """D-08: container init still configures git exclusions, but never adds .envrc."""
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    target = _make_git_target(tmp_path)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=target / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.add_git_exclusion") as add_excl_mock,
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
    ):
        init.run(project="projects/org/proj", container=True, target=target)

    # Collect entries passed to add_git_exclusion
    entries = [call.args[0] for call in add_excl_mock.call_args_list]
    assert ".planning" in entries
    assert "AGENTS.md" in entries
    assert "CLAUDE.md" in entries
    # CRITICAL D-08: .envrc must NOT be in the exclude list in container mode
    assert ".envrc" not in entries


# ---------------------------------------------------------------------------
# Container mode — no-op short-circuits (all must exit 0, not 1)
# ---------------------------------------------------------------------------


def test_container_mode_no_project_exits_zero_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --project and no MNEMOSYNE_PROJECT → exit 0 with 'no vault project configured'."""
    target = _make_git_target(tmp_path)
    monkeypatch.delenv("MNEMOSYNE_PROJECT", raising=False)
    with pytest.raises(typer.Exit) as exc_info:
        init.run(project=None, container=True, target=target)
    assert exc_info.value.exit_code == 0
    captured = capsys.readouterr()
    assert "no vault project configured" in captured.err


def test_container_mode_no_target_exits_zero_with_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MNEMOSYNE_WORKSPACE", raising=False)
    with pytest.raises(typer.Exit) as exc_info:
        init.run(project="projects/org/proj", container=True, target=None)
    assert exc_info.value.exit_code == 0
    captured = capsys.readouterr()
    assert "no vault project configured" in captured.err


def test_container_mode_target_does_not_exist_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--target points at a path that doesn't exist → exit 0 with skip message."""
    target = tmp_path / "missing"  # never created
    with pytest.raises(typer.Exit) as exc_info:
        init.run(project="projects/org/proj", container=True, target=target)
    assert exc_info.value.exit_code == 0
    captured = capsys.readouterr()
    assert "no vault project configured" in captured.err


def test_container_mode_project_not_in_vault_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = _make_git_target(tmp_path)
    with patch(
        "mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault
    ):
        with pytest.raises(typer.Exit) as exc_info:
            init.run(project="projects/ghost/proj", container=True, target=target)
    assert exc_info.value.exit_code == 0
    captured = capsys.readouterr()
    assert "no vault project configured" in captured.err


def test_container_mode_target_not_git_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"
    _make_vault_project(vault, "projects/org/proj")
    # Target exists but has no .git
    target = tmp_path / "workspace"
    target.mkdir()
    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault),
        patch(
            "mnemosyne_cli.commands.init.lib_git.get_git_dir",
            side_effect=Exception("not a repo"),
        ),
    ):
        with pytest.raises(typer.Exit) as exc_info:
            init.run(project="projects/org/proj", container=True, target=target)
    assert exc_info.value.exit_code == 0
    captured = capsys.readouterr()
    assert "no vault project configured" in captured.err


# ---------------------------------------------------------------------------
# CLI-level smoke test (typer runner)
# ---------------------------------------------------------------------------


def test_cli_accepts_container_and_target_options() -> None:
    """mnemosyne init --help advertises --container and --target."""
    from mnemosyne_cli.main import app as root_app

    result = runner.invoke(root_app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--container" in result.output
    assert "--target" in result.output
