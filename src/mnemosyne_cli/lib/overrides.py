"""Local-override patterns: keep an upstream-tracked path out of our
working tree while presenting our own content there locally.

The pattern combines three things:
1. The local file (typically a symlink into the vault).
2. A sparse-checkout exclusion so `git pull` doesn't overwrite it.
3. assume-unchanged so the typechange stays out of `git status` and
   can't be staged accidentally. (skip-worktree silently fails when
   the working tree differs from the index, so it can't substitute
   for assume-unchanged once the local content is in place.)

Combined with the project's existing .git/info/exclude entry, this
keeps upstream content out of our agent context AND keeps the local
override from leaking back upstream.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def is_tracked(cwd: Path, path: str) -> bool:
    """Return True if *path* is tracked by upstream in the repo at *cwd*."""
    return subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "--error-unmatch", path],
        capture_output=True,
    ).returncode == 0


def has_sparse_exclusion(git_dir: Path, path: str) -> bool:
    """Return True if .git/info/sparse-checkout excludes *path*."""
    sparse_file = git_dir / "info" / "sparse-checkout"
    if not sparse_file.exists():
        return False
    return f"!/{path}" in sparse_file.read_text().splitlines()


def has_assume_unchanged(cwd: Path, path: str) -> bool:
    """Return True if *path* has the assume-unchanged or skip-worktree flag.

    `git ls-files -v` flag chars (status × case):
        H/h = cached            (lowercase = also assume-unchanged)
        S/s = skip-worktree     (lowercase = also assume-unchanged)
    Either flag suffices for our purpose: the path stays out of `git
    status` typechange noise and out of `git add .`.
    """
    result = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "-v", path],
        capture_output=True, text=True,
    )
    if not result.stdout:
        return False
    return result.stdout[0] in ("h", "S", "s")


def diagnose_claude_md_override(cwd: Path, git_dir: Path) -> list[str]:
    """Return a list of problem strings for the CLAUDE.md override.

    Empty list means the override is correctly in place. Caller is
    expected to have already verified that the override applies
    (AGENTS.md is a symlink and CLAUDE.md is tracked upstream).
    """
    problems: list[str] = []
    claude_md = cwd / "CLAUDE.md"
    if not claude_md.is_symlink() or os.readlink(claude_md) != "AGENTS.md":
        problems.append("not a symlink to AGENTS.md")
    if not has_sparse_exclusion(git_dir, "CLAUDE.md"):
        problems.append("no sparse-checkout exclusion")
    if not has_assume_unchanged(cwd, "CLAUDE.md"):
        problems.append("not assume-unchanged")
    return problems


def apply_claude_md_override(cwd: Path, git_dir: Path) -> None:
    """Apply the full CLAUDE.md override pattern in *cwd*.

    Steps:
    1. Unstage any pending typechange so it isn't carried forward.
    2. Remove the local file so sparse-checkout reapply can mark the
       path SKIP_WORKTREE cleanly before we replace it.
    3. Ensure sparse-checkout is enabled with the !/CLAUDE.md pattern.
    4. Reapply sparse-checkout.
    5. Re-create the symlink to AGENTS.md.
    6. Mark the index entry assume-unchanged.
    """
    claude_md = cwd / "CLAUDE.md"

    # 1. Unstage any pending typechange.
    subprocess.run(
        ["git", "-C", str(cwd), "restore", "--staged", "CLAUDE.md"],
        capture_output=True,
    )
    # 2. Remove the local file.
    if claude_md.is_symlink() or claude_md.exists():
        claude_md.unlink()
    # 3. Sparse-checkout config + pattern.
    subprocess.run(
        ["git", "-C", str(cwd), "config", "core.sparseCheckout", "true"],
        check=True,
    )
    sparse_file = git_dir / "info" / "sparse-checkout"
    sparse_file.parent.mkdir(parents=True, exist_ok=True)
    lines = sparse_file.read_text().splitlines() if sparse_file.exists() else []
    if not lines:
        lines = ["/*"]
    if "!/CLAUDE.md" not in lines:
        lines.append("!/CLAUDE.md")
    sparse_file.write_text("\n".join(lines) + "\n")
    # 4. Reapply.
    subprocess.run(
        ["git", "-C", str(cwd), "sparse-checkout", "reapply"],
        check=True,
    )
    # 5. Re-create the symlink.
    claude_md.symlink_to("AGENTS.md")
    # 6. Pin with assume-unchanged.
    subprocess.run(
        ["git", "-C", str(cwd), "update-index",
         "--assume-unchanged", "CLAUDE.md"],
        check=True,
    )
