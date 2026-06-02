"""Unit tests for mnemosyne_cli.share.wikilinks — D-03/D-04/D-05 coverage."""

from __future__ import annotations

from mnemosyne_cli.share.wikilinks import (
    extract_frontmatter_wikilinks,
    extract_wikilinks,
)


def test_plain_body_link() -> None:
    """Plain body wikilink returns the target."""
    assert extract_wikilinks("See [[note-a]] for details.") == ["note-a"]


def test_path_qualified_link() -> None:
    """Path-qualified wikilink returns the full path."""
    assert extract_wikilinks("[[technologies/anvil/index]]") == [
        "technologies/anvil/index"
    ]


def test_alias_link_returns_target_not_alias() -> None:
    """Alias link returns the target, NOT the alias text (D-04)."""
    assert extract_wikilinks("[[technologies/python/index|Python]]") == [
        "technologies/python/index"
    ]


def test_embed_link() -> None:
    """Embed ![[note]] is an edge identical to [[note]] (D-03, D-04)."""
    assert extract_wikilinks("![[technologies/anvil/reference/x]]") == [
        "technologies/anvil/reference/x"
    ]


def test_heading_anchor_stripped() -> None:
    """Heading anchor [[note#Section]] strips to bare note path (D-05)."""
    assert extract_wikilinks("[[note-a#Section]]") == ["note-a"]


def test_block_anchor_stripped() -> None:
    """Block anchor [[note^abc123]] strips to bare note path (D-05)."""
    assert extract_wikilinks("[[note-a^abc123]]") == ["note-a"]


def test_markdown_link_excluded() -> None:
    """Standard markdown link [text](path.md) is NOT an edge (D-03)."""
    assert extract_wikilinks("[some text](path.md)") == []


def test_obsidian_uri_excluded() -> None:
    """obsidian:// URI is NOT an edge (D-03)."""
    uri = "obsidian://open?vault=empiria&file=technologies/python/index"
    assert extract_wikilinks(uri) == []


def test_obsidian_comment_excluded() -> None:
    """Wikilinks inside Obsidian %%...%% comments are excluded."""
    assert extract_wikilinks("%%[[commented-out]]%%") == []


def test_deduplication() -> None:
    """Same target appearing twice is returned once, first-seen order."""
    content = "[[note-a]] and then [[note-a]] again."
    assert extract_wikilinks(content) == ["note-a"]


def test_empty_content() -> None:
    """Empty content returns empty list."""
    assert extract_wikilinks("") == []


def test_no_links() -> None:
    """Content with no wikilinks returns empty list."""
    assert extract_wikilinks("Just plain text, no links here.") == []


def test_multiple_links_preserve_order() -> None:
    """Multiple distinct links are returned in document order."""
    content = "[[first]] then [[second]] then [[third]]"
    assert extract_wikilinks(content) == ["first", "second", "third"]


def test_frontmatter_wikilinks_scalar_and_list() -> None:
    """extract_frontmatter_wikilinks parses scalar and list string values (D-04)."""
    metadata = {
        "organisation": "[[Friendly Fox Games]]",
        "tags": ["project"],
        "related": ["[[a/b]]", "plain"],
    }
    result = extract_frontmatter_wikilinks(metadata)
    assert result == ["Friendly Fox Games", "a/b"]


def test_frontmatter_wikilinks_ignores_non_strings() -> None:
    """extract_frontmatter_wikilinks ignores non-string values."""
    metadata = {"count": 42, "active": True, "link": "[[note]]"}
    assert extract_frontmatter_wikilinks(metadata) == ["note"]


def test_frontmatter_wikilinks_empty() -> None:
    """extract_frontmatter_wikilinks on empty dict returns empty list."""
    assert extract_frontmatter_wikilinks({}) == []
