"""Tests for share/walker.py — wikilink-closure walker.

Covers all eight edge cases from the Phase 48 synthetic fixture vault,
plus classification, policy gate, strip_candidates, and D-01 ambiguity error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.share.manifest import load_manifest, validate_manifest_dict
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


# ---------------------------------------------------------------------------
# CR-01: malformed frontmatter must NOT silently hide a downstream breach
# ---------------------------------------------------------------------------


def test_malformed_note_surfaces_parse_error_not_clean(tmp_path: Path):
    """A note with malformed frontmatter is recorded in parse_errors (CR-01).

    Layout: seed (in_set, tag share:test) -> [[bad]].  `bad.md` has a
    malformed YAML frontmatter block (an unquoted [[...]] scalar value) so
    frontmatter.load() raises.  If that failure were swallowed as "no links"
    the walk would report CLEAN.  Instead it must surface as a parse error and
    the result must be unsafe under refuse policy (T-48-05-01).
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "seed.md").write_text(
        "---\ntags: [share:test]\n---\n# Seed\n\n[[bad]]\n"
    )
    # Malformed frontmatter: a double-colon mapping value (`a: b: c`) is not
    # valid YAML and makes frontmatter.load() raise — this is the CR-01 trigger
    # note whose own outbound link to the would-be breach we cannot see.
    (vault / "bad.md").write_text(
        "---\nrelated: secret: leak\n---\n# Bad\n\n[[secret-leak]]\n"
    )
    (vault / "secret-leak.md").write_text(
        "---\ntags: []\n---\n# Secret Leak\n"
    )

    manifest = validate_manifest_dict({
        "client": {"slug": "cr01-test", "mode": "direct"},
        "direct": {"target_vault": "x"},
        "include": {"paths": [], "tags": ["share:test"]},
        "on_closure_breach": {"policy": "refuse"},
    })

    result = walk_manifest(manifest, vault)

    # The unparseable note is surfaced, never silently dropped.
    assert "bad.md" in result.parse_errors, (
        f"expected 'bad.md' in parse_errors, got: {result.parse_errors}"
    )
    # A note we cannot parse makes a refuse-policy walk unsafe — NOT clean.
    assert result.is_unsafe is True


def test_malformed_note_does_not_report_clean_under_refuse(tmp_path: Path):
    """A refuse-policy walk with a parse error must gate (is_unsafe), not pass.

    Even with zero excluded/breach notes, the presence of an unparseable note
    means the closure cannot be trusted — has_breaches may be False but
    is_unsafe must be True so the doctor exit gate fails (CR-01)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "seed.md").write_text(
        "---\ntags: [share:test]\n---\n# Seed\n\n[[bad]]\n"
    )
    (vault / "bad.md").write_text(
        "---\nrelated: secret: leak\n---\n# Bad\n"
    )

    manifest = validate_manifest_dict({
        "client": {"slug": "cr01b-test", "mode": "direct"},
        "direct": {"target_vault": "x"},
        "include": {"paths": [], "tags": ["share:test"]},
        "on_closure_breach": {"policy": "refuse"},
    })

    result = walk_manifest(manifest, vault)
    assert result.parse_errors, "parse failure must be surfaced, not swallowed"
    assert result.is_unsafe is True


# ---------------------------------------------------------------------------
# CR-02: path-qualified link into a hidden (dot) directory must NOT resolve
# ---------------------------------------------------------------------------


def test_hidden_dir_link_not_resolved_into_closure(tmp_path: Path):
    """A [[.hidden/secret]] link to a dot-directory file is out-of-universe.

    Hidden directories are excluded everywhere (_vault_md_files); the
    path-qualified branch of _resolve must apply the same guard (CR-02) so a
    real file under a dot-dir never appears in in_set/excluded/breach."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "seed.md").write_text(
        "---\ntags: [share:test]\n---\n# Seed\n\n[[.hidden/secret]]\n"
    )
    hidden = vault / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text(
        "---\ntags: []\n---\n# Secret under a dot-dir\n"
    )

    manifest = validate_manifest_dict({
        "client": {"slug": "cr02-test", "mode": "direct"},
        "direct": {"target_vault": "x"},
        "include": {"paths": [], "tags": ["share:test"]},
        "on_closure_breach": {"policy": "warn"},
    })

    result = walk_manifest(manifest, vault)

    hidden_rel = ".hidden/secret.md"
    assert hidden_rel not in result.in_set
    assert hidden_rel not in result.excluded
    assert hidden_rel not in result.breach
    # No classified list should reference anything under the dot-dir.
    all_classified = result.in_set + result.excluded + result.breach
    assert not any(p.startswith(".hidden/") for p in all_classified), (
        f"hidden-dir file leaked into closure: {all_classified}"
    )


# ---------------------------------------------------------------------------
# WR-01: an excluded note is a cut-point — closure does not traverse through it
# ---------------------------------------------------------------------------


def test_excluded_note_is_cut_point(tmp_path: Path):
    """A -> excluded B -> C (C reachable ONLY via B): B excluded, C not a breach.

    An excluded note will not be published, so its outbound links cannot leak
    anything; the BFS must not expand through it (WR-01, exclude = cut-point).
    B is still classified `excluded` (and still gates under D-11), but C — which
    is reachable only through B — must NOT appear as a breach."""
    vault = tmp_path / "vault"
    (vault / "included").mkdir(parents=True)
    (vault / "decision").mkdir(parents=True)
    (vault / "deep").mkdir(parents=True)

    # A: in_set (matches include.paths), links to excluded B.
    (vault / "included" / "a.md").write_text(
        "---\ntags: []\n---\n# A\n\n[[decision/b]]\n"
    )
    # B: under decision/ → matches exclude.paths → excluded; links to C.
    (vault / "decision" / "b.md").write_text(
        "---\ntags: []\n---\n# B (excluded)\n\n[[deep/c]]\n"
    )
    # C: reachable ONLY through excluded B.  Must not be pulled into the walk.
    (vault / "deep" / "c.md").write_text(
        "---\ntags: []\n---\n# C — only reachable via excluded B\n"
    )

    manifest = validate_manifest_dict({
        "client": {"slug": "wr01-test", "mode": "direct"},
        "direct": {"target_vault": "x"},
        "include": {"paths": ["included/**"], "tags": []},
        "exclude": {"paths": ["decision/**"]},
        "on_closure_breach": {"policy": "warn"},
    })

    result = walk_manifest(manifest, vault)

    assert "included/a.md" in result.in_set
    # B is reached and classified excluded (still policy-actionable, D-11).
    assert "decision/b.md" in result.excluded
    # C is reachable ONLY through excluded B → cut-point → never visited.
    assert "deep/c.md" not in result.breach
    assert "deep/c.md" not in result.in_set
    assert "deep/c.md" not in result.excluded
