"""Unit tests for mnemosyne_cli.share.publish — Phase 49 Plan 01.

True RED → GREEN ordering: this file is written before publish.py exists.
All tests import from the production module, which does not exist yet.

Decision coverage:
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
    content_hash,
    extract_third_party,
    render_license,
    render_third_party_notices,
    stage_note,
    strip_cross_set_wikilinks,
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
