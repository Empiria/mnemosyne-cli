"""Obsidian embed note parsing."""

from __future__ import annotations

import re
from pathlib import Path

# Match ![[path]] — stops at ] or # to handle section-qualified embeds like ![[file.md#Heading]]
EMBED_RE = re.compile(r'!\[\[([^\]#]+)')

# Match Obsidian comment blocks %%...%% (including multiline)
COMMENT_RE = re.compile(r'%%.*?%%', re.DOTALL)


def extract_embed_target(content: str) -> str | None:
    """Extract the vault-relative path from an Obsidian embed note.

    Reads the first ![[...]] embed found in the content (outside Obsidian
    comment blocks) and returns the vault-relative path (e.g.
    "technologies/anvil/standards.md").

    Returns None if no embed is found.
    """
    # Strip Obsidian comment blocks before searching — they may contain
    # literal ![[...]] in documentation text that would match the embed pattern.
    stripped = COMMENT_RE.sub('', content)
    match = EMBED_RE.search(stripped)
    if not match:
        return None
    return match.group(1).strip()


def read_embed_targets(embed_dir: Path) -> dict[str, str]:
    """Read all .md files in a directory and extract embed targets.

    Returns a dict mapping filename (e.g. "anvil.md") to vault-relative
    target path (e.g. "technologies/anvil/standards.md").

    Files without a valid embed are skipped with no error.
    """
    targets: dict[str, str] = {}
    if not embed_dir.is_dir():
        return targets
    for md_file in sorted(embed_dir.glob("*.md")):
        content = md_file.read_text()
        target = extract_embed_target(content)
        if target is not None:
            targets[md_file.name] = target
    return targets
