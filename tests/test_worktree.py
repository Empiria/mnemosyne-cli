"""Unit tests for git worktree library functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mnemosyne_cli.lib import git as lib_git


PORCELAIN_TWO_WORKTREES = """\
worktree /repo
HEAD abc123
branch refs/heads/main

worktree /repo/worktrees/feature-x
HEAD def456
branch refs/heads/feature-x

"""

PORCELAIN_WITH_DETACHED = """\
worktree /repo
HEAD abc123
branch refs/heads/main

worktree /repo/worktrees/detached-wt
HEAD def456
detached

"""

PORCELAIN_SINGLE = """\
worktree /repo
HEAD abc123
branch refs/heads/main

"""


def _make_run_result(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# list_worktrees
# ---------------------------------------------------------------------------


def test_list_worktrees_porcelain() -> None:
    """list_worktrees returns two dicts when porcelain output has two blocks."""
    with patch("subprocess.run", return_value=_make_run_result(PORCELAIN_TWO_WORKTREES)):
        worktrees = lib_git.list_worktrees(Path("/repo"))

    assert len(worktrees) == 2
    assert worktrees[0]["worktree"] == "/repo"
    assert worktrees[0]["HEAD"] == "abc123"
    assert worktrees[0]["branch"] == "main"

    assert worktrees[1]["worktree"] == "/repo/worktrees/feature-x"
    assert worktrees[1]["HEAD"] == "def456"
    assert worktrees[1]["branch"] == "feature-x"


def test_list_worktrees_detached() -> None:
    """list_worktrees returns branch='(detached)' for detached HEAD worktrees."""
    with patch("subprocess.run", return_value=_make_run_result(PORCELAIN_WITH_DETACHED)):
        worktrees = lib_git.list_worktrees(Path("/repo"))

    assert len(worktrees) == 2
    assert worktrees[1]["branch"] == "(detached)"


def test_list_worktrees_empty() -> None:
    """list_worktrees returns one entry for a repo with only the main worktree."""
    with patch("subprocess.run", return_value=_make_run_result(PORCELAIN_SINGLE)):
        worktrees = lib_git.list_worktrees(Path("/repo"))

    assert len(worktrees) == 1
    assert worktrees[0]["worktree"] == "/repo"


# ---------------------------------------------------------------------------
# is_branch_merged_to_main
# ---------------------------------------------------------------------------


def test_branch_merge_detection_merged() -> None:
    """is_branch_merged_to_main returns True when git outputs the branch name."""
    branch = "feature-x"
    with patch("subprocess.run", return_value=_make_run_result(f"  {branch}\n")):
        result = lib_git.is_branch_merged_to_main(Path("/repo"), branch)

    assert result is True


def test_branch_merge_detection_not_merged() -> None:
    """is_branch_merged_to_main returns False when git output is empty."""
    with patch("subprocess.run", return_value=_make_run_result("")):
        result = lib_git.is_branch_merged_to_main(Path("/repo"), "feature-x")

    assert result is False


# ---------------------------------------------------------------------------
# worktree_add
# ---------------------------------------------------------------------------


def test_worktree_add_new_branch() -> None:
    """worktree_add with new_branch=True calls git worktree add -b branch path."""
    with patch("subprocess.run") as mock_run:
        lib_git.worktree_add(
            Path("/repo"),
            Path("/repo/worktrees/feature-x"),
            "feature-x",
            new_branch=True,
        )

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "worktree", "add", "-b", "feature-x", "/repo/worktrees/feature-x"
    ]


def test_worktree_add_existing_branch() -> None:
    """worktree_add with new_branch=False calls git worktree add path branch."""
    with patch("subprocess.run") as mock_run:
        lib_git.worktree_add(
            Path("/repo"),
            Path("/repo/worktrees/feature-x"),
            "feature-x",
            new_branch=False,
        )

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "worktree", "add", "/repo/worktrees/feature-x", "feature-x"
    ]


# ---------------------------------------------------------------------------
# worktree_remove
# ---------------------------------------------------------------------------


def test_worktree_remove() -> None:
    """worktree_remove calls git worktree remove path."""
    with patch("subprocess.run") as mock_run:
        lib_git.worktree_remove(Path("/repo"), Path("/repo/worktrees/feature-x"))

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "worktree", "remove", "/repo/worktrees/feature-x"]


def test_worktree_remove_force() -> None:
    """worktree_remove with force=True appends --force to the command."""
    with patch("subprocess.run") as mock_run:
        lib_git.worktree_remove(
            Path("/repo"), Path("/repo/worktrees/feature-x"), force=True
        )

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "worktree", "remove", "/repo/worktrees/feature-x", "--force"
    ]
