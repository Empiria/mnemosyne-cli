"""Learning manifest generation for technology directories."""

from __future__ import annotations

import re
from pathlib import Path

# Match YAML frontmatter block
FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---', re.DOTALL)

# Match tags list in YAML (handles both inline and block style)
TAGS_RE = re.compile(r'^tags:\s*\n((?:\s+-\s+.+\n)*)', re.MULTILINE)
TAGS_INLINE_RE = re.compile(r'^tags:\s*\[([^\]]*)\]', re.MULTILINE)

# Match first H1 heading
H1_RE = re.compile(r'^#\s+(.+)', re.MULTILINE)


def _extract_tags(content: str) -> list[str]:
    """Extract tags from YAML frontmatter."""
    fm_match = FRONTMATTER_RE.match(content)
    if not fm_match:
        return []
    fm = fm_match.group(1)

    # Block style: tags:\n  - foo\n  - bar
    block_match = TAGS_RE.search(fm)
    if block_match:
        return [
            line.strip().lstrip("- ").strip('"').strip("'")
            for line in block_match.group(1).strip().splitlines()
            if line.strip()
        ]

    # Inline style: tags: [foo, bar]
    inline_match = TAGS_INLINE_RE.search(fm)
    if inline_match:
        return [t.strip().strip('"').strip("'") for t in inline_match.group(1).split(",") if t.strip()]

    return []


def _extract_heading(content: str) -> str:
    """Extract the first H1 heading, skipping frontmatter."""
    # Skip past frontmatter
    fm_match = FRONTMATTER_RE.match(content)
    body = content[fm_match.end():] if fm_match else content

    h1_match = H1_RE.search(body)
    return h1_match.group(1).strip() if h1_match else "(no heading)"


def generate_learning_manifest(tech_dir: Path) -> str | None:
    """Generate a learning manifest for a technology directory.

    Reads technologies/{tech}/learning/*.md, extracts frontmatter tags
    and the first H1 heading, produces a markdown table.

    Returns markdown content, or None if no learning notes exist.
    """
    learning_dir = tech_dir / "learning"
    if not learning_dir.is_dir():
        return None

    notes = sorted(learning_dir.glob("*.md"))
    if not notes:
        return None

    tech_name = tech_dir.name
    # Filter out the tech's own tag from per-note tags (redundant)
    tech_tag = tech_name

    rows: list[str] = []
    for note in notes:
        content = note.read_text()
        tags = [t for t in _extract_tags(content) if t != tech_tag]
        heading = _extract_heading(content)
        rel_path = f"technologies/{tech_name}/learning/{note.name}"
        tag_str = ", ".join(tags) if tags else ""
        rows.append(f"| [{note.stem}]({rel_path}) | {tag_str} | {heading} |")

    table = "\n".join(rows)

    return f"""\
---
description: Hard-won lessons for {tech_name}. Read the full note when working in the relevant area.
type: manifest
tags:
  - {tech_name}
generated: true
---

# {tech_name.title()} — Learning Notes

Read the full note before working in the tagged area. Paths are relative to $MNEMOSYNE_VAULT.

| Note | Tags | Lesson |
|------|------|--------|
{table}
"""
