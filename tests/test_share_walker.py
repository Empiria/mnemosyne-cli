"""Tests for share/walker.py — wikilink-closure walker.

Covers all eight edge cases from the Phase 48 synthetic fixture vault,
plus classification, policy gate, strip_candidates, and D-01 ambiguity error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.share.manifest import load_manifest
from mnemosyne_cli.share.walker import (
    AmbiguousLinkError,
    WalkResult,
    resolve_seed,
    walk_manifest,
)

# ---------------------------------------------------------------------------
# Fixture path helpers
# ---------------------------------------------------------------------------

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "share_vault"
FIXTURE_MANIFEST = FIXTURE_VAULT / "clients" / "testclient" / "share-manifest.toml"


@pytest.fixture(scope="module")
def manifest():
    """Load the fixture manifest once for all module tests."""
    return load_manifest(FIXTURE_MANIFEST)


@pytest.fixture(scope="module")
def result(manifest):
    """Walk the fixture manifest once and cache the WalkResult."""
    return walk_manifest(manifest, FIXTURE_VAULT)


# ---------------------------------------------------------------------------
# Edge case 1: body link — b is reached via a's body [[...]] link
# ---------------------------------------------------------------------------


def test_body_link_b_is_in_set(result):
    """Note b is reached via a's body link and classified as in_set."""
    assert "technologies/demo/reference/b.md" in result.in_set


# ---------------------------------------------------------------------------
# Edge case 2: frontmatter-list link — c is reached via b's related: field
# ---------------------------------------------------------------------------


def test_frontmatter_list_link_c_is_in_set(result):
    """Note c is reached via b's frontmatter list field and classified as in_set."""
    assert "technologies/demo/reference/c.md" in result.in_set


# ---------------------------------------------------------------------------
# Edge case 3: alias-only link — d is reached via c's [[.../d|Friendly D]]
# ---------------------------------------------------------------------------


def test_alias_only_link_d_is_in_set(result):
    """Note d is reached via c's alias link and classified as in_set (by target, not alias)."""
    assert "technologies/demo/reference/d.md" in result.in_set


# ---------------------------------------------------------------------------
# Edge case 4: broken link — does-not-exist in broken, NOT in breach (D-02)
# ---------------------------------------------------------------------------


def test_broken_link_in_broken_not_breach(result):
    """Broken link target appears in result.broken and NOT in result.breach (D-02)."""
    # The exact path will be a vault-rel path attempt
    broken_targets = result.broken
    assert any("does-not-exist" in t for t in broken_targets), (
        f"expected broken target in result.broken, got: {broken_targets}"
    )
    breach_targets = result.breach
    assert not any("does-not-exist" in t for t in breach_targets), (
        f"broken target must not appear in breach, got: {breach_targets}"
    )


# ---------------------------------------------------------------------------
# Edge case 5: circular link — a<->e terminates, both in_set
# ---------------------------------------------------------------------------


def test_circular_link_terminates_and_both_in_set(result):
    """Circular a<->e terminates (no RecursionError) and both are in_set."""
    assert "technologies/demo/reference/a.md" in result.in_set
    assert "technologies/demo/reference/e.md" in result.in_set


# ---------------------------------------------------------------------------
# Edge case 6: in-set-to-exclude — adr-1 is excluded, contributes to has_breaches
# ---------------------------------------------------------------------------


def test_in_set_to_exclude_adr_in_excluded(result):
    """Note adr-1 is reachable but matches exclude.paths → classified as excluded."""
    assert "technologies/demo/decision/adr-1.md" in result.excluded


def test_excluded_makes_has_breaches_true(result):
    """has_breaches is True because excluded is non-empty (D-11)."""
    assert result.has_breaches is True


# ---------------------------------------------------------------------------
# Edge case 7: in-set-to-tag-included — shared-note is in_set via tag
# ---------------------------------------------------------------------------


def test_tag_included_shared_note_is_in_set(result):
    """shared-note is outside include.paths but carries share:test → in_set via tag (D-13/D-15)."""
    assert "technologies/other/shared-note.md" in result.in_set


# ---------------------------------------------------------------------------
# Edge case 8: embed — f is reached via a's ![[...]] embed
# ---------------------------------------------------------------------------


def test_embed_f_is_in_set(result):
    """Note f is reached via a's embed and classified as in_set."""
    assert "technologies/demo/reference/f.md" in result.in_set


# ---------------------------------------------------------------------------
# Closure breach — secret/leaky is in breach (truly unclassified)
# ---------------------------------------------------------------------------


def test_closure_breach_leaky_in_breach(result):
    """secret/leaky is reachable but unclassified → appears in result.breach."""
    assert "technologies/secret/leaky.md" in result.breach


# ---------------------------------------------------------------------------
# has_breaches and strip_candidates
# ---------------------------------------------------------------------------


def test_has_breaches_true_when_excluded_or_breach(result):
    """has_breaches is True because both excluded (adr-1) and breach (leaky) exist (D-11)."""
    assert result.has_breaches is True


def test_strip_candidates_contains_excluded_and_breach_edges(result):
    """strip_candidates records in_set→excluded and in_set→breach edges (D-12)."""
    candidate_targets = {target for _source, target in result.strip_candidates}
    assert "technologies/demo/decision/adr-1.md" in candidate_targets, (
        f"expected adr-1 in strip_candidates, got: {candidate_targets}"
    )
    assert "technologies/secret/leaky.md" in candidate_targets, (
        f"expected leaky in strip_candidates, got: {candidate_targets}"
    )


# ---------------------------------------------------------------------------
# D-01: AmbiguousLinkError on bare-basename collision
# ---------------------------------------------------------------------------


def test_ambiguous_bare_link_raises(tmp_path: Path):
    """Bare [[dup-name]] with two matching notes raises AmbiguousLinkError (D-01)."""
    import tomllib

    from mnemosyne_cli.share.manifest import validate_manifest_dict

    # Build a tiny vault with two notes sharing the basename "dup-name"
    vault = tmp_path / "vault"
    (vault / "dir_a").mkdir(parents=True)
    (vault / "dir_b").mkdir(parents=True)
    (vault / "dir_a" / "dup-name.md").write_text(
        "---\ntags: []\n---\n# Dup A\n"
    )
    (vault / "dir_b" / "dup-name.md").write_text(
        "---\ntags: []\n---\n# Dup B\n"
    )
    # A seed note that links via bare basename
    (vault / "seed.md").write_text(
        "---\ntags: [share:test]\n---\n# Seed\n\n[[dup-name]]\n"
    )

    manifest = validate_manifest_dict({
        "client": {"slug": "ambig-test", "mode": "direct"},
        "direct": {"target_vault": "x"},
        "include": {"paths": [], "tags": ["share:test"]},
        "on_closure_breach": {"policy": "warn"},
    })

    with pytest.raises(AmbiguousLinkError):
        walk_manifest(manifest, vault)


# ---------------------------------------------------------------------------
# WalkResult metadata
# ---------------------------------------------------------------------------


def test_walk_result_carries_metadata(result, manifest):
    """WalkResult carries client_slug, policy, and manifest_path."""
    assert result.client_slug == "testclient"
    assert result.policy == "refuse"
