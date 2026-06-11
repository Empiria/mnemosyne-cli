"""Tests for vault agent worktrees — branch isolation for container agents.

Covers lib/vault_worktree.py (ensure/list/repair), the init --container
wiring through the worktree, and the host-side `mnemosyne vault worktrees`
/ `mnemosyne vault merge` verbs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mnemosyne_cli.lib import vault_worktree
from mnemosyne_cli.main import app


runner = CliRunner()

PROJECT = "projects/friendly-fox/infinite-worlds"
SLUG = "friendly-fox-infinite-worlds"
BRANCH = f"agents/{SLUG}"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _make_vault_repo(root: Path) -> Path:
    """Git-initialised vault with a committed project + gsd-planning dir."""
    vault = root / "vault"
    project_dir = vault / PROJECT
    (project_dir / "gsd-planning").mkdir(parents=True)
    (project_dir / "gsd-planning" / "STATE.md").write_text("# state\n")
    (project_dir / "AGENTS.md").write_text("# agents\n")
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "init")
    return vault


def test_project_slug():
    assert vault_worktree.project_slug(PROJECT) == SLUG
    assert vault_worktree.project_slug("/projects/org/proj/") == "org-proj"
    assert vault_worktree.project_slug("org/proj") == "org-proj"


def test_ensure_creates_worktree_and_branch(tmp_path):
    vault = _make_vault_repo(tmp_path)
    wt = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    assert wt == vault / "worktrees" / SLUG
    assert (wt / PROJECT / "gsd-planning" / "STATE.md").is_file()
    head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == BRANCH
    # Main checkout's branch is untouched
    main_head = _git(vault, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert main_head != BRANCH


def test_ensure_is_idempotent(tmp_path):
    vault = _make_vault_repo(tmp_path)
    first = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    second = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    assert first == second


def test_ensure_adopts_existing_branch(tmp_path):
    """Branch left over from a merged-with---keep or manual flow is re-used."""
    vault = _make_vault_repo(tmp_path)
    _git(vault, "branch", BRANCH)
    wt = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == BRANCH


def test_ensure_raises_outside_git_repo(tmp_path):
    not_repo = tmp_path / "vault"
    not_repo.mkdir()
    with pytest.raises(RuntimeError, match="not a git repository"):
        vault_worktree.ensure_vault_worktree(not_repo, PROJECT)


def test_init_container_wires_planning_into_worktree(tmp_path, monkeypatch):
    """Container init points .planning at the vault worktree, not the main checkout."""
    vault = _make_vault_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()

    from mnemosyne_cli.commands import init as init_cmd

    with patch(
        "mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault
    ):
        init_cmd.run(project=PROJECT, container=True, target=workspace)

    planning = workspace / ".planning"
    assert planning.is_symlink()
    resolved = planning.resolve()
    assert resolved == (vault / "worktrees" / SLUG / PROJECT / "gsd-planning").resolve()


def test_init_container_falls_back_when_vault_not_repo(tmp_path, monkeypatch):
    """Non-git vault (e.g. test fixtures, degraded mounts) wires to the main checkout."""
    vault = tmp_path / "vault"
    project_dir = vault / PROJECT
    (project_dir / "gsd-planning").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()

    from mnemosyne_cli.commands import init as init_cmd

    with patch(
        "mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=vault
    ):
        init_cmd.run(project=PROJECT, container=True, target=workspace)

    planning = workspace / ".planning"
    assert planning.is_symlink()
    assert planning.resolve() == (project_dir / "gsd-planning").resolve()


def test_vault_merge_brings_commits_and_cleans_up(tmp_path):
    vault = _make_vault_repo(tmp_path)
    wt = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    # Agent commits a planning artifact in the worktree
    (wt / PROJECT / "gsd-planning" / "PLAN.md").write_text("# plan\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "plan")

    with patch(
        "mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault
    ):
        result = runner.invoke(app, ["vault", "merge", SLUG])

    assert result.exit_code == 0, result.output
    # Commit landed on the main checkout's branch
    assert (vault / PROJECT / "gsd-planning" / "PLAN.md").is_file()
    # Worktree and branch removed
    assert not (vault / "worktrees" / SLUG).exists()
    branches = _git(vault, "branch", "--list", BRANCH).stdout.strip()
    assert branches == ""


def test_vault_merge_refuses_dirty_worktree(tmp_path):
    vault = _make_vault_repo(tmp_path)
    wt = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    (wt / PROJECT / "gsd-planning" / "WIP.md").write_text("wip\n")

    with patch(
        "mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault
    ):
        result = runner.invoke(app, ["vault", "merge", SLUG])

    assert result.exit_code == 1
    assert (vault / "worktrees" / SLUG).exists()


def test_vault_worktrees_lists_unmerged(tmp_path):
    vault = _make_vault_repo(tmp_path)
    wt = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    (wt / PROJECT / "gsd-planning" / "PLAN.md").write_text("# plan\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "plan")

    with patch(
        "mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault
    ):
        # Wide virtual terminal so rich doesn't wrap the slug mid-word
        result = runner.invoke(app, ["vault", "worktrees"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert SLUG in result.output
    assert BRANCH in result.output


def test_repair_flips_paths_after_vault_move(tmp_path):
    """Simulates the host<->container mount-path flip: move the whole vault,
    repair, and confirm the worktree is usable at the new location."""
    vault = _make_vault_repo(tmp_path / "side-a")
    wt = vault_worktree.ensure_vault_worktree(vault, PROJECT)
    (wt / PROJECT / "gsd-planning" / "PLAN.md").write_text("# plan\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "plan")

    moved = tmp_path / "side-b" / "vault"
    moved.parent.mkdir()
    vault.rename(moved)

    entries = vault_worktree.list_agent_worktrees(moved)  # repairs first
    assert len(entries) == 1
    moved_wt = Path(entries[0]["worktree"])
    assert moved_wt == moved / "worktrees" / SLUG
    # Git operations work in the repaired worktree
    status = subprocess.run(
        ["git", "-C", str(moved_wt), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    assert status.returncode == 0
