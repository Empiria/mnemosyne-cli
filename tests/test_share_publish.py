"""Unit tests for mnemosyne_cli.share.publish — Phase 49 Plans 01 and 02.

True RED → GREEN ordering: this file is extended for Plan 02 before the
idempotency functions exist.

Decision coverage:
  D-04  detect-and-refuse against client edits (test_detect_client_edits)
  D-05  diff-only re-runs (test_diff_only_reruns, test_diff_only_deletions)
  D-06  zero-change no-op signal (test_no_op_on_zero_changes)
  D-07  per-file dual source/output hash in PUBLISHED.json
  D-08  all eight PUBLISHED.json fields (test_published_json_fields)
  D-09  byte-level determinism (test_determinism)
  D-11  SPDX injection, source vault untouched (test_spdx_injection)
  D-12  third-party spdx: override, THIRD-PARTY-NOTICES rendering
  D-13  LICENSE.md rendering with {year}/{copyright_holder} substitution
  D-49-12 cross-set wikilink strip (test_strip_wikilinks)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.share.publish import (
    PublishError,
    WritePlan,
    build_published_json,
    compute_write_plan,
    content_hash,
    detect_client_edits,
    extract_third_party,
    load_published_json,
    render_license,
    render_third_party_notices,
    stage_note,
    strip_cross_set_wikilinks,
    write_published_json,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "publish_vault"
TESTING_MD = FIXTURE_ROOT / "technologies" / "anvil" / "reference" / "testing.md"
FORMS_MD = FIXTURE_ROOT / "technologies" / "anvil" / "reference" / "forms.md"
VENDORED_LIB_MD = FIXTURE_ROOT / "technologies" / "python" / "reference" / "vendored-lib.md"
LICENSE_TEMPLATE = FIXTURE_ROOT / "clients" / "friendly-fox" / "license-template.md"

CLIENT_SPDX = "LicenseRef-Empiria-FriendlyFox-2026"
COPYRIGHT_TEXT = "Copyright (c) 2026 Empiria Ltd."


# ---------------------------------------------------------------------------
# (a) SPDX injection for a normal note
# ---------------------------------------------------------------------------


def test_spdx_injection(tmp_path: Path) -> None:
    """Stage a normal note; assert SPDX fields in staged copy and source untouched (D-11)."""
    import frontmatter

    dest = tmp_path / "technologies" / "anvil" / "reference" / "testing.md"

    stage_note(
        TESTING_MD,
        dest,
        client_spdx_identifier=CLIENT_SPDX,
        copyright_text=COPYRIGHT_TEXT,
    )

    # Staged copy must have the SPDX fields
    staged = frontmatter.load(str(dest))
    assert staged.metadata.get("SPDX-License-Identifier") == CLIENT_SPDX
    assert staged.metadata.get("SPDX-FileCopyrightText") == COPYRIGHT_TEXT

    # Source note must be UNCHANGED (D-11 / Pitfall 3)
    source = frontmatter.load(str(TESTING_MD))
    assert "SPDX-License-Identifier" not in source.metadata
    assert "SPDX-FileCopyrightText" not in source.metadata


# ---------------------------------------------------------------------------
# (b) Third-party note: spdx: overrides client LicenseRef-
# ---------------------------------------------------------------------------


def test_spdx_injection_third_party(tmp_path: Path) -> None:
    """Stage a third-party note; SPDX-License-Identifier must be MIT, not client LicenseRef- (D-12)."""
    import frontmatter

    dest = tmp_path / "technologies" / "python" / "reference" / "vendored-lib.md"

    stage_note(
        VENDORED_LIB_MD,
        dest,
        client_spdx_identifier=CLIENT_SPDX,
        copyright_text=COPYRIGHT_TEXT,
    )

    staged = frontmatter.load(str(dest))
    assert staged.metadata.get("SPDX-License-Identifier") == "MIT"
    # Copyright text always injected
    assert staged.metadata.get("SPDX-FileCopyrightText") == COPYRIGHT_TEXT


# ---------------------------------------------------------------------------
# (c) Third-party extraction + THIRD-PARTY-NOTICES rendering
# ---------------------------------------------------------------------------


def test_third_party_notices(tmp_path: Path) -> None:
    """extract_third_party returns vendored-lib entry; render produces MIT + attribution text."""
    in_set_paths = [TESTING_MD, VENDORED_LIB_MD]
    third_party = extract_third_party(in_set_paths, FIXTURE_ROOT)

    # Only vendored-lib has spdx: frontmatter
    paths_found = [entry["path"] for entry in third_party]
    assert any("vendored-lib" in p for p in paths_found), f"vendored-lib missing: {paths_found}"

    # Attribution field captured
    vendored_entry = next(e for e in third_party if "vendored-lib" in e["path"])
    assert vendored_entry["spdx"] == "MIT"
    assert "Upstream Author" in vendored_entry["attribution"]

    # Render with content
    notices = render_third_party_notices(third_party)
    assert "MIT" in notices
    assert "Upstream Author" in notices

    # Empty list yields no-third-party doc
    empty_notices = render_third_party_notices([])
    assert "No third-party content" in empty_notices


# ---------------------------------------------------------------------------
# (d) LICENSE.md rendering
# ---------------------------------------------------------------------------


def test_license_rendering(tmp_path: Path) -> None:
    """render_license substitutes placeholders and appends LicenseRef- section (D-13)."""
    template_text = LICENSE_TEMPLATE.read_text(encoding="utf-8")

    result = render_license(
        template_text=template_text,
        year=2026,
        copyright_holder="Empiria Ltd.",
        spdx_license_ref=CLIENT_SPDX,
    )

    assert "Empiria Ltd." in result
    assert "2026" in result
    # LicenseRef- definition section
    assert f"## {CLIENT_SPDX}" in result
    # No unsubstituted placeholders remain
    assert "{year}" not in result
    assert "{copyright_holder}" not in result


# ---------------------------------------------------------------------------
# (e) Strip cross-set wikilinks
# ---------------------------------------------------------------------------


def test_strip_wikilinks() -> None:
    """strip_cross_set_wikilinks flattens cross-set links; in-set links unchanged (D-49-12)."""
    content = (
        "In-set: [[technologies/anvil/reference/forms]]\n"
        "Cross-set: [[technologies/secret/internal|the internal note]]\n"
        "Embed: ![[technologies/secret/internal]]\n"
    )
    breach_targets = {"technologies/secret/internal"}

    result = strip_cross_set_wikilinks(content, breach_targets)

    # In-set link is UNCHANGED
    assert "[[technologies/anvil/reference/forms]]" in result
    # Cross-set plain link replaced by alias text
    assert "the internal note" in result
    assert "[[technologies/secret/internal" not in result
    # Embed stripped to empty string
    assert "![[technologies/secret/internal]]" not in result


# ---------------------------------------------------------------------------
# (f) Byte-level determinism (D-09)
# ---------------------------------------------------------------------------


def test_determinism(tmp_path: Path) -> None:
    """Stage the same vault twice; every content-tree file is byte-identical (D-09)."""
    import frontmatter

    in_set = [TESTING_MD, FORMS_MD]
    template_text = LICENSE_TEMPLATE.read_text(encoding="utf-8")
    third_party = extract_third_party(in_set, FIXTURE_ROOT)

    def build_tree(root: Path) -> None:
        for src in in_set:
            rel = src.relative_to(FIXTURE_ROOT)
            dest = root / rel
            stage_note(
                src,
                dest,
                client_spdx_identifier=CLIENT_SPDX,
                copyright_text=COPYRIGHT_TEXT,
            )
        (root / "LICENSE.md").write_text(
            render_license(
                template_text=template_text,
                year=2026,
                copyright_holder="Empiria Ltd.",
                spdx_license_ref=CLIENT_SPDX,
            ),
            encoding="utf-8",
        )
        (root / "THIRD-PARTY-NOTICES.md").write_text(
            render_third_party_notices(third_party),
            encoding="utf-8",
        )

    root_a = tmp_path / "run_a"
    root_b = tmp_path / "run_b"
    root_a.mkdir()
    root_b.mkdir()
    build_tree(root_a)
    build_tree(root_b)

    # Compare every file in root_a against root_b — must be byte-identical
    for path_a in sorted(root_a.rglob("*")):
        if path_a.is_file():
            rel = path_a.relative_to(root_a)
            path_b = root_b / rel
            assert path_b.exists(), f"Missing in run_b: {rel}"
            assert path_a.read_bytes() == path_b.read_bytes(), (
                f"Non-deterministic output: {rel}"
            )


# ---------------------------------------------------------------------------
# (g) content_hash stability
# ---------------------------------------------------------------------------


def test_content_hash_stable(tmp_path: Path) -> None:
    """content_hash of the same file is stable and starts with 'sha256:'."""
    f = tmp_path / "file.md"
    f.write_text("hello", encoding="utf-8")

    h1 = content_hash(f)
    h2 = content_hash(f)

    assert h1 == h2
    assert h1.startswith("sha256:")


# ---------------------------------------------------------------------------
# Phase 49 Plan 02 — idempotency + provenance layer (D-04/D-05/D-06/D-07/D-08)
# ---------------------------------------------------------------------------


def _make_file(root: Path, rel: str, text: str) -> Path:
    """Write *text* to *root/rel*, creating parent dirs."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# (a) PUBLISHED.json — eight D-08 fields and dual per-file hashes (D-07)


def test_published_json_fields(tmp_path: Path) -> None:
    """build_published_json produces all eight D-08 fields; each file entry has
    both source_hash and output_hash (D-07); write+reload round-trips cleanly."""
    publish_root = tmp_path / "publish"
    publish_root.mkdir()

    file_hashes = {
        "notes/alpha.md": {
            "source_hash": "sha256:aaa",
            "output_hash": "sha256:bbb",
        },
        "notes/beta.md": {
            "source_hash": "sha256:ccc",
            "output_hash": "sha256:ddd",
        },
    }

    data = build_published_json(
        source_vault_sha="abc1234",
        share_manifest_hash="sha256:manifest",
        license_md_hash="sha256:license",
        third_party_notices_hash="sha256:tpn",
        file_hashes=file_hashes,
    )

    # All eight D-08 fields must be present
    assert "schema_version" in data, "schema_version missing"
    assert "publish_timestamp" in data, "publish_timestamp missing"
    assert "source_vault_sha" in data, "source_vault_sha missing"
    assert "share_manifest_hash" in data, "share_manifest_hash missing"
    assert "license_md_hash" in data, "license_md_hash missing"
    assert "third_party_notices_hash" in data, "third_party_notices_hash missing"
    assert "files" in data, "files missing"

    # schema_version must be a non-empty string
    assert isinstance(data["schema_version"], str)
    assert data["schema_version"] != ""

    # publish_timestamp must look like a UTC ISO-8601 string ending in Z
    ts = data["publish_timestamp"]
    assert isinstance(ts, str)
    assert ts.endswith("Z"), f"timestamp does not end with Z: {ts!r}"
    assert "T" in ts, f"timestamp has no T separator: {ts!r}"

    # D-07: every file entry must have BOTH source_hash and output_hash
    for rel, entry in data["files"].items():
        assert "source_hash" in entry, f"{rel}: source_hash missing"
        assert "output_hash" in entry, f"{rel}: output_hash missing"

    # Correct values passed through
    assert data["source_vault_sha"] == "abc1234"
    assert data["share_manifest_hash"] == "sha256:manifest"
    assert data["license_md_hash"] == "sha256:license"
    assert data["third_party_notices_hash"] == "sha256:tpn"

    # Round-trip via write + load
    write_published_json(publish_root, data)
    loaded = load_published_json(publish_root)
    assert loaded is not None
    assert loaded["schema_version"] == data["schema_version"]
    assert loaded["files"] == data["files"]
    assert loaded["source_vault_sha"] == "abc1234"


# (b) Diff-only re-runs: changed + new → to_write; unchanged → skip


def test_diff_only_reruns(tmp_path: Path) -> None:
    """compute_write_plan with a prior PUBLISHED.json: only changed/new paths appear
    in to_write; unchanged paths are absent (D-05)."""
    h1 = "sha256:hash_a_unchanged"
    h2_old = "sha256:hash_b_old"
    h2_new = "sha256:hash_b_new"
    h3 = "sha256:hash_c_new"

    prior_published = {
        "files": {
            "notes/alpha.md": {"source_hash": h1, "output_hash": "sha256:out_a"},
            "notes/beta.md": {"source_hash": h2_old, "output_hash": "sha256:out_b"},
        }
    }

    current_sources = {
        "notes/alpha.md": h1,       # unchanged
        "notes/beta.md": h2_new,    # changed
        "notes/gamma.md": h3,       # new
    }

    plan = compute_write_plan(current_sources, prior_published)

    assert sorted(plan.to_write) == ["notes/beta.md", "notes/gamma.md"]
    assert plan.to_delete == []
    assert plan.has_changes is True


# (c) Diff-only: deletions when a file disappears from the current output set


def test_diff_only_deletions(tmp_path: Path) -> None:
    """A path present in prior PUBLISHED.json but absent from current_sources
    appears in to_delete (D-05)."""
    h1 = "sha256:hash_a"
    h2 = "sha256:hash_b"

    prior_published = {
        "files": {
            "notes/alpha.md": {"source_hash": h1, "output_hash": "sha256:out_a"},
            "notes/beta.md": {"source_hash": h2, "output_hash": "sha256:out_b"},
        }
    }

    # beta.md is gone from current output
    current_sources = {
        "notes/alpha.md": h1,
    }

    plan = compute_write_plan(current_sources, prior_published)

    assert plan.to_write == []
    assert plan.to_delete == ["notes/beta.md"]
    assert plan.has_changes is True


# (d) Zero-change no-op: all source hashes match → has_changes is False (D-06)


def test_no_op_on_zero_changes(tmp_path: Path) -> None:
    """When all current source hashes match the prior PUBLISHED.json, compute_write_plan
    returns a plan where has_changes is False (D-06)."""
    h1 = "sha256:hash_a"
    h2 = "sha256:hash_b"

    prior_published = {
        "files": {
            "notes/alpha.md": {"source_hash": h1, "output_hash": "sha256:out_a"},
            "notes/beta.md": {"source_hash": h2, "output_hash": "sha256:out_b"},
        }
    }

    current_sources = {
        "notes/alpha.md": h1,
        "notes/beta.md": h2,
    }

    plan = compute_write_plan(current_sources, prior_published)

    assert plan.has_changes is False
    assert plan.to_write == []
    assert plan.to_delete == []


# (e) First publish (no prior PUBLISHED.json): all current paths go to to_write


def test_first_publish_full(tmp_path: Path) -> None:
    """compute_write_plan(current_sources, None) returns every current path in
    to_write and nothing in to_delete (first-publish case)."""
    current_sources = {
        "notes/alpha.md": "sha256:ha",
        "notes/beta.md": "sha256:hb",
        "notes/gamma.md": "sha256:hc",
    }

    plan = compute_write_plan(current_sources, None)

    assert sorted(plan.to_write) == ["notes/alpha.md", "notes/beta.md", "notes/gamma.md"]
    assert plan.to_delete == []
    assert plan.has_changes is True


# (f) detect_client_edits: raises on edited/deleted, respects force flag (D-04)


def test_detect_client_edits(tmp_path: Path) -> None:
    """detect_client_edits raises PublishError on edited/deleted files when
    force=False; returns the list without raising when force=True (D-04).
    Deleted files are reported as '<rel> (deleted)'."""
    publish_root = tmp_path / "publish"
    publish_root.mkdir()

    # Write a staged file and record its output_hash in prior_published
    note_rel = "notes/alpha.md"
    note_path = publish_root / note_rel
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text("original content", encoding="utf-8")
    original_hash = content_hash(note_path)

    # Deleted file — not on disk
    deleted_rel = "notes/deleted.md"

    prior_published = {
        "files": {
            note_rel: {"source_hash": "sha256:src", "output_hash": original_hash},
            deleted_rel: {"source_hash": "sha256:src2", "output_hash": "sha256:someoldhash"},
        }
    }

    # No edits yet — should return empty list (nothing changed)
    result = detect_client_edits(publish_root, prior_published, force=False)
    assert deleted_rel + " (deleted)" in result  # deleted file is always reported
    assert note_rel not in result

    # Now mutate the staged file — simulates a client edit
    note_path.write_text("client edited this!", encoding="utf-8")

    # force=False → must raise PublishError listing the edited path
    with pytest.raises(PublishError) as exc_info:
        detect_client_edits(publish_root, prior_published, force=False)
    assert note_rel in str(exc_info.value)

    # force=True → returns the list without raising
    edited = detect_client_edits(publish_root, prior_published, force=True)
    assert note_rel in edited
    assert deleted_rel + " (deleted)" in edited


# (g) detect_client_edits with no prior PUBLISHED.json → always returns []


def test_detect_client_edits_first_publish(tmp_path: Path) -> None:
    """detect_client_edits(publish_root, None, ...) returns [] without raising
    (first publish — nothing to protect yet, D-04)."""
    publish_root = tmp_path / "publish"
    publish_root.mkdir()

    result = detect_client_edits(publish_root, None, force=False)
    assert result == []
