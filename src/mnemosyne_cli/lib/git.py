"""Git operations: exclusions, merge drivers, fetch, rev-list."""

from __future__ import annotations

import subprocess
from pathlib import Path


def get_git_dir(cwd: Path) -> Path:
    """Return the .git directory path for the repo at *cwd*."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    git_dir = result.stdout.strip()
    # git rev-parse --git-dir can return an absolute or relative path
    p = Path(git_dir)
    if p.is_absolute():
        return p
    return (cwd / p).resolve()


def check_git_exclusion(entry: str, git_dir: Path) -> bool:
    """Return True if *entry* is already listed in .git/info/exclude."""
    exclude_file = git_dir / "info" / "exclude"
    if not exclude_file.exists():
        return False
    return entry in exclude_file.read_text().splitlines()


def add_git_exclusion(entry: str, git_dir: Path) -> None:
    """Add *entry* to .git/info/exclude idempotently."""
    exclude_file = git_dir / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    exclude_file.touch()
    lines = exclude_file.read_text().splitlines()
    if entry not in lines:
        with open(exclude_file, "a") as f:
            f.write(f"{entry}\n")


def register_merge_drivers(vault_path: Path) -> None:
    """Register gsd-state and gsd-roadmap custom merge drivers in the vault repo."""
    drivers = [
        (
            "gsd-state",
            "GSD STATE.md merge driver",
            "mnemosyne merge-driver state %O %A %B",
        ),
        (
            "gsd-roadmap",
            "GSD ROADMAP.md merge driver",
            "mnemosyne merge-driver roadmap %O %A %B",
        ),
    ]
    for key, name, driver in drivers:
        subprocess.run(
            ["git", "config", f"merge.{key}.name", name],
            cwd=vault_path,
            check=True,
        )
        subprocess.run(
            ["git", "config", f"merge.{key}.driver", driver],
            cwd=vault_path,
            check=True,
        )


def fetch_origin(repo_path: Path) -> None:
    """Run git fetch origin --quiet in *repo_path*. Errors are silently ignored."""
    subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "origin", "--quiet"],
        capture_output=True,
    )


def get_behind_ahead(repo_path: Path, branch: str = "main") -> tuple[int, int]:
    """Return (behind, ahead) commit counts relative to origin/<branch>.

    Returns (0, 0) if the comparison cannot be made (e.g. no remote tracking).
    """
    def _count(spec: str) -> int:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", spec],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return 0
        try:
            return int(result.stdout.strip())
        except ValueError:
            return 0

    behind = _count(f"{branch}..origin/{branch}")
    ahead = _count(f"origin/{branch}..{branch}")
    return behind, ahead


def list_worktrees(repo_path: Path) -> list[dict[str, str]]:
    """Return worktrees as dicts with 'worktree', 'HEAD', 'branch' keys.

    branch is '(detached)' for detached HEAD worktrees.
    Always uses --porcelain for stable output.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                worktrees.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["worktree"] = line[9:]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = "(detached)"
    if current:
        worktrees.append(current)
    return worktrees


def is_branch_merged_to_main(repo_path: Path, branch: str, main: str = "main") -> bool:
    """Return True if branch has been merged into main."""
    result = subprocess.run(
        ["git", "branch", "--merged", main, "--list", branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def worktree_add(
    repo_path: Path, worktree_path: Path, branch: str, new_branch: bool = False
) -> None:
    """Create a git worktree.

    new_branch=True: git worktree add -b branch path.
    new_branch=False: git worktree add path branch.
    """
    if new_branch:
        cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path)]
    else:
        cmd = ["git", "worktree", "add", str(worktree_path), branch]
    subprocess.run(cmd, cwd=repo_path, check=True)


def worktree_remove(repo_path: Path, worktree_path: Path, force: bool = False) -> None:
    """Remove a registered git worktree."""
    cmd = ["git", "worktree", "remove", str(worktree_path)]
    if force:
        cmd.append("--force")
    subprocess.run(cmd, cwd=repo_path, check=True)
