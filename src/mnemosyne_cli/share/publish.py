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
import os
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable
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
# published_relpath — client-facing path mapping
# ---------------------------------------------------------------------------

# Empiria's four knowledge-note content types (technologies/knowledge-standards.md)
# are encoded as directory tiers (technologies/<tech>/<type>/<note>.md). Those
# tier names leak the internal taxonomy into a client publish, so they are
# dropped from the published path — the same concern as stripping `type:` from
# the staged frontmatter.
_KNOWLEDGE_TYPE_DIRS: frozenset[str] = frozenset(
    {"reference", "learning", "decision", "standard"}
)

# Top-level vault category directories stripped from the published path — they
# expose the vault's internal organisation rather than anything the client needs.
_VAULT_CATEGORY_DIRS: frozenset[str] = frozenset({"technologies"})


def published_relpath(source_rel: str) -> str:
    """Map a source vault-relative note path to its client-facing published path.

    Two layers of Empiria-internal structure are stripped so the publish does
    not leak the vault's organisation:

    1. A *leading* top-level category directory (``technologies``).
    2. Any *intermediate* knowledge-type directory tier (``reference`` /
       ``learning`` / ``decision`` / ``standard``).

    The filename is always kept; the tech name (and any other directories) are
    preserved.

    Example: ``technologies/anvil/reference/playwright-patterns.md`` →
    ``anvil/playwright-patterns.md``.

    Note: short-form Obsidian links (``[[name]]``) are unaffected by flattening
    (they resolve by basename). A full-path in-set link that embeds the stripped
    structure (``[[technologies/anvil/reference/forms]]``) is NOT rewritten and
    would not resolve post-flatten — callers relying on that should prefer
    short-form links.

    Args:
        source_rel: Vault-relative POSIX path of the source note.

    Returns:
        The client-facing vault-relative POSIX path.
    """
    parts = source_rel.split("/")
    if len(parts) <= 1:
        return source_rel
    # 1. Drop a leading category dir (keep the filename if that's all there is).
    if parts[0] in _VAULT_CATEGORY_DIRS and len(parts) > 1:
        parts = parts[1:]
    # 2. Drop intermediate knowledge-type tiers.
    kept = [
        p
        for i, p in enumerate(parts)
        if not (i < len(parts) - 1 and p in _KNOWLEDGE_TYPE_DIRS)
    ]
    return "/".join(kept)


# ---------------------------------------------------------------------------
# stage_note
# ---------------------------------------------------------------------------


def stage_note(
    source_path: Path,
    dest_path: Path,
    *,
    client_spdx_identifier: str,
    copyright_text: str,
    content_override: str | None = None,
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
        content_override:      If provided, parse frontmatter from this string
                               instead of reading *source_path* from disk.  Used
                               by the strip path to inject SPDX into already-
                               transformed content without re-reading the source.

    Returns:
        The UTF-8-encoded bytes of the staged file content.
    """
    if content_override is not None:
        post = frontmatter.loads(content_override)
    else:
        post = frontmatter.load(str(source_path))

    # D-12: third-party override — use the note's own spdx: value if present.
    # Read it from the SOURCE metadata before discarding the rest.
    source_spdx = post.metadata.get("spdx")
    if source_spdx:
        spdx_identifier = str(source_spdx)
    else:
        spdx_identifier = client_spdx_identifier

    # Client-facing output carries ONLY the two SPDX fields. All source
    # frontmatter (type, tags, created/updated, internal provenance) is dropped
    # so the publish does not leak Empiria's internal note schema. A fresh Post
    # built from the body is the single source of the staged metadata.
    staged = frontmatter.Post(post.content)
    staged["SPDX-License-Identifier"] = spdx_identifier
    staged["SPDX-FileCopyrightText"] = copyright_text

    content = frontmatter.dumps(staged)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    return content.encode("utf-8")


# ---------------------------------------------------------------------------
# strip_cross_set_wikilinks
# ---------------------------------------------------------------------------


def strip_cross_set_wikilinks(
    content: str,
    breach_targets: set[str],
    *,
    resolver: Callable[[str], str | None] | None = None,
) -> str:
    """Replace wikilinks to *breach_targets* with alias/display text.

    For each wikilink in *content*:
    - If its target is a breach target:
      - Embed (``![[...]]``): replaced with empty string.
      - Plain link with alias (``[[target|alias]]``): replaced with alias.
      - Plain link without alias (``[[target]]``): replaced with the bare
        base path.
    - Otherwise the wikilink is left unchanged.

    A link's base is a breach target if its literal base path is in
    *breach_targets* OR — when *resolver* is supplied — if resolving the base
    yields a vault-relative path whose extensionless form is in the set.

    The resolver is what makes short-form Obsidian links work. *breach_targets*
    holds full extensionless vault-relative paths (e.g.
    ``technologies/anvil/reference/anvil-uplink-testing``), but the common
    Obsidian link is the bare basename (``[[anvil-uplink-testing]]``), which
    never matches literally. Passing the walker's resolver (``_index_vault`` +
    ``_resolve``) maps each link to its real target before the membership test,
    so short-form and path-qualified links are both neutralised. Without a
    resolver the function falls back to literal matching (path-qualified links
    only) — preserving the original Phase 49 behaviour for existing callers.

    Reuses the Phase 48 ``WIKILINK_RE`` parser from ``share/wikilinks.py``
    (D-49-12).

    Args:
        content:        Raw markdown note body (may include frontmatter text).
        breach_targets: Set of full extensionless vault-relative breach paths.
        resolver:       Optional callable mapping a wikilink base to its
                        resolved vault-relative path (``.md``-suffixed) or
                        ``None``. Use the walker's resolution so short-form
                        links are matched.

    Returns:
        The modified content string.
    """

    def _is_breach_target(base: str) -> bool:
        if base in breach_targets:
            return True
        if resolver is not None:
            resolved = resolver(base)
            if resolved is not None:
                norm = resolved[:-3] if resolved.endswith(".md") else resolved
                return norm in breach_targets
        return False

    def replacer(match: re.Match) -> str:  # type: ignore[type-arg]
        inner = match.group(1)
        parts = inner.split("|", 1)
        raw_target = parts[0].strip()
        # Strip heading / block anchor to get the base note path
        base = raw_target.split("#", 1)[0].split("^", 1)[0].strip()

        if not _is_breach_target(base):
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
            "%Y-%m-%dT%H:%M:%S.%fZ"
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

    ``"files"`` keys are client-facing published paths (see
    :func:`published_relpath`), so they locate the on-disk file under
    *publish_root* directly.

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


# ---------------------------------------------------------------------------
# Phase 49 Plan 03 — preconditions, deploy-key resolution, git helpers
# ---------------------------------------------------------------------------

_SECRETS_PATH = Path("~/.config/mnemosyne/secrets.toml").expanduser()


def resolve_deploy_key(deploy_key_ref: str) -> Path:
    """Resolve a deploy-key reference to an absolute filesystem path.

    Reads ``~/.config/mnemosyne/secrets.toml`` and looks up
    ``data["deploy_keys"][deploy_key_ref]``.

    **Security (T-49-03-01):** The resolved path is returned as a ``Path``
    object; it MUST be used ONLY in ``GIT_SSH_COMMAND``.  It is NEVER written
    to any vault file, NEVER passed to ``print``/``console``/``error_console``,
    and NEVER written to any log.

    Args:
        deploy_key_ref: The symbolic key reference from ``[direct].deploy_key_ref``
                        in the share-manifest.

    Returns:
        Absolute ``Path`` to the SSH private key file.

    Raises:
        ``PublishError`` with an actionable message if ``secrets.toml`` is
        missing or if ``deploy_key_ref`` is absent from ``[deploy_keys]``.
    """
    if not _SECRETS_PATH.exists():
        raise PublishError(
            f"resolve_deploy_key: secrets.toml not found at {_SECRETS_PATH}.\n"
            f"Create it with the following TOML block:\n\n"
            f"[deploy_keys]\n"
            f'{deploy_key_ref} = "/path/to/ssh/private/key"\n'
        )
    try:
        with open(_SECRETS_PATH, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise PublishError(
            f"resolve_deploy_key: failed to parse {_SECRETS_PATH}: {exc}"
        ) from exc

    deploy_keys = data.get("deploy_keys", {})
    raw = deploy_keys.get(deploy_key_ref)
    if not raw:
        raise PublishError(
            f"resolve_deploy_key: key ref '{deploy_key_ref}' not found in "
            f"{_SECRETS_PATH} [deploy_keys].\n"
            f"Add the following to {_SECRETS_PATH}:\n\n"
            f"[deploy_keys]\n"
            f'{deploy_key_ref} = "/path/to/ssh/private/key"\n'
        )
    key_path = Path(raw).expanduser().resolve()
    if not key_path.is_file():
        raise PublishError(
            f"resolve_deploy_key: the key file for ref '{deploy_key_ref}' does not "
            f"exist or is not a regular file.\n"
            f"Check the [deploy_keys] entry in {_SECRETS_PATH} and ensure the path "
            f"points to a valid SSH private key file."
        )
    return key_path


def check_review_gate(manifest, *, skip_review_check: bool) -> None:
    """Enforce the licence-template review precondition (D-10).

    Reads ``manifest.license["license_template_reviewed_at"]``.  If absent or
    empty and ``skip_review_check`` is False, raises ``PublishError``.
    If ``skip_review_check`` is True, the gate is bypassed — callers MUST emit
    a loud warning after calling this function.

    Args:
        manifest:           Validated :class:`~mnemosyne_cli.share.manifest.ShareManifest`.
        skip_review_check:  If True, bypass the gate (loud-warning path).

    Raises:
        ``PublishError`` if the review field is absent/empty and
        ``skip_review_check`` is False.
    """
    if skip_review_check:
        return

    license_block = manifest.license or {}
    reviewed_at = license_block.get("license_template_reviewed_at")
    if not reviewed_at:
        raise PublishError(
            "check_review_gate: the share-manifest [license] block is missing "
            "'license_template_reviewed_at:' (D-10).\n"
            "Review the licence template against the master agreement and add:\n\n"
            "  [license]\n"
            "  license_template_reviewed_at = \"YYYY-MM-DD\"\n\n"
            "Or pass --skip-review-check to bypass (loud warning will be emitted)."
        )


def check_target_registered(manifest, config: dict) -> Path:
    """Verify the target vault is registered and cross-vault write is permitted (D-02).

    Resolves ``manifest.direct["target_vault"]`` against the ``[vaults.*]``
    registry in *config*, then asserts that the ``empiria`` vault
    ``can_read`` the target (or the rule grants write access).

    Args:
        manifest:   Validated :class:`~mnemosyne_cli.share.manifest.ShareManifest`.
        config:     Full config dict (as returned by ``lib.vault._read_config()``).

    Returns:
        Absolute :class:`Path` to the target vault's local directory.

    Raises:
        ``PublishError`` if the vault is unregistered or the cross-vault rule
        does not permit the write.
    """
    from mnemosyne_cli.lib.vault import vault_by_name, can_read as vault_can_read

    direct = manifest.direct or {}
    target_vault_name = direct.get("target_vault", "")
    if not target_vault_name:
        raise PublishError(
            "check_target_registered: manifest [direct].target_vault is missing."
        )

    vc = vault_by_name(target_vault_name)
    if vc is None:
        raise PublishError(
            f"check_target_registered: target vault '{target_vault_name}' is not "
            f"registered in config.toml [vaults.*] (D-02).\n"
            f"Register it with:\n"
            f"  mnemosyne vault add {target_vault_name} /path/to/local/clone"
        )

    # Cross-vault authorisation: empiria must be allowed to read/write target
    if not vault_can_read("empiria", target_vault_name):
        raise PublishError(
            f"check_target_registered: cross-vault write not authorised — "
            f"config.toml [[vault_rules]] does not grant 'empiria' can_read "
            f"'{target_vault_name}' (D-02 / security).\n"
            f"Add to ~/.config/mnemosyne/config.toml:\n\n"
            f"[[vault_rules]]\n"
            f'from = "empiria"\n'
            f'can_read = ["{target_vault_name}"]\n'
        )

    return vc.path.expanduser().resolve()


def is_worktree_dirty_outside(repo_path: Path, subtree: str) -> list[str]:
    """Return a list of dirty paths outside *subtree* in *repo_path*.

    Used by :func:`check_worktree_clean` to determine whether blocking dirty
    files exist outside the publish subtree.

    Args:
        repo_path: Path to the git repository.
        subtree:   Vault-relative publish subtree prefix.

    Returns:
        List of dirty path strings outside *subtree*.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    prefix = subtree.rstrip("/") + "/"
    dirty_outside: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        if path_part.startswith(prefix):
            continue
        dirty_outside.append(path_part)
    return dirty_outside


def check_worktree_clean(target_vault_path: Path, subtree: str) -> None:
    """Refuse to publish if the target repo has uncommitted changes outside the subtree (D-02).

    Runs ``git status --porcelain`` in *target_vault_path*; any path NOT under
    *subtree* (and not ``PUBLISHED.json``) is a blocking dirty file.

    Args:
        target_vault_path: Absolute path to the target vault's local clone.
        subtree:           Vault-relative publish subtree (e.g. ``"imported/empiria"``).

    Raises:
        ``PublishError`` listing any dirty paths outside the subtree.
    """
    dirty_outside = is_worktree_dirty_outside(target_vault_path, subtree)
    if dirty_outside:
        paths_listed = "\n  ".join(dirty_outside)
        raise PublishError(
            f"check_worktree_clean: target vault at {target_vault_path} has "
            f"uncommitted changes outside the publish subtree '{subtree}' (D-02):\n"
            f"  {paths_listed}\n"
            f"Commit or stash those changes before publishing."
        )


def validate_publish_root_under_target(target_vault_path: Path, subtree: str) -> Path:
    """Compute and validate the publish root is inside the target vault (path-traversal guard).

    Args:
        target_vault_path: Absolute path to the target vault root.
        subtree:           Vault-relative publish subtree (e.g. ``"imported/empiria"``).

    Returns:
        Resolved absolute :class:`Path` to the publish root.

    Raises:
        ``PublishError`` if the resolved path escapes the target vault root
        (e.g. a ``../`` traversal attempt).
    """
    resolved_vault = target_vault_path.resolve()
    publish_root = (resolved_vault / subtree).resolve()
    if not (publish_root == resolved_vault or publish_root.is_relative_to(resolved_vault)):
        raise PublishError(
            f"validate_publish_root_under_target: resolved publish root "
            f"{publish_root} is OUTSIDE the target vault {resolved_vault}. "
            f"Possible path traversal via subtree='{subtree}' — aborting."
        )
    return publish_root


def get_short_sha(repo_path: Path) -> str:
    """Return the short (7-char) git SHA of HEAD for *repo_path*.

    Args:
        repo_path: Path to the git repository root.

    Returns:
        Short hex SHA string (e.g. ``"abc1234"``).
    """
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_commit(repo_path: Path, paths: list[str], message: str) -> bool:
    """Stage *paths* and create a git commit in *repo_path*.

    Args:
        repo_path: Working directory for the commit (the target vault root).
        paths:     List of relative paths to stage (``git add``).
        message:   Commit message.

    Returns:
        True if a commit was created, False if nothing changed to commit.
    """
    subprocess.run(
        ["git", "add", "--"] + paths,
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    # Scope the commit itself to the explicit pathspec (not just the staging).
    # In local mode the target is an actively-developed client code repo whose
    # index may already carry unrelated staged changes; a bare `git commit`
    # would sweep those into the Empiria publish commit. Committing with `--
    # <paths>` commits ONLY the publish artefacts and leaves any other staged
    # work untouched (completes the WR-02 intent for the in-repo case).
    result = subprocess.run(
        ["git", "commit", "-m", message, "--"] + paths,
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # "nothing to commit" is not an error for our purposes
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            return False
        raise subprocess.CalledProcessError(
            result.returncode, result.args,
            output=result.stdout, stderr=result.stderr,
        )
    return True


def git_push_with_deploy_key(repo_path: Path, key_path: Path) -> None:
    """Push ``origin main`` using a deploy key via ``GIT_SSH_COMMAND`` (D-01/D-03).

    The deploy key path is injected ONLY into the subprocess environment as
    ``GIT_SSH_COMMAND``.  It is NEVER logged, printed, or written to any file.
    ``-o StrictHostKeyChecking=accept-new`` is included so the first push to a
    fresh clone succeeds without interactive host-key confirmation (Finding 2).

    Args:
        repo_path: Working directory for the push (the target vault root).
        key_path:  Absolute path to the SSH private key file (T-49-03-01).
    """
    env = os.environ.copy()
    # GIT_SSH_COMMAND is the ONLY consumer of key_path (T-49-03-01).
    # shlex.quote ensures paths with spaces or shell metacharacters are safe
    # when git passes this string to /bin/sh -c.
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {shlex.quote(str(key_path))} -o StrictHostKeyChecking=accept-new"
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def derive_scope_summary(written: list[str], deleted: list[str]) -> str:
    """Build a human-readable commit-message scope summary.

    Produces something like ``"3 files (technologies, clients)"`` — file count
    plus the top-3 top-level directories touched, for use in the structured
    commit message ``"Empiria publish: {scope_summary}, source @ {sha}"``.

    Args:
        written:  List of output-relative paths that were written.
        deleted:  List of output-relative paths that were deleted.

    Returns:
        A short summary string.
    """
    all_paths = written + deleted
    total = len(all_paths)
    # Collect distinct top-level directory names
    top_dirs: list[str] = []
    seen: set[str] = set()
    for p in all_paths:
        parts = p.split("/")
        top = parts[0] if parts else ""
        if top and top not in seen:
            seen.add(top)
            top_dirs.append(top)
    top_dirs = sorted(top_dirs)[:3]
    noun = "file" if total == 1 else "files"
    if top_dirs:
        return f"{total} {noun} ({', '.join(top_dirs)})"
    return f"{total} {noun}"


# ---------------------------------------------------------------------------
# Phase 49 Plan 03 — run_publish orchestrator (Task 2a)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    """Result of a :func:`run_publish` call.

    Attributes:
        success:   True if the operation completed without error.
        published: True if a new commit was created (and, in vault mode, pushed);
                   also True for a successful ``--no-commit`` working-tree write.
        message:   Human-readable summary message.
    """

    success: bool
    published: bool
    message: str


def run_publish(
    *,
    client: str,
    force: bool,
    skip_review_check: bool,
    dry_run: bool,
    into: str | Path | None = None,
    commit: bool = True,
) -> "PublishResult":
    """End-to-end publish orchestrator — §4.3 steps 5–9 pipeline.

    Two target mechanisms (Phase 50):

    - **LOCAL MODE (default for in-repo colocation)** — pass *into* pointing at
      a local git clone (e.g. a client code repo). The published slice is
      written under ``<into>/<target_subtree>`` and committed there, and the
      function returns; **the push is NOT performed** — transport is the
      operator's responsibility. The ``vault_can_read`` gate and the
      clean-worktree-outside-subtree check do not apply (the target is a plain
      git repo being actively developed, not a registered vault). No deploy key
      is required. A manifest whose ``[direct]`` block omits ``target_vault`` is
      local-only and *requires* *into*.
    - **VAULT MODE (legacy direct publish)** — *into* is ``None`` and the
      manifest names a registered ``[direct].target_vault``: resolve it, enforce
      ``[[vault_rules]]``, commit, and push ``origin main`` via the deploy key.

    Pipeline (49-RESEARCH Pattern 2):

    1. Resolve vault_root (empiria) + load config. Load manifest.
    2. ``check_review_gate`` (D-10); warn if skip_review_check.
    3. Resolve target: *into* (local mode) or ``check_target_registered``
       (vault mode); ``validate_publish_root_under_target``.
    4. ``check_worktree_clean`` (D-02) — vault mode only, unless dry_run.
    5. Load ``PUBLISHED.json``. ``detect_client_edits`` (D-04).
    6. ``walk_manifest`` (Phase 48). Apply policy: refuse / warn / strip (D-49-12).
    7. Compute ``current_sources``. ``compute_write_plan`` (D-05).
    8. NO-OP GATE (D-06) — must precede all writes.
    9. Stage + LICENSE + THIRD-PARTY + PUBLISHED.json (guarded by dry_run).
    10. If dry_run → print plan, return published=False.
    11. commit (local + vault) + deploy-key push (vault mode only, D-03).

    Args:
        client:             Client slug (e.g. ``"friendly-fox"``).
        force:              Override detect-and-refuse (D-04).
        skip_review_check:  Bypass licence review gate (loud warning; D-10).
        dry_run:            Show the plan, write nothing to the target.
        into:               Local clone path → LOCAL MODE (write + commit, no
                            push). ``None`` → VAULT MODE.
        commit:             If ``False``, write the slice into the working tree
                            but do not stage/commit it (operator commits). Only
                            meaningful with *into*.

    Returns:
        :class:`PublishResult`.

    Raises:
        :class:`PublishError`: On any precondition or pipeline failure.
    """
    from rich.console import Console
    from mnemosyne_cli.lib.vault import resolve_vault_path, _read_config
    from mnemosyne_cli.share.manifest import load_manifest, ManifestError
    from mnemosyne_cli.share.walker import walk_manifest

    console = Console()
    error_console = Console(stderr=True, style="bold red")

    # -----------------------------------------------------------------------
    # Step 1: Resolve vault_root + config; load manifest
    # -----------------------------------------------------------------------
    vault_root = resolve_vault_path()
    config = _read_config()

    manifest_path = vault_root / "clients" / client / "share-manifest.toml"
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        raise PublishError(f"run_publish: failed to load manifest: {exc}") from exc

    # -----------------------------------------------------------------------
    # WR-06: Mode gate — only direct mode is implemented in this command
    # -----------------------------------------------------------------------
    if manifest.mode != "direct":
        raise PublishError(
            f"run_publish: manifest mode is '{manifest.mode}', but this command "
            f"only supports mode='direct'.\n"
            f"The intermediary-mode publish flow is not yet implemented — use a "
            f"direct-mode manifest or wait for the intermediary command."
        )

    # -----------------------------------------------------------------------
    # Step 2: Review gate (D-10)
    # -----------------------------------------------------------------------
    check_review_gate(manifest, skip_review_check=skip_review_check)
    if skip_review_check:
        error_console.print(
            "[bold yellow]WARNING:[/bold yellow] --skip-review-check is active — "
            "the licence template has NOT been verified against the master agreement. "
            "Proceeding anyway."
        )

    # -----------------------------------------------------------------------
    # Step 3: Resolve publish target + publish-root validation (D-02)
    #
    # --into forces LOCAL MODE: write + commit into a local clone, no push,
    # no vault_can_read gate. With no --into, a manifest carrying a
    # target_vault uses VAULT MODE (legacy: registered vault + deploy-key push);
    # a manifest without target_vault is local-only and requires --into.
    # -----------------------------------------------------------------------
    direct = manifest.direct or {}
    target_subtree = direct.get("target_subtree", "imported/empiria")

    if into is not None:
        local_mode = True
        target_root = Path(into).expanduser().resolve()
        if not target_root.is_dir():
            raise PublishError(
                f"run_publish: --into path does not exist or is not a directory: "
                f"{target_root}"
            )
        if not (target_root / ".git").exists():
            raise PublishError(
                f"run_publish: --into path is not a git repository (no .git): "
                f"{target_root}\n"
                f"tech-publish commits the published slice locally; point --into "
                f"at a git clone."
            )
    elif direct.get("target_vault"):
        local_mode = False
        target_root = check_target_registered(manifest, config)
    else:
        raise PublishError(
            "run_publish: manifest [direct] has no 'target_vault' (local-mode "
            "manifest), but no --into <path> was supplied.\n"
            "Pass the local clone to publish into:\n"
            f"  mnemosyne tech-publish --client {client} --into /path/to/clone"
        )

    publish_root = validate_publish_root_under_target(target_root, target_subtree)

    # -----------------------------------------------------------------------
    # Step 4: Dirty-worktree check (D-02) — VAULT MODE only, skipped in dry_run.
    # In local mode the target is an actively-developed code repo whose working
    # tree is routinely dirty; we stage only explicit subtree paths at commit
    # time (and commit with an explicit pathspec), so a dirty tree elsewhere is
    # fine and must not block.
    # -----------------------------------------------------------------------
    if not dry_run and not local_mode:
        check_worktree_clean(target_root, target_subtree)

    # -----------------------------------------------------------------------
    # Step 5: Load PUBLISHED.json + detect client edits (D-04)
    # -----------------------------------------------------------------------
    prior = load_published_json(publish_root)
    client_edits = detect_client_edits(publish_root, prior, force=force)

    # -----------------------------------------------------------------------
    # Step 6: Walk manifest + apply policy (Phase 48 walker)
    # -----------------------------------------------------------------------
    walk = walk_manifest(manifest, vault_root)
    policy = manifest.policy

    if policy == "refuse" and walk.has_breaches:
        breach_list = "\n  ".join(walk.excluded + walk.breach)
        raise PublishError(
            f"run_publish: policy='refuse' — publish blocked by closure breaches "
            f"(D-49-12):\n  {breach_list}"
        )
    elif policy == "warn" and walk.has_breaches:
        breach_list = "\n  ".join(walk.excluded + walk.breach)
        console.print(
            f"[bold yellow]WARN:[/bold yellow] closure breaches detected (policy='warn'), "
            f"proceeding with in_set only:\n  {breach_list}"
        )

    # Build breach_targets set for strip policy.
    # walk.excluded and walk.breach are .md-suffixed vault-relative paths;
    # strip_cross_set_wikilinks matches against the extensionless wikilink base,
    # so we strip the .md suffix when building the set (CR-01 fix).
    breach_targets: set[str] = set()
    strip_resolver: Callable[[str], str | None] | None = None
    if policy == "strip":
        breach_targets = {
            p[:-3] if p.endswith(".md") else p
            for p in set(walk.excluded) | set(walk.breach)
        }
        # Resolve wikilinks the SAME way the walker did, so short-form links
        # (`[[anvil-uplink-testing]]`) — not just path-qualified ones — match a
        # breach target and get stripped. Reuses the walker's basename index and
        # D-01 resolution. The walk above already succeeded, so no in-closure
        # link is ambiguous; guard defensively anyway.
        from mnemosyne_cli.share.walker import (
            AmbiguousLinkError,
            _index_vault,
            _resolve,
        )

        _strip_index = _index_vault(vault_root)

        def strip_resolver(target: str) -> str | None:
            try:
                return _resolve(target, vault_root, _strip_index)
            except AmbiguousLinkError:
                return None

    # -----------------------------------------------------------------------
    # Step 7: Map in-set source notes to their client-facing published paths,
    #         then compute current hashes + write plan (D-05).
    #
    # The whole diff pipeline and PUBLISHED.json key on the PUBLISHED path (not
    # the source path) so the client artefact never leaks the source layout
    # (the stripped category/knowledge-type dirs). `pub_to_source` is the 1:1
    # inverse used to read source content for staging.
    #
    # Guard: flattening must not collapse two distinct source notes onto the
    # same published path (e.g. anvil/reference/x.md and anvil/learning/x.md
    # both → anvil/x.md). Fail fast rather than silently overwrite.
    # -----------------------------------------------------------------------
    pub_to_source: dict[str, str] = {}
    current_sources: dict[str, str] = {}
    for rel_str in walk.in_set:
        source_abs = vault_root / rel_str
        if not source_abs.exists():
            continue
        pub = published_relpath(rel_str)
        if pub in pub_to_source and pub_to_source[pub] != rel_str:
            raise PublishError(
                f"run_publish: publish-path collision — '{pub_to_source[pub]}' and "
                f"'{rel_str}' both map to '{pub}' after stripping category / "
                f"knowledge-type directories. Rename one source note, or narrow "
                f"the include set."
            )
        pub_to_source[pub] = rel_str
        current_sources[pub] = content_hash(source_abs)

    plan = compute_write_plan(current_sources, prior)

    # -----------------------------------------------------------------------
    # Step 8: NO-OP GATE (D-06) — must precede all writes
    # When force=True and client edits exist, bypass gate (D-04 overwrite needed)
    # -----------------------------------------------------------------------
    if not plan.has_changes and prior is not None and not (force and client_edits):
        console.print("nothing to publish")
        return PublishResult(
            success=True,
            published=False,
            message="nothing to publish",
        )

    # When force=True with client edits, we need to re-write all edited files.
    # Intentional: rewriting ALL in-set files on --force restores every file to
    # canonical Empiria content, not just the one the client touched.  This is
    # the desired behaviour — a force-publish is a full authoritative overwrite,
    # not a selective patch (WR-05).
    if force and client_edits and not plan.has_changes:
        plan = compute_write_plan(current_sources, None)

    # -----------------------------------------------------------------------
    # Step 9: Stage notes + render LICENSE + THIRD-PARTY + PUBLISHED.json
    #         dry_run: no writes to target
    # -----------------------------------------------------------------------
    if dry_run:
        # Show the plan without writing anything
        console.print(
            f"[bold]dry-run:[/bold] would write {len(plan.to_write)} file(s), "
            f"delete {len(plan.to_delete)} file(s)"
        )
        if plan.to_write:
            console.print("  to write: " + ", ".join(plan.to_write[:5]) +
                          (" …" if len(plan.to_write) > 5 else ""))
        if plan.to_delete:
            console.print("  to delete: " + ", ".join(plan.to_delete[:5]) +
                          (" …" if len(plan.to_delete) > 5 else ""))
        if breach_targets:
            console.print(f"  breach summary: {len(breach_targets)} cross-set link target(s) to strip")
        return PublishResult(
            success=True,
            published=False,
            message="dry-run complete",
        )

    # --- Not dry_run: actually write ---
    license_block = manifest.license or {}
    spdx_license_ref = license_block.get("spdx_license_ref", "LicenseRef-Unknown")
    copyright_holder = license_block.get("copyright_holder", "Unknown")
    year = datetime.now(timezone.utc).year
    copyright_text = f"Copyright (c) {year} {copyright_holder}"
    client_spdx_identifier = spdx_license_ref

    in_set_paths: list[Path] = []
    file_hashes: dict[str, dict] = {}

    # Carry forward unchanged entries from prior
    if prior:
        file_hashes.update(prior.get("files", {}))

    # plan.to_write/to_delete carry PUBLISHED paths; pub_to_source maps each
    # back to its source note for reading content.
    for pub_rel in plan.to_write:
        source_rel = pub_to_source.get(pub_rel)
        if source_rel is None:
            continue
        source_abs = vault_root / source_rel
        if not source_abs.exists():
            continue
        dest_abs = publish_root / pub_rel
        dest_abs.parent.mkdir(parents=True, exist_ok=True)

        # Apply strip transform for 'strip' policy, then stage via stage_note
        # so both paths share a single SPDX-injection code path (WR-03).
        if policy == "strip":
            raw_content = source_abs.read_text(encoding="utf-8")
            stripped_content = strip_cross_set_wikilinks(
                raw_content, breach_targets, resolver=strip_resolver
            )
            staged_bytes = stage_note(
                source_abs,
                dest_abs,
                client_spdx_identifier=client_spdx_identifier,
                copyright_text=copyright_text,
                content_override=stripped_content,
            )
        else:
            staged_bytes = stage_note(
                source_abs,
                dest_abs,
                client_spdx_identifier=client_spdx_identifier,
                copyright_text=copyright_text,
            )

        in_set_paths.append(source_abs)
        src_hash = content_hash(source_abs)
        out_hash = "sha256:" + hashlib.sha256(staged_bytes).hexdigest()
        file_hashes[pub_rel] = {
            "source_hash": src_hash,
            "output_hash": out_hash,
        }

    # Also populate in_set_paths for notes NOT in plan.to_write (unchanged)
    for pub_rel, source_rel in pub_to_source.items():
        if pub_rel not in plan.to_write:
            source_abs = vault_root / source_rel
            if source_abs.exists():
                in_set_paths.append(source_abs)

    # Delete removed files from publish_root (keys are client-facing paths)
    for pub_rel in plan.to_delete:
        dest_abs = publish_root / pub_rel
        if dest_abs.exists():
            dest_abs.unlink()
        file_hashes.pop(pub_rel, None)

    # Render LICENSE.md
    template_path_rel = license_block.get("template", "")
    if template_path_rel:
        template_path = vault_root / template_path_rel
    else:
        # Fallback: look in clients/{client}/license-template.md
        template_path = vault_root / "clients" / client / "license-template.md"

    if template_path.exists():
        template_text = template_path.read_text(encoding="utf-8")
        license_content = render_license(
            template_text=template_text,
            year=year,
            copyright_holder=copyright_holder,
            spdx_license_ref=spdx_license_ref,
        )
    else:
        license_content = (
            f"# License\n\nSPDX-License-Identifier: {spdx_license_ref}\n"
            f"Copyright (c) {year} {copyright_holder}\n"
        )
    license_path = publish_root / "LICENSE.md"
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_text(license_content, encoding="utf-8")

    # Render THIRD-PARTY-NOTICES.md
    third_party = extract_third_party(in_set_paths, vault_root)
    tpn_content = render_third_party_notices(third_party)
    tpn_path = publish_root / "THIRD-PARTY-NOTICES.md"
    tpn_path.write_text(tpn_content, encoding="utf-8")

    # Build + write PUBLISHED.json
    source_vault_sha = get_short_sha(vault_root)
    manifest_hash = content_hash(manifest_path)
    license_hash = "sha256:" + hashlib.sha256(license_content.encode("utf-8")).hexdigest()
    tpn_hash = "sha256:" + hashlib.sha256(tpn_content.encode("utf-8")).hexdigest()

    published_data = build_published_json(
        source_vault_sha=source_vault_sha,
        share_manifest_hash=manifest_hash,
        license_md_hash=license_hash,
        third_party_notices_hash=tpn_hash,
        file_hashes=file_hashes,
    )
    write_published_json(publish_root, published_data)

    # -----------------------------------------------------------------------
    # Step 13: commit (local + vault) + deploy-key push (vault mode only, D-03)
    # -----------------------------------------------------------------------
    scope_summary = derive_scope_summary(plan.to_write, plan.to_delete)
    commit_message = (
        f"Empiria publish: {scope_summary}, source @ {source_vault_sha}"
    )

    # Build the explicit stage list: only the files the pipeline wrote/deleted
    # plus the three generated artefacts.  Staging the whole subtree directory
    # would sweep any pre-existing client stray files into the Empiria commit
    # (WR-02).
    # plan.to_write/to_delete are already client-facing (published) paths, which
    # is exactly where the files were written — stage them directly.
    subtree_prefix = target_subtree.rstrip("/") + "/"
    stage_paths: list[str] = (
        [subtree_prefix + p for p in plan.to_write]
        + [subtree_prefix + p for p in plan.to_delete]
        + [
            subtree_prefix + "LICENSE.md",
            subtree_prefix + "THIRD-PARTY-NOTICES.md",
            subtree_prefix + "PUBLISHED.json",
        ]
    )

    # --no-commit: leave the written slice in the working tree for the operator
    # to inspect, stage, and commit. Short-circuits before any deploy-key work.
    if not commit:
        return PublishResult(
            success=True,
            published=True,
            message=(
                f"Wrote {scope_summary} to {publish_root} (--no-commit). "
                f"Review, then commit and push yourself."
            ),
        )

    # Vault mode resolves + validates the deploy key BEFORE committing, so a
    # missing key fails fast without leaving a commit (D-01). Local mode never
    # pushes, so no key is needed.
    key_path: Path | None = None
    if not local_mode:
        deploy_key_ref = direct.get("deploy_key_ref", "")
        key_path = resolve_deploy_key(deploy_key_ref)

    committed = git_commit(
        target_root,
        stage_paths,
        commit_message,
    )
    if not committed:
        # Nothing actually changed on disk — treat as no-op
        return PublishResult(
            success=True,
            published=False,
            message="nothing to publish",
        )

    if local_mode:
        # Transport is the operator's responsibility (Phase 50): commit only.
        return PublishResult(
            success=True,
            published=True,
            message=(
                f"Published locally (committed; push handled externally): "
                f"{scope_summary}, source @ {source_vault_sha}"
            ),
        )

    # Vault mode: push origin main via the deploy key.
    # WR-01: wrap push failure so operators get an actionable message
    # (raw CalledProcessError bypasses the Typer except PublishError handler).
    # The local commit is intentionally preserved — it can be pushed manually.
    assert key_path is not None  # set above in vault mode
    try:
        git_push_with_deploy_key(target_root, key_path)
    except subprocess.CalledProcessError as exc:
        raise PublishError(
            f"run_publish: the commit was created in the target repo but the "
            f"push to origin failed (exit {exc.returncode}).\n"
            f"The commit is still present in the local target clone — push it "
            f"manually once the remote is reachable:\n"
            f"  cd <target-vault> && git push origin main\n"
            f"stderr: {exc.stderr}"
        ) from exc

    return PublishResult(
        success=True,
        published=True,
        message=f"Published: {scope_summary}, source @ {source_vault_sha}",
    )
