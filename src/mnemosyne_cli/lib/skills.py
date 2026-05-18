"""Vault-wide skill discovery (D-10).

Walks $MNEMOSYNE_VAULT/agents/skills/ one level deep; for each top-level
entry that has a SKILL.md it yields the (name, dir) tuple. For collection
dirs (no top-level SKILL.md but a skills/ subdir — e.g. obsidian-skills/),
walks one MORE level into skills/* and yields each <name>/SKILL.md-bearing
child as (name, dir).

Does NOT recurse beyond depth 2. The flat + one-level-nested coverage matches
the production layout audited in 33.1-RESEARCH §Q5/Q10 (2026-05-18).
"""

from __future__ import annotations

from pathlib import Path


def discover_vault_skills(vault_path: Path) -> list[tuple[str, Path]]:
    """Yield (skill_name, skill_dir) for every SKILL.md-bearing directory
    under vault/agents/skills/, walking one level into collections.

    Returns sorted list; deterministic order for symlink creation.
    """
    root = vault_path / "agents" / "skills"
    if not root.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "SKILL.md").exists():
            out.append((entry.name, entry))
            continue
        nested = entry / "skills"
        if nested.is_dir():
            for sub in sorted(nested.iterdir()):
                if sub.is_dir() and (sub / "SKILL.md").exists():
                    out.append((sub.name, sub))
    return sorted(out, key=lambda pair: pair[0])
