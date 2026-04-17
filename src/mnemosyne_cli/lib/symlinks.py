"""Symlink creation and validation logic."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Filename of the per-project skill allowlist inside claude-config/.
SKILLS_YAML_FILENAME = "skills.yaml"


# ---------------------------------------------------------------------------
# Legacy types — preserved for doctor.py callers on the migration path
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Result of a symlink or configuration check."""

    ok: bool
    message: str
    fix_cmd: str | None = None


# ---------------------------------------------------------------------------
# skills.yaml parser
# ---------------------------------------------------------------------------


def parse_skills_list(skills_yaml_path: Path) -> list[str]:
    """Parse a ``claude-config/skills.yaml`` file and return skill names.

    Returns an empty list if the file does not exist.
    Raises ``ValueError`` if the file is malformed (missing ``skills:`` key,
    non-list value, or non-string entry).

    PyYAML is not available in this project's dependencies, so we use a
    minimal hand-rolled parser.  The format is intentionally simple — a
    ``skills:`` key followed by ``  - <name>`` list entries — which makes a
    hand-rolled parser safe here.

    Expected format::

        # claude-config/skills.yaml — per-project skill allowlist
        skills:
          - mnemosyne-plan
          - mnemosyne-execute
          - obsidian-skills
    """
    if not skills_yaml_path.exists():
        return []

    text = skills_yaml_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Strip comment-only lines and blank lines for parsing purposes, but keep
    # line numbers for error messages.
    in_skills_block = False
    found_skills_key = False
    skills: list[str] = []

    for lineno, raw_line in enumerate(lines, start=1):
        # Strip inline comments and trailing whitespace
        line = raw_line.split("#")[0].rstrip()

        if not line:
            # A blank (or comment-only) line outside the skills block is fine.
            # Inside the block it ends the block.
            if in_skills_block:
                in_skills_block = False
            continue

        if line.startswith("skills:"):
            found_skills_key = True
            remainder = line[len("skills:"):].strip()
            if remainder == "[]" or remainder == "":
                # Either an empty inline list or block-style list follows
                in_skills_block = True
                if remainder == "[]":
                    in_skills_block = False  # empty list — no items follow
                continue
            raise ValueError(
                f"skills.yaml line {lineno}: unexpected value after 'skills:' — "
                f"expected a block list or empty list, got: {remainder!r}"
            )

        if in_skills_block:
            if line.startswith("  -") or line.startswith("- "):
                # List item line — strip leading dash and whitespace
                entry = line.lstrip().lstrip("-").strip()
                if not entry:
                    raise ValueError(
                        f"skills.yaml line {lineno}: empty list entry"
                    )
                # Validate: must be a non-empty string without YAML structure
                if ":" in entry and not entry.startswith('"') and not entry.startswith("'"):
                    # Looks like a map entry, not a string
                    raise ValueError(
                        f"skills.yaml line {lineno}: expected a string entry, "
                        f"got what looks like a mapping: {entry!r}"
                    )
                # Numeric-looking entries are invalid (per spec: must be strings)
                try:
                    float(entry)
                    raise ValueError(
                        f"skills.yaml line {lineno}: expected a string skill name, "
                        f"got a number: {entry!r}"
                    )
                except ValueError as exc:
                    if "skills.yaml" in str(exc):
                        raise
                    # float() raised ValueError — good, it's not a number
                skills.append(entry)
            elif not line[0].isspace():
                # A new top-level key — we've left the skills block
                in_skills_block = False

    if not found_skills_key:
        raise ValueError(
            f"skills.yaml: missing required 'skills:' key in {skills_yaml_path}"
        )

    return skills


# ---------------------------------------------------------------------------
# Bundle expansion
# ---------------------------------------------------------------------------


def expand_skill_names(skill_names: list[str], vault_path: Path) -> list[str]:
    """Expand a list of skill names, resolving bundles to their sub-skills.

    A bundle is a skill directory that contains a ``skills/`` subdirectory
    with at least one child directory containing a ``SKILL.md`` file.  When a
    bundle is detected, only the sub-skill names are yielded — the bundle name
    itself is not included in the output.

    If a skill directory does not exist in the vault, the name is passed
    through unchanged (missing dirs are not an error at expand time).

    Raises ``ValueError`` if two source entries would both produce the same
    resolved skill name (collision).

    Returns a flat list of resolved skill names ready for symlink creation.
    """
    skills_root = vault_path / "agents" / "skills"
    resolved: list[str] = []
    # Map from resolved name -> the source entry that produced it, for
    # collision error messages.
    source_of: dict[str, str] = {}

    for name in skill_names:
        skill_dir = skills_root / name
        sub_skills_dir = skill_dir / "skills"

        if sub_skills_dir.is_dir():
            # Candidate bundle — check for SKILL.md children
            sub_names = [
                child.name
                for child in sorted(sub_skills_dir.iterdir())
                if child.is_dir() and (child / "SKILL.md").exists()
            ]
            if sub_names:
                # It's a bundle — expand to sub-skill names
                for sub in sub_names:
                    _register(sub, name, resolved, source_of)
                continue

        # Flat skill (or missing dir, or bundle with no SKILL.md children)
        _register(name, name, resolved, source_of)

    return resolved


def _register(
    resolved_name: str,
    source_entry: str,
    resolved: list[str],
    source_of: dict[str, str],
) -> None:
    """Add *resolved_name* to the list, raising on collision."""
    if resolved_name in source_of:
        raise ValueError(
            f"Skill name collision: '{resolved_name}' produced by both "
            f"'{source_of[resolved_name]}' and '{source_entry}'"
        )
    source_of[resolved_name] = source_entry
    resolved.append(resolved_name)


# ---------------------------------------------------------------------------
# Directory-symlink primitives (new layout: .claude/skills/<name>/)
# ---------------------------------------------------------------------------


def create_skill_symlink(cwd: Path, skill_name: str, vault_path: Path) -> None:
    """Create ``.claude/skills/<skill_name>`` in *cwd* as a directory symlink.

    Points at ``vault_path/agents/skills/<skill_name>``.

    - Creates ``.claude/skills/`` parent if absent.
    - If a symlink already exists: unlink and re-create (idempotent re-run).
    - If a real (non-symlink) directory exists: raise ``FileExistsError``.
    - The vault skill directory does not need to exist at creation time.
    """
    dest = cwd / ".claude" / "skills" / skill_name
    target = vault_path / "agents" / "skills" / skill_name

    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        raise FileExistsError(
            f"{dest} already exists as a real directory. "
            "Remove it manually before creating the skill symlink."
        )

    dest.symlink_to(target)


def check_skill_symlink(cwd: Path, skill_name: str, vault_path: Path) -> CheckResult:
    """Return a ``CheckResult`` for ``.claude/skills/<skill_name>`` in *cwd*.

    Delegates to ``check_symlink`` with:
    - ``name = cwd / ".claude" / "skills" / skill_name``
    - ``expected_target = vault_path / "agents" / "skills" / skill_name``
    """
    name = cwd / ".claude" / "skills" / skill_name
    expected_target = vault_path / "agents" / "skills" / skill_name
    return check_symlink(name, expected_target)


# ---------------------------------------------------------------------------
# Legacy primitives — unchanged; doctor.py callers depend on these
# ---------------------------------------------------------------------------


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
