"""Vault-wide skill discovery (D-10, D-13).

Walks two roots:
1. ``$MNEMOSYNE_VAULT/agents/skills/`` — the primary skills tree (flat + one-level
   nested collections such as the legacy obsidian-skills/ wrapper).
2. ``$MNEMOSYNE_VAULT/agents/vendored/`` — vendored packages that ship their own
   skills (e.g. ``anvil-agent-references/skills/*``, ``obsidian-skills/skills/*``
   after the D-13 submodule retirement).  Each top-level entry under
   ``agents/vendored/`` is treated as a collection: its ``skills/`` subdir is
   walked one level deep.

Does NOT recurse beyond depth 2 within each root.  The flat + one-level-nested
coverage matches the production layout audited in 33.1-RESEARCH §Q5/Q10
(2026-05-18) and extended in Phase 54 D-13 / Pitfall 3.

Skill names are de-duplicated (first occurrence wins when both roots yield the
same name) and the result is sorted for deterministic symlink creation.
"""

from __future__ import annotations

from pathlib import Path


def _walk_skills_root(root: Path) -> list[tuple[str, Path]]:
    """Walk a single skills root and return (name, dir) pairs.

    Handles two layouts:
    - Flat: ``<root>/<skill-name>/SKILL.md`` → yields ``(skill-name, dir)``.
    - Nested collection: ``<root>/<collection>/skills/<skill-name>/SKILL.md``
      → yields each inner ``(skill-name, dir)``.  The collection wrapper itself
      is NOT yielded.
    """
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
    return out


def discover_vault_skills(vault_path: Path) -> list[tuple[str, Path]]:
    """Yield (skill_name, skill_dir) for every SKILL.md-bearing directory
    under vault/agents/skills/ and vault/agents/vendored/, walking one level
    into collections.

    Skill names are de-duplicated (first occurrence wins) and the result is
    sorted for deterministic symlink creation.
    """
    roots = [
        vault_path / "agents" / "skills",
        vault_path / "agents" / "vendored",
    ]
    seen: dict[str, Path] = {}
    for root in roots:
        for name, skill_dir in _walk_skills_root(root):
            if name not in seen:
                seen[name] = skill_dir
    return sorted(seen.items(), key=lambda pair: pair[0])
