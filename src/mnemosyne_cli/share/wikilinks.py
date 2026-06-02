"""Unified Obsidian wikilink parser for the closure-walker subsystem.

Implements D-03, D-04, and D-05 from Phase 48 CONTEXT.md:

- D-03: Follow Obsidian wikilinks only (body + embeds); exclude markdown links
  and obsidian:// URIs — those are never wrapped in [[ ]] so the regex
  naturally excludes them.
- D-04: All wikilink forms are edges: body [[link]], embeds ![[link]],
  alias-only [[note|alias]] (resolve via target, not alias), frontmatter
  list links.  De-duplication (visited-set) is the walker's job, not ours —
  we de-dup within a single note parse only.
- D-05: Strip heading/block anchors ([[note#Heading]], [[note^block-id]]) to
  the bare note path.  Sub-note granularity is not used in Phase 48.

This module is stdlib-only (re).  No path resolution is performed here — that
is the walker's job (D-01).
"""

from __future__ import annotations

import re

# Match Obsidian comment blocks %%...%% (including multiline).
# Copied from lib/embeds.py — strip before any regex scan so that
# documentation text inside comments (e.g. %%[[example]]%%) is excluded.
COMMENT_RE = re.compile(r"%%.*?%%", re.DOTALL)

# Match both plain [[target]] and embed ![[target]].
# The leading '!' is optional.  Non-greedy capture stops at ']]'.
# This naturally excludes:
#   - standard markdown links [text](url)  — no leading [[
#   - obsidian:// URIs                      — no [[ at all
WIKILINK_RE = re.compile(r"!?\[\[([^\]]+?)\]\]")


def _normalize_target(raw: str) -> str:
    """Strip alias, heading/block anchor, and surrounding whitespace.

    Pipeline:
    1. Split on first '|' — discard alias text, keep target side.
    2. Split on first '#' — discard heading anchor, keep path side.
    3. Split on first '^' — discard block ID, keep path side.
    4. strip() whitespace.
    """
    # 1. alias — [[target|Alias Text]] → "target"
    target = raw.split("|", 1)[0]
    # 2. heading anchor — [[note#Heading]] → "note"
    target = target.split("#", 1)[0]
    # 3. block ID — [[note^abc123]] → "note"
    target = target.split("^", 1)[0]
    return target.strip()


def extract_wikilinks(content: str) -> list[str]:
    """Return wikilink targets from Obsidian note body content.

    Strips Obsidian comment blocks (%%...%%) before scanning.  Returns
    targets in document order, de-duplicated preserving first-seen order.

    Args:
        content: Raw markdown note body (may include YAML frontmatter text,
                 Obsidian comments, etc.).

    Returns:
        Ordered, de-duplicated list of note-path targets.  Anchors and
        aliases are stripped (D-05).  Empty list if no wikilinks found.
    """
    stripped = COMMENT_RE.sub("", content)
    seen: set[str] = set()
    results: list[str] = []
    for match in WIKILINK_RE.finditer(stripped):
        target = _normalize_target(match.group(1))
        if target and target not in seen:
            seen.add(target)
            results.append(target)
    return results


def extract_frontmatter_wikilinks(metadata: dict) -> list[str]:
    """Return wikilink targets found in frontmatter scalar/list string values.

    Vault taxonomy encodes relationships in frontmatter as string values
    containing wikilinks, e.g. ``organisation: "[[Friendly Fox Games]]"`` or
    ``related: ["[[a/b]]", "[[c/d]]"]``.  This function walks the dict,
    extracts targets from any string scalars and string elements of lists,
    and returns them de-duplicated in traversal order.

    Non-string values (int, bool, nested dicts, etc.) are silently ignored.

    Args:
        metadata: Parsed frontmatter dict (e.g. from python-frontmatter).

    Returns:
        Ordered, de-duplicated list of note-path targets.
    """
    seen: set[str] = set()
    results: list[str] = []

    def _collect(value: object) -> None:
        if isinstance(value, str):
            for target in extract_wikilinks(value):
                if target not in seen:
                    seen.add(target)
                    results.append(target)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    for val in metadata.values():
        _collect(val)

    return results
