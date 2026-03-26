"""Symlink creation and validation logic."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    """Result of a symlink or configuration check."""

    ok: bool
    message: str
    fix_cmd: str | None = None


# Maps command filename suffix to the per-project config directory for each tool.
# e.g. "claude-code-command.md" -> ".claude/commands"
TOOL_COMMAND_DIRS: dict[str, str] = {
    "claude-code-command.md": ".claude/commands",
    "opencode-command.md": ".opencode/commands",
}


@dataclass
class AgentCommand:
    """A vault-wide agent command to be linked into a client codebase."""

    agent_name: str
    tool_dir: str  # e.g. ".claude/commands"
    target: Path  # absolute path to the command file in the vault


def discover_agent_commands(vault_path: Path) -> list[AgentCommand]:
    """Scan agents/*/ for tool-specific command files.

    Returns one AgentCommand per (agent, tool) pair found. Agents may have
    command files for multiple tools (e.g. both claude-code-command.md and
    opencode-command.md).
    """
    agents_dir = vault_path / "agents"
    if not agents_dir.is_dir():
        return []

    commands: list[AgentCommand] = []
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        for suffix, tool_dir in TOOL_COMMAND_DIRS.items():
            cmd_file = agent_dir / suffix
            if cmd_file.is_file():
                commands.append(
                    AgentCommand(
                        agent_name=agent_dir.name,
                        tool_dir=tool_dir,
                        target=cmd_file,
                    )
                )
    return commands


def create_symlink(name: Path, target: Path, *, force: bool = False) -> None:
    """Create a symlink at *name* pointing to *target*.

    Creates parent directories as needed. Overwrites an existing symlink at
    *name*. When *force* is True, also replaces a regular file (useful in
    worktrees where git checks out tracked files that should be symlinks).
    Will not replace a directory.
    """
    name.parent.mkdir(parents=True, exist_ok=True)
    if name.is_symlink():
        name.unlink()
    elif force and name.is_file():
        name.unlink()
    name.symlink_to(target)


def check_symlink(name: Path, expected_target: Path) -> CheckResult:
    """Check that *name* is a symlink pointing to *expected_target*.

    Mirrors the logic from scripts/check-project-setup.sh lines 98-120:
    - Missing entirely -> fail with fix command
    - Exists but not a symlink -> fail (no simple fix)
    - Is a symlink but target does not resolve -> fail with fix command
    - Is a symlink and target resolves -> ok
    """
    fix_cmd = f'ln -sfn "{expected_target}" {name}'

    if not name.exists() and not name.is_symlink():
        return CheckResult(
            ok=False,
            message=f"{name} — missing",
            fix_cmd=fix_cmd,
        )

    if not name.is_symlink():
        return CheckResult(
            ok=False,
            message=f"{name} — exists but is not a symlink (regular file/directory)",
            fix_cmd=None,
        )

    if not name.exists():
        return CheckResult(
            ok=False,
            message=f"{name} — symlink exists but target does not resolve",
            fix_cmd=fix_cmd,
        )

    actual = name.readlink()
    # Compare resolved paths so absolute vs relative doesn't cause false failures
    actual_resolved = name.resolve()
    expected_resolved = expected_target.resolve() if expected_target.is_absolute() else expected_target
    if expected_target.is_absolute() and actual_resolved != expected_resolved:
        return CheckResult(
            ok=False,
            message=f"{name} -> {actual} (expected {expected_target})",
            fix_cmd=fix_cmd,
        )
    return CheckResult(
        ok=True,
        message=f"{name} -> {actual}",
        fix_cmd=None,
    )
