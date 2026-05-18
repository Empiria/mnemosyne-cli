"""Shared worktree symlink wiring.

Used by both the host-side init (commands/init.py) and the container-side
init branch (commands/init.py with --container) to wire a worktree or a
first-time checkout to the vault project.

The function is pure-library: it does not render output, does not call
typer.Exit, and does not prompt. Callers are responsible for presentation
(printing the "Created ..." lines or summary tables).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mnemosyne_cli.lib import symlinks as lib_symlinks
from mnemosyne_cli.lib.embeds import read_embed_targets
from mnemosyne_cli.lib.symlinks import (
    SKILLS_YAML_FILENAME,
    create_skill_symlink,
    expand_skill_names,
    parse_skills_list,
)
from mnemosyne_cli.lib.techstack import discover_tech_rules, parse_tech_stack


def _replicate_assume_unchanged(main_root: Path, target: Path) -> None:
    """Copy git assume-unchanged flags from main_root to target.

    Worktrees don't inherit assume-unchanged flags, so symlinks replacing
    tracked files (e.g. CLAUDE.md) show as typechanges without this.
    No-op on any git failure — this is a nice-to-have, not a correctness
    requirement.
    """
    result = subprocess.run(
        ["git", "ls-files", "-v"],
        cwd=main_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    # Lines starting with lowercase letter have assume-unchanged set
    unchanged = [
        line[2:] for line in result.stdout.splitlines()
        if line and line[0].islower()
    ]
    if unchanged:
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", *unchanged],
            cwd=target,
            capture_output=True,
        )


def setup_worktree_symlinks(
    target: Path,
    vault_path: Path,
    vault_project_path: Path,
    source_checkout: Path | None = None,
) -> None:
    """Wire target to the vault project via the canonical symlink set.

    Creates:
        target/.planning                   -> vault_project_path/gsd-planning
        target/AGENTS.md                   -> vault_project_path/AGENTS.md
        target/CLAUDE.md                   -> AGENTS.md (relative)
        target/.claude/settings.json       -> vault_project_path/claude-config/settings.json
        target/.claude/rules/<file>        -> vault_path / <embed target>
        target/.claude/rules/<file>        -> tech-stack auto-rules (per AGENTS.md)
        target/.claude/skills/<skill>/     -> vault_path/agents/skills/<skill>/

    All symlinks use force=True — existing files (e.g. tracked CLAUDE.md from
    a git checkout) are replaced.

    source_checkout: if given, git assume-unchanged flags set on that
    checkout are replicated onto target. Supplied by the host worktree path
    (the main checkout); left None in container mode.

    Raises: propagates exceptions from symlink creation. Callers wrap for
    presentation.
    """
    # .planning -> main planning dir
    planning_dir = vault_project_path / "gsd-planning"
    lib_symlinks.create_symlink(target / ".planning", planning_dir, force=True)

    # AGENTS.md -> vault project AGENTS.md
    agents_target = vault_project_path / "AGENTS.md"
    if agents_target.exists():
        lib_symlinks.create_symlink(
            target / "AGENTS.md", agents_target, force=True
        )
        # CLAUDE.md -> AGENTS.md (relative symlink)
        lib_symlinks.create_symlink(
            target / "CLAUDE.md", Path("AGENTS.md"), force=True
        )

    # Worktree-specific: replicate assume-unchanged from the main checkout
    if source_checkout is not None:
        _replicate_assume_unchanged(source_checkout, target)

    # .claude/settings.json
    claude_config = vault_project_path / "claude-config"
    settings_target = claude_config / "settings.json"
    if settings_target.exists():
        lib_symlinks.create_symlink(
            target / ".claude" / "settings.json", settings_target, force=True
        )

    # .claude/rules — per-file symlinks from embed notes
    rules_embed_dir = claude_config / "rules"
    if rules_embed_dir.is_dir():
        for filename, target_rel in read_embed_targets(rules_embed_dir).items():
            lib_symlinks.create_symlink(
                target / ".claude" / "rules" / filename,
                vault_path / target_rel,
                force=True,
            )

    # .claude/rules — tech stack auto-rules
    if agents_target.exists():
        for tech in parse_tech_stack(agents_target):
            for filename, target_abs in discover_tech_rules(vault_path, tech).items():
                lib_symlinks.create_symlink(
                    target / ".claude" / "rules" / filename,
                    target_abs,
                    force=True,
                )

    # .claude/skills — directory symlinks from skills.yaml
    skills_yaml = claude_config / SKILLS_YAML_FILENAME
    if skills_yaml.exists():
        raw_names = parse_skills_list(skills_yaml)
        skill_names = expand_skill_names(raw_names, vault_path)
        for name in skill_names:
            create_skill_symlink(target, name, vault_path)
