"""Content producers for mnemosyne tech-publish (Phase 49 Plan 01).

Pure, side-effect-light building blocks:

- ``PublishError``               — domain error for publish failures
- ``content_hash``               — SHA-256 hash of a file (``sha256:<hex>``)
- ``stage_note``                 — copy a source note to dest with SPDX injection
- ``strip_cross_set_wikilinks``  — replace cross-set [[links]] with alias text
- ``extract_third_party``        — collect spdx:/attribution: notes from in-set
- ``render_license``             — substitute placeholders in a licence template
- ``render_third_party_notices`` — build THIRD-PARTY-NOTICES.md from third-party list

These functions are deliberately PURE w.r.t. config and git:
  - No config.toml / secrets.toml reads
  - No subprocess calls
  - All inputs are explicit (paths, strings, dicts)
  - NEVER write back to the source vault (D-11 / Pitfall 3)

Determinism (D-09):
  - python-frontmatter uses PyYAML with sort_keys=True by default — YAML keys
    in the output frontmatter are alphabetically sorted, giving byte-identical
    dumps for the same input.
  - third-party lists are sorted by path before any rendering.
  - No wall-clock calls (datetime.now, time.time, etc.).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import frontmatter

from mnemosyne_cli.share.wikilinks import WIKILINK_RE


# ---------------------------------------------------------------------------
# Domain error
# ---------------------------------------------------------------------------


class PublishError(Exception):
    """Raised on any content-producer failure.

    Reused by Plans 02 (idempotency) and 03 (CLI + git) to surface
    actionable error messages to the operator.
    """


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def content_hash(path: Path) -> str:
    """Return a SHA-256 hex digest of the file at *path*, prefixed ``sha256:``.

    Args:
        path: Filesystem path to the file to hash.

    Returns:
        A string of the form ``"sha256:<64-char-hex-digest>"``.
    """
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# stage_note
# ---------------------------------------------------------------------------


def stage_note(
    source_path: Path,
    dest_path: Path,
    *,
    client_spdx_identifier: str,
    copyright_text: str,
) -> bytes:
    """Copy *source_path* to *dest_path*, injecting SPDX frontmatter fields.

    SPDX injection rules (D-11 / D-12):
    - If the source frontmatter has a truthy ``spdx:`` key (third-party note),
      use that value as ``SPDX-License-Identifier``.
    - Otherwise, use *client_spdx_identifier* (the client ``LicenseRef-``).
    - Always set ``SPDX-FileCopyrightText`` to *copyright_text*.

    The source vault file is NEVER modified (D-11 / Pitfall 3).

    Args:
        source_path:           Path to the source note in the vault.
        dest_path:             Path to write the staged copy.
        client_spdx_identifier: The client-level ``LicenseRef-*`` identifier.
        copyright_text:        The ``SPDX-FileCopyrightText`` value.

    Returns:
        The UTF-8-encoded bytes of the staged file content.
    """
    post = frontmatter.load(str(source_path))

    # D-12: third-party override — use the note's own spdx: value if present
    source_spdx = post.metadata.get("spdx")
    if source_spdx:
        spdx_identifier = str(source_spdx)
    else:
        spdx_identifier = client_spdx_identifier

    post["SPDX-License-Identifier"] = spdx_identifier
    post["SPDX-FileCopyrightText"] = copyright_text

    content = frontmatter.dumps(post)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    return content.encode("utf-8")


# ---------------------------------------------------------------------------
# strip_cross_set_wikilinks
# ---------------------------------------------------------------------------


def strip_cross_set_wikilinks(content: str, breach_targets: set[str]) -> str:
    """Replace wikilinks to *breach_targets* with alias/display text.

    For each wikilink in *content*:
    - If its base target path is in *breach_targets*:
      - Embed (``![[...]]``): replaced with empty string.
      - Plain link with alias (``[[target|alias]]``): replaced with alias.
      - Plain link without alias (``[[target]]``): replaced with the bare
        base path.
    - Otherwise the wikilink is left unchanged.

    Reuses the Phase 48 ``WIKILINK_RE`` parser from ``share/wikilinks.py``
    (D-49-12).

    Args:
        content:        Raw markdown note body (may include frontmatter text).
        breach_targets: Set of vault-relative base paths to strip.

    Returns:
        The modified content string.
    """

    def replacer(match: re.Match) -> str:  # type: ignore[type-arg]
        inner = match.group(1)
        parts = inner.split("|", 1)
        raw_target = parts[0].strip()
        # Strip heading / block anchor to get the base note path
        base = raw_target.split("#", 1)[0].split("^", 1)[0].strip()

        if base not in breach_targets:
            return match.group(0)  # unchanged

        is_embed = match.group(0).startswith("!")
        if is_embed:
            return ""

        # Plain link — use alias if present, otherwise bare base
        alias = parts[1].strip() if len(parts) > 1 else base
        return alias

    return WIKILINK_RE.sub(replacer, content)


# ---------------------------------------------------------------------------
# extract_third_party
# ---------------------------------------------------------------------------


def extract_third_party(in_set_paths: list[Path], vault_root: Path) -> list[dict]:
    """Collect third-party entries from in-set notes with ``spdx:`` frontmatter.

    A note is third-party if its frontmatter carries a truthy ``spdx:`` key
    (D-12).  The ``attribution:`` frontmatter field supplies the attribution
    string for THIRD-PARTY-NOTICES.md.

    Args:
        in_set_paths: List of absolute paths to in-set vault notes.
        vault_root:   Root of the source vault (used to build vault-relative paths).

    Returns:
        A list of dicts ``{path, spdx, attribution}``, sorted by ``path``
        (vault-relative string) for determinism (D-09).  ``attribution`` is
        an empty string if the key is absent.
    """
    results: list[dict] = []
    for note_path in in_set_paths:
        post = frontmatter.load(str(note_path))
        spdx_val = post.metadata.get("spdx")
        if not spdx_val:
            continue
        try:
            rel = str(note_path.relative_to(vault_root))
        except ValueError:
            rel = str(note_path)
        attribution = str(post.metadata.get("attribution") or "")
        results.append(
            {
                "path": rel,
                "spdx": str(spdx_val),
                "attribution": attribution,
            }
        )
    # Sort by path for D-09 determinism
    results.sort(key=lambda e: e["path"])
    return results


# ---------------------------------------------------------------------------
# render_license
# ---------------------------------------------------------------------------


def render_license(
    *,
    template_text: str,
    year: int,
    copyright_holder: str,
    spdx_license_ref: str,
) -> str:
    """Render a LICENSE.md from a per-client template.

    Substitutions (D-13):
    - ``{year}``              → *year*
    - ``{copyright_holder}``  → *copyright_holder*

    After substitution, appends a section ``## {spdx_license_ref}`` that
    defines the identifier for external SPDX scanners.

    Args:
        template_text:    Raw template content (read from ``license-template.md``).
        year:             Publication year (e.g. 2026).
        copyright_holder: The legal entity holding the copyright.
        spdx_license_ref: The ``LicenseRef-*`` identifier string.

    Returns:
        The fully-rendered LICENSE.md content string.

    Raises:
        ``PublishError`` if unsubstituted placeholders remain after rendering
        (defence against mismatched template variables).
    """
    rendered = template_text.replace("{year}", str(year)).replace(
        "{copyright_holder}", copyright_holder
    )

    if "{year}" in rendered or "{copyright_holder}" in rendered:
        raise PublishError(
            "render_license: unsubstituted placeholder(s) remain after template "
            "substitution — check the template for mismatched {year} / "
            "{copyright_holder} markers."
        )

    definition = (
        f"\n\n## {spdx_license_ref}\n\n"
        f"This licence is identified in SPDX as `{spdx_license_ref}`.\n"
        f"It applies to all files in this repository that carry "
        f"`SPDX-License-Identifier: {spdx_license_ref}` in their frontmatter, "
        f"unless a more specific licence is indicated.\n"
    )
    return rendered.rstrip() + definition


# ---------------------------------------------------------------------------
# render_third_party_notices
# ---------------------------------------------------------------------------


def render_third_party_notices(third_party: list[dict]) -> str:
    """Produce a deterministic THIRD-PARTY-NOTICES.md document.

    Each entry in *third_party* (as returned by :func:`extract_third_party`)
    is rendered as a section showing the note path, ``SPDX-License-Identifier``,
    and attribution text.  Entries are sorted by path (determinism, D-09).

    Args:
        third_party: List of dicts ``{path, spdx, attribution}`` as produced
                     by :func:`extract_third_party`.  May be empty.

    Returns:
        A complete THIRD-PARTY-NOTICES.md string.  If *third_party* is empty,
        returns a minimal doc stating no third-party content is present
        (still byte-stable, D-09).
    """
    lines = ["# Third-Party Notices", ""]

    if not third_party:
        lines.append("No third-party content in this publish.")
        lines.append("")
        return "\n".join(lines)

    sorted_entries = sorted(third_party, key=lambda e: e["path"])

    for entry in sorted_entries:
        lines.append(f"## {entry['path']}")
        lines.append("")
        lines.append(f"SPDX-License-Identifier: {entry['spdx']}")
        if entry.get("attribution"):
            lines.append(f"Attribution: {entry['attribution']}")
        lines.append("")

    return "\n".join(lines)
