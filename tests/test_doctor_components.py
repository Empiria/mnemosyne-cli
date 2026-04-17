"""Tests for the doctor Components category gate — Phase 32 Plan 05."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.commands.doctor import _components_apply_here


@pytest.fixture
def mnemosyne_vault_with_planning_symlink(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a fake vault + a client cwd whose .planning symlinks into projects/empiria/mnemosyne/gsd-planning/.

    Returns (cwd, vault_root).
    """
    vault_root = tmp_path / "vault"
    mnemosyne_planning = vault_root / "projects" / "empiria" / "mnemosyne" / "gsd-planning"
    mnemosyne_planning.mkdir(parents=True)

    cwd = tmp_path / "client"
    cwd.mkdir()
    (cwd / ".planning").symlink_to(mnemosyne_planning)
    return cwd, vault_root


def test_components_apply_when_planning_links_to_mnemosyne(mnemosyne_vault_with_planning_symlink: tuple[Path, Path]) -> None:
    cwd, vault_root = mnemosyne_vault_with_planning_symlink
    assert _components_apply_here(cwd, vault_root) is True


def test_components_skip_when_no_planning_symlink(tmp_path: Path) -> None:
    cwd = tmp_path / "client"
    cwd.mkdir()
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    assert _components_apply_here(cwd, vault_root) is False


def test_components_skip_when_planning_links_to_other_project(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    other_planning = vault_root / "projects" / "friendly-fox" / "infinite-worlds" / "gsd-planning"
    other_planning.mkdir(parents=True)

    cwd = tmp_path / "client"
    cwd.mkdir()
    (cwd / ".planning").symlink_to(other_planning)

    assert _components_apply_here(cwd, vault_root) is False
