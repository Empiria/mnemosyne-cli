"""Content producers for mnemosyne tech-publish (Phase 49 Plans 01 and 02).

Pure, side-effect-light building blocks:

Plan 01 — content producers:
- ``PublishError``               — domain error for publish failures
- ``content_hash``               — SHA-256 hash of a file (``sha256:<hex>``)
- ``stage_note``                 — copy a source note to dest with SPDX injection
- ``strip_cross_set_wikilinks``  — replace cross-set [[links]] with alias text
- ``extract_third_party``        — collect spdx:/attribution: notes from in-set
- ``render_license``             — substitute placeholders in a licence template
- ``render_third_party_notices`` — build THIRD-PARTY-NOTICES.md from third-party list

Plan 02 — idempotency + provenance layer:
- ``WritePlan``                  — frozen dataclass: to_write, to_delete, has_changes
- ``load_published_json``        — read PUBLISHED.json from publish root (or None)
- ``build_published_json``       — assemble the eight-field D-08 provenance dict
- ``write_published_json``       — write PUBLISHED.json with sort_keys + newline
- ``compute_write_plan``         — diff current source hashes against prior PUBLISHED.json
- ``detect_client_edits``        — compare target files against prior output hashes (D-04)

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
  - PUBLISHED.json uses sort_keys=True for stable key ordering.
  - PUBLISHED.json is explicitly excluded from the determinism scope — it carries
    a wall-clock timestamp (D-09 exemption).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Phase 49 Plan 02 — idempotency + provenance layer (D-04/D-05/D-06/D-07/D-08)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WritePlan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WritePlan:
    """The result of :func:`compute_write_plan`.

    Attributes:
        to_write:  Sorted list of output-relative paths whose source-side hash
                   is new or has changed since the prior publish (D-05).
        to_delete: Sorted list of output-relative paths that were in the prior
                   ``PUBLISHED.json`` but are no longer in the current output
                   set (D-05 deletions).
    """

    to_write: list[str]
    to_delete: list[str]

    @property
    def has_changes(self) -> bool:
        """``True`` if any writes or deletions are needed (D-06)."""
        return bool(self.to_write or self.to_delete)


# ---------------------------------------------------------------------------
# load_published_json
# ---------------------------------------------------------------------------


def load_published_json(publish_root: Path) -> dict | None:
    """Read ``PUBLISHED.json`` from *publish_root*.

    Args:
        publish_root: The publish root directory (e.g. ``imported/empiria/``).

    Returns:
        The parsed JSON dict, or ``None`` if the file does not exist (first
        publish).

    Raises:
        ``PublishError`` if the file exists but cannot be parsed as valid JSON.
    """
    path = publish_root / "PUBLISHED.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishError(
            f"load_published_json: PUBLISHED.json at {path} is not valid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# build_published_json
# ---------------------------------------------------------------------------


def build_published_json(
    *,
    source_vault_sha: str,
    share_manifest_hash: str,
    license_md_hash: str,
    third_party_notices_hash: str,
    file_hashes: dict[str, dict],
) -> dict:
    """Assemble a ``PUBLISHED.json`` provenance dict (D-08).

    The dict carries the eight required D-08 fields:
    - ``schema_version``              — always ``"1.0"``
    - ``publish_timestamp``           — UTC ISO-8601 string ending in ``Z``
      (tz-aware ``datetime.now(timezone.utc)``; no deprecated wall-clock helpers)
    - ``source_vault_sha``            — short git SHA of the source vault HEAD
    - ``share_manifest_hash``         — ``sha256:…`` of the share-manifest
    - ``license_md_hash``             — ``sha256:…`` of rendered LICENSE.md
    - ``third_party_notices_hash``    — ``sha256:…`` of THIRD-PARTY-NOTICES.md
    - ``files``                       — per-file entries with both hashes (D-07)

    Args:
        source_vault_sha:          Short git SHA of the source vault HEAD.
        share_manifest_hash:       Content hash of the share-manifest file.
        license_md_hash:           Content hash of the rendered LICENSE.md.
        third_party_notices_hash:  Content hash of THIRD-PARTY-NOTICES.md.
        file_hashes:               Dict mapping output-relative path to
                                   ``{"source_hash": ..., "output_hash": ...}``.

    Returns:
        The provenance dict ready for :func:`write_published_json`.
    """
    return {
        "schema_version": "1.0",
        "publish_timestamp": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "source_vault_sha": source_vault_sha,
        "share_manifest_hash": share_manifest_hash,
        "license_md_hash": license_md_hash,
        "third_party_notices_hash": third_party_notices_hash,
        "files": file_hashes,
    }


# ---------------------------------------------------------------------------
# write_published_json
# ---------------------------------------------------------------------------


def write_published_json(publish_root: Path, data: dict) -> None:
    """Serialise *data* to ``PUBLISHED.json`` at *publish_root*.

    Uses ``sort_keys=True, indent=2`` for human-readable, stable key ordering
    and appends a trailing newline.  Note: ``sort_keys`` keeps the file
    readable and diff-friendly — the legitimately-varying fields
    (``publish_timestamp``, ``source_vault_sha``) sit near the bottom of the
    alphabetical sort, making them easy to spot in diffs (D-09 exemption).

    Args:
        publish_root: Directory where ``PUBLISHED.json`` will be written.
        data:         Provenance dict as returned by :func:`build_published_json`.
    """
    path = publish_root / "PUBLISHED.json"
    path.write_text(
        json.dumps(data, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# compute_write_plan
# ---------------------------------------------------------------------------


def compute_write_plan(
    current_sources: dict[str, str],
    prior_published: dict | None,
) -> WritePlan:
    """Compute which output files need to be written or deleted (D-05).

    Compares *current_sources* (output-relative-path → current source-side
    ``content_hash``) against the hashes recorded in *prior_published* and
    returns a :class:`WritePlan`.

    Rules:
    - If *prior_published* is ``None`` (first publish): every current path is
      ``to_write``; nothing to delete.
    - Otherwise, a path is ``to_write`` if it is new OR if its current source
      hash differs from ``prior_published["files"][path]["source_hash"]``.
    - A path is ``to_delete`` if it appears in ``prior_published["files"]``
      but NOT in *current_sources* (file removed from the output set).
    - Both ``to_write`` and ``to_delete`` are sorted for determinism (D-09).

    Args:
        current_sources:  Maps output-relative path → current ``content_hash``.
        prior_published:  The loaded ``PUBLISHED.json`` dict, or ``None``.

    Returns:
        A :class:`WritePlan` with sorted ``to_write`` and ``to_delete`` lists.
    """
    if prior_published is None:
        return WritePlan(
            to_write=sorted(current_sources.keys()),
            to_delete=[],
        )

    prior_files: dict[str, dict] = prior_published.get("files", {})

    to_write: list[str] = []
    for rel, current_hash in current_sources.items():
        prior_entry = prior_files.get(rel)
        if prior_entry is None or prior_entry.get("source_hash") != current_hash:
            to_write.append(rel)

    to_delete: list[str] = [
        rel for rel in prior_files if rel not in current_sources
    ]

    return WritePlan(
        to_write=sorted(to_write),
        to_delete=sorted(to_delete),
    )


# ---------------------------------------------------------------------------
# detect_client_edits
# ---------------------------------------------------------------------------


def detect_client_edits(
    publish_root: Path,
    prior_published: dict | None,
    *,
    force: bool,
) -> list[str]:
    """Detect files in the publish subtree that a client has edited or deleted (D-04).

    Compares the current on-disk content of each file recorded in
    *prior_published*[``"files"``] against the stored ``output_hash`` (the
    hash of the file as Empiria last wrote it).  A mismatch means the client
    modified or deleted the file.

    NOTE: Only the entries in ``"files"`` are checked.  Sibling files such as
    ``LICENSE.md`` and ``THIRD-PARTY-NOTICES.md`` are Empiria-generated
    artefacts and are not client-edit-protected (49-RESEARCH Open Q3).

    Args:
        publish_root:     The publish root directory.
        prior_published:  The loaded ``PUBLISHED.json`` dict, or ``None`` for a
                          first publish (returns ``[]`` immediately — nothing to
                          protect yet).
        force:            If ``True``, return the edited list without raising
                          (caller takes responsibility).  If ``False`` and the
                          list is non-empty, raise ``PublishError``.

    Returns:
        A list of strings describing edited/deleted files.  Deleted entries are
        reported as ``"<rel> (deleted)"``.  Returns ``[]`` when nothing has
        changed (or *prior_published* is ``None``).

    Raises:
        ``PublishError`` if client edits are detected and *force* is ``False``.
    """
    if prior_published is None:
        return []

    prior_files: dict[str, dict] = prior_published.get("files", {})
    edited: list[str] = []

    for rel, info in prior_files.items():
        target = publish_root / rel
        expected_hash = info.get("output_hash", "")

        if not target.exists():
            edited.append(f"{rel} (deleted)")
        elif content_hash(target) != expected_hash:
            edited.append(rel)

    if edited and not force:
        paths_listed = "\n  ".join(edited)
        raise PublishError(
            f"detect_client_edits: the following files in the publish subtree "
            f"have been modified or deleted since the last publish:\n"
            f"  {paths_listed}\n"
            f"Re-run with --force to overwrite, or restore the files first."
        )

    return edited
