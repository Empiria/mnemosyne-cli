"""Tech stack parsing and vault rule discovery."""

from __future__ import annotations

import re
from pathlib import Path

# Match **Tech stack:** or Tech stack: with optional bold markers
TECH_STACK_RE = re.compile(r'^\*{0,2}Tech stack:\*{0,2}\s*(.+)', re.IGNORECASE | re.MULTILINE)


def parse_tech_stack(agents_md: Path) -> list[str]:
    """Parse technology names from the **Tech stack:** line in AGENTS.md.

    Returns a list of technology slugs (e.g. ["anvil", "python", "github"]).
    Returns an empty list if the line is missing, empty, or contains
    placeholder text from the template.
    """
    if not agents_md.exists():
        return []

    content = agents_md.read_text()
    match = TECH_STACK_RE.search(content)
    if not match:
        return []

    raw = match.group(1).strip()

    # Skip template placeholder
    if raw.startswith("[") or "subdirectory names" in raw.lower():
        return []

    techs = [t.strip() for t in raw.split(",") if t.strip()]
    return techs


def discover_tech_rules(vault_path: Path, tech: str) -> dict[str, Path]:
    """Discover rule symlink targets for a technology.

    Returns a dict mapping rule filename -> absolute vault path for:
    - index.md (standards root note, if exists)
    - Each file in decision/ (always relevant, small)
    - learning-manifest.md (lookup table, if exists)
    """
    rules: dict[str, Path] = {}
    tech_dir = vault_path / "technologies" / tech

    if not tech_dir.is_dir():
        return rules

    # Index (standards root note)
    index = tech_dir / "index.md"
    if index.exists():
        rules[f"{tech}.md"] = index

    # Decision records
    decision_dir = tech_dir / "decision"
    if decision_dir.is_dir():
        for note in sorted(decision_dir.glob("*.md")):
            rules[f"{tech}-decision-{note.name}"] = note

    # Learning manifest
    manifest = tech_dir / "learning-manifest.md"
    if manifest.exists():
        rules[f"{tech}-learnings.md"] = manifest

    return rules
