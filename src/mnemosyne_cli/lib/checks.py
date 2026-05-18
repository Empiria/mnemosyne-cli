"""Shared check helpers used by mnemosyne init --container self-verify (D-23)
and mnemosyne doctor --container (D-21, D-22).

Centralised here to avoid a circular import between commands/init.py and
commands/doctor.py — both import from lib/checks.py instead of each other.
RESEARCH §Q6/Q7 specifies this module shape.

CheckResult is re-exported from lib.symlinks (the existing single source of
truth) so doctor.py, init.py, and this module all share ONE dataclass —
no duck-typing, no parallel definitions.

All check_* functions are pure — no side effects beyond filesystem reads
and env-var reads. They return CheckResult dataclasses; presentation lives
in the calling command.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from mnemosyne_cli.lib.symlinks import CheckResult  # noqa: F401  (re-export)


# Status file path written by agents/scion-template/hooks/post-start.sh after
# the mnemosyne init --container invocation. Module-level so tests can
# monkeypatch with monkeypatch.setattr("mnemosyne_cli.lib.checks._INIT_STATUS_PATH", ...).
_INIT_STATUS_PATH = Path("/tmp/mnemosyne-init.status")


def check_mnemosyne_on_path() -> CheckResult:
    path = shutil.which("mnemosyne")
    if path is None:
        return CheckResult(
            ok=False,
            message="mnemosyne not on PATH",
            fix_cmd="uv pip install git+https://github.com/Empiria/mnemosyne-cli.git",
        )
    return CheckResult(ok=True, message=f"mnemosyne on PATH ({path})")


def check_gsd_tools_on_path() -> CheckResult:
    path = shutil.which("gsd-tools")
    if path is None:
        return CheckResult(
            ok=False,
            message="gsd-tools not on PATH",
            fix_cmd="npm install -g get-shit-done-cc",
        )
    return CheckResult(ok=True, message=f"gsd-tools on PATH ({path})")


def check_user_skills_populated(vault_path: Path) -> CheckResult:
    skills_dir = Path.home() / ".claude" / "skills"
    if not skills_dir.is_dir():
        return CheckResult(
            ok=False,
            message="~/.claude/skills/ does not exist — vault skill wiring did not run",
            fix_cmd="mnemosyne init --container",
        )
    vault_resolved = vault_path.resolve()
    valid = 0
    for entry in skills_dir.iterdir():
        if not entry.is_symlink():
            continue
        try:
            resolved = entry.resolve(strict=True)
        except OSError:
            continue
        if vault_resolved in resolved.parents and "agents/skills" in str(resolved):
            valid += 1
    if valid == 0:
        return CheckResult(
            ok=False,
            message="~/.claude/skills/ has no symlinks resolving to vault skills",
            fix_cmd="mnemosyne init --container",
        )
    return CheckResult(
        ok=True,
        message=f"{valid} vault skills linked under ~/.claude/skills/",
    )


def check_workspace_planning(target: Path, vault_path: Path) -> CheckResult:
    planning = target / ".planning"
    if not planning.is_symlink():
        return CheckResult(
            ok=False,
            message=f"{planning} is not a symlink — workspace not wired",
            fix_cmd="mnemosyne init --container",
        )
    try:
        resolved = planning.resolve(strict=True)
    except OSError as exc:
        return CheckResult(
            ok=False,
            message=f"{planning} symlink target unresolvable: {exc}",
            fix_cmd="mnemosyne init --container",
        )
    projects_root = (vault_path / "projects").resolve()
    if projects_root not in resolved.parents:
        return CheckResult(
            ok=False,
            message=f"{planning} resolves outside vault/projects/: {resolved}",
            fix_cmd=None,
        )
    return CheckResult(ok=True, message=f"{planning} -> {resolved}")


def check_required_env_vars() -> CheckResult:
    workspace = os.environ.get("MNEMOSYNE_WORKSPACE")
    project = os.environ.get("MNEMOSYNE_PROJECT")
    missing = []
    if not workspace:
        missing.append("MNEMOSYNE_WORKSPACE")
    if not project:
        missing.append("MNEMOSYNE_PROJECT")
    if missing:
        return CheckResult(
            ok=False,
            message=f"unset env vars: {', '.join(missing)}",
            fix_cmd="See docs/how-to/scion-grove-setup.md §6",
        )
    return CheckResult(
        ok=True,
        message=f"MNEMOSYNE_WORKSPACE={workspace} MNEMOSYNE_PROJECT={project}",
    )


def check_init_status_file() -> CheckResult:
    status_file = _INIT_STATUS_PATH
    if not status_file.exists():
        return CheckResult(
            ok=False,
            message=f"post-start hook status file missing ({status_file}) — hook may not have run",
            fix_cmd=None,
        )
    try:
        rc = int(status_file.read_text().strip())
    except (OSError, ValueError) as exc:
        return CheckResult(
            ok=False,
            message=f"post-start status file unreadable: {exc}",
            fix_cmd=None,
        )
    if rc == 0:
        return CheckResult(ok=True, message="post-start hook init succeeded")
    return CheckResult(
        ok=False,
        message=f"post-start hook init exited with code {rc}",
        fix_cmd="mnemosyne init --container",
    )


def run_container_checks(target: Path, vault_path: Path) -> list[CheckResult]:
    """Run all D-22 checks in order, return ordered list of results."""
    return [
        check_mnemosyne_on_path(),
        check_gsd_tools_on_path(),
        check_user_skills_populated(vault_path),
        check_workspace_planning(target, vault_path),
        check_required_env_vars(),
        check_init_status_file(),
    ]
