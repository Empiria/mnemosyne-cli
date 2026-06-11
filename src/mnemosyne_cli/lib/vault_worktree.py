"""Vault agent worktrees — branch isolation for container agents.

Container agents must never run git operations against the vault's main
checkout: the vault is bind-mounted read-write into every agent container,
so a `git checkout` there switches the branch under the host operator and
every other agent sharing the mount. That was the documented (agents.md
"Branch Workflow") behaviour that stranded planning commits on unmerged
scion/* branches.

Instead, each project gets a dedicated git worktree at

    <vault>/worktrees/<project-slug>/        (branch: agents/<project-slug>)

The worktree lives INSIDE the mounted vault path, so it is host-persisted:
deleting the agent or its container cannot destroy committed or uncommitted
work, and the branch ref lives in the shared object store. The worktree is
project-scoped, not agent-scoped — all agents working on the same project
share it (see technologies/git/worktrees.md in the vault).

Path translation: git worktree metadata records absolute paths, and the
vault is /vault inside containers but somewhere else on the host. Only one
side operates on a worktree at a time (agents in containers; the host when
merging), so each side runs `git worktree repair` on entry to rewrite the
links to its own view of the paths. ensure_vault_worktree() does this for
the container; `mnemosyne vault worktrees` / `mnemosyne vault merge` do it
for the host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORKTREES_DIRNAME = "worktrees"
AGENT_BRANCH_PREFIX = "agents/"


def project_slug(project: str) -> str:
    """projects/friendly-fox/infinite-worlds -> friendly-fox-infinite-worlds."""
    parts = [p for p in project.strip().strip("/").split("/") if p]
    if parts and parts[0] == "projects":
        parts = parts[1:]
    return "-".join(parts)


def worktree_branch(slug: str) -> str:
    return f"{AGENT_BRANCH_PREFIX}{slug}"


def worktree_path(vault_path: Path, slug: str) -> Path:
    return vault_path / WORKTREES_DIRNAME / slug


def _git(vault_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault_path), *args],
        capture_output=True,
        text=True,
    )


def branch_exists(vault_path: Path, branch: str) -> bool:
    return (
        _git(vault_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode
        == 0
    )


def repair_worktrees(vault_path: Path) -> None:
    """Rewrite worktree path metadata for this side of the vault mount.

    Safe to call unconditionally: repair on already-correct paths is a no-op,
    and failures (no worktrees, not a repo) are ignored — callers find out
    via the operations that follow.
    """
    candidates: list[str] = []
    wt_root = vault_path / WORKTREES_DIRNAME
    if wt_root.is_dir():
        candidates = [str(p) for p in sorted(wt_root.iterdir()) if p.is_dir()]
    _git(vault_path, "worktree", "repair", *candidates)


def list_agent_worktrees(vault_path: Path) -> list[dict[str, str]]:
    """Return registered worktrees under <vault>/worktrees/ (repairs first)."""
    repair_worktrees(vault_path)
    result = _git(vault_path, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return []
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines() + [""]:
        if not line:
            if current:
                worktrees.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["worktree"] = line[9:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = "(detached)"
    prefix = str((vault_path / WORKTREES_DIRNAME).resolve())
    return [
        wt
        for wt in worktrees
        if wt.get("worktree", "").startswith(prefix)
    ]


def unmerged_commit_count(vault_path: Path, branch: str) -> int:
    """Commits on branch not reachable from the main checkout's HEAD."""
    result = _git(vault_path, "rev-list", "--count", f"HEAD..{branch}")
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def is_worktree_dirty(worktree: Path) -> bool:
    result = _git(worktree, "status", "--porcelain")
    return bool(result.stdout.strip())


def ensure_vault_worktree(vault_path: Path, project: str) -> Path:
    """Create or adopt the project's agent worktree; return its path.

    Idempotent. Branches from the main checkout's current HEAD on first
    creation; re-uses the agents/<slug> branch if it already exists.
    Raises RuntimeError when the vault is not a git repository or the git
    operations fail — callers fall back to the main checkout with a warning
    (bootstrap must not die on this).
    """
    if not (vault_path / ".git").exists():
        raise RuntimeError(f"vault is not a git repository: {vault_path}")

    slug = project_slug(project)
    if not slug:
        raise RuntimeError(f"cannot derive project slug from {project!r}")
    path = worktree_path(vault_path, slug)
    branch = worktree_branch(slug)

    repair_worktrees(vault_path)
    _git(vault_path, "worktree", "prune")

    registered = {
        str(Path(wt["worktree"]).resolve())
        for wt in list_agent_worktrees(vault_path)
        if "worktree" in wt
    }
    if str(path.resolve()) in registered:
        return path

    if path.exists():
        raise RuntimeError(
            f"{path} exists but is not a registered worktree — "
            f"remove it or run `git -C {vault_path} worktree repair {path}`"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(vault_path, branch):
        cmd = ["worktree", "add", str(path), branch]
    else:
        cmd = ["worktree", "add", "-b", branch, str(path)]
    result = _git(vault_path, *cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return path
