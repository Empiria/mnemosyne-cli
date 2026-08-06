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

import os
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


def _user_skills_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "skills"


def classify_user_skills(
    vault_path: Path, home: Path | None = None
) -> dict[str, list[tuple[str, ...]]]:
    """Compare ~/.claude/skills/ against the vault's discoverable skills.

    Returns a dict with five buckets, every entry a tuple starting with the
    skill name:

    - ``ok``          (name,)                      symlink points at the right dir
    - ``missing``     (name, expected)             discovered, but no entry present
    - ``repoint``     (name, current, expected)    symlink present, wrong or dangling target
    - ``stale``       (name, current)              not discovered, but points into the vault
    - ``foreign``     (name, current)              not discovered, points outside the vault

    Real directories and files are never classified — ``~/.claude/skills/``
    also holds locally installed skills (the ``gsd-*`` tree), and those are not
    ours to reconcile.
    """
    discovered = dict(discover_vault_skills(vault_path))
    skills_dir = _user_skills_dir(home)
    vault_resolved = vault_path.resolve()

    entries: dict[str, Path] = {}
    if skills_dir.is_dir():
        entries = {p.name: p for p in sorted(skills_dir.iterdir()) if p.is_symlink()}

    out: dict[str, list[tuple[str, ...]]] = {
        "ok": [],
        "missing": [],
        "repoint": [],
        "stale": [],
        "foreign": [],
    }

    for name, expected in discovered.items():
        link = entries.get(name)
        if link is None:
            # A real directory of the same name is a local install; leave it be.
            if (skills_dir / name).exists():
                continue
            out["missing"].append((name, str(expected)))
            continue
        current = os.readlink(link)
        if Path(current) == expected:
            out["ok"].append((name,))
        else:
            out["repoint"].append((name, current, str(expected)))

    for name, link in entries.items():
        if name in discovered:
            continue
        current = os.readlink(link)
        try:
            inside = vault_resolved in Path(current).resolve().parents
        except OSError:
            inside = str(current).startswith(str(vault_resolved))
        out["stale" if inside else "foreign"].append((name, current))

    return out


def sync_user_skills(vault_path: Path, home: Path | None = None) -> dict[str, list[str]]:
    """Reconcile ~/.claude/skills/ with the vault's discoverable skills.

    Creates missing symlinks, repoints wrong or dangling ones, and removes
    symlinks that point into the vault but no longer correspond to a skill.
    Entries pointing outside the vault are reported and left alone, as are real
    directories and files.

    Returns the names acted on, keyed by ``linked``, ``repointed`` and
    ``removed``. ``skipped`` carries anything that raised.
    """
    report = classify_user_skills(vault_path, home)
    skills_dir = _user_skills_dir(home)
    skills_dir.mkdir(parents=True, exist_ok=True)

    done: dict[str, list[str]] = {
        "linked": [],
        "repointed": [],
        "removed": [],
        "skipped": [],
    }

    def _relink(name: str, expected: str, bucket: str) -> None:
        link = skills_dir / name
        try:
            if link.is_symlink():
                link.unlink()
            link.symlink_to(Path(expected), target_is_directory=True)
            done[bucket].append(name)
        except OSError as exc:
            done["skipped"].append(f"{name}: {exc}")

    for name, expected in report["missing"]:
        _relink(name, expected, "linked")

    for name, _current, expected in report["repoint"]:
        _relink(name, expected, "repointed")

    for name, _current in report["stale"]:
        link = skills_dir / name
        try:
            if link.is_symlink():
                link.unlink()
                done["removed"].append(name)
        except OSError as exc:
            done["skipped"].append(f"{name}: {exc}")

    return done
