"""Share-manifest loader and strict schema validator (D-19).

Turns an on-disk ``share-manifest.toml`` into a typed, validated
:class:`ShareManifest`.  Strict validation is the core safety mechanism:
a typo'd ``exclude`` path that silently no-ops would be an accidental leak.

Public API
----------
- :class:`ManifestError`         — raised on any validation failure
- :class:`ShareManifest`         — frozen dataclass representing a validated manifest
- :func:`validate_manifest_dict` — validate a raw ``tomllib``-parsed dict
- :func:`load_manifest`          — parse + validate from a file path
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Known schema sets — the source of D-19 strict validation
# ---------------------------------------------------------------------------

_KNOWN_TABLES: set[str] = {
    "client",
    "direct",
    "intermediary",
    "include",
    "exclude",
    "on_closure_breach",
    "license",
}

_KNOWN_KEYS: dict[str, set[str]] = {
    "client": {"slug", "display", "mode"},
    "direct": {"target_vault", "target_subtree", "deploy_key_ref"},
    "intermediary": {"package_repo", "package_branch"},
    "include": {"paths", "tags"},
    "exclude": {"paths"},
    "on_closure_breach": {"policy"},
    "license": {"template", "contract_ref", "copyright_holder", "spdx_license_ref"},
}

_VALID_MODES: set[str] = {"direct", "intermediary"}
_VALID_POLICIES: set[str] = {"refuse", "warn", "strip"}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """Raised on any share-manifest validation failure.

    The message always names the offending table/key so operators can find
    the exact mistake without guessing.
    """


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareManifest:
    """Validated, typed representation of a ``share-manifest.toml``.

    All list fields default to empty lists when the corresponding key is
    absent from the manifest (but the parent table is present).  Optional
    dict fields are ``None`` when the corresponding table is absent.
    """

    client_slug: str
    client_display: str | None
    mode: str

    # Mode-specific blocks — None when not present
    direct: dict | None
    intermediary: dict | None

    # Seed-set matching
    include_paths: list[str]
    include_tags: list[str]
    exclude_paths: list[str]

    # Policy
    policy: str

    # Optional license block
    license: dict | None


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------


def validate_manifest_dict(data: dict) -> ShareManifest:
    """Validate a raw dict (from ``tomllib.load``) against the manifest schema.

    Implements strict D-19 validation:
    - Any unknown top-level table name → :class:`ManifestError`.
    - Any unknown key within a known table → :class:`ManifestError` naming
      ``table.key``.
    - Required fields enforced: ``[client].slug``, ``[client].mode``,
      ``[include]`` (table must exist), ``[on_closure_breach].policy``.
    - When ``mode == "direct"``, the ``[direct]`` table is required.
    - Enum fields (``mode``, ``on_closure_breach.policy``) validated against
      their allowed sets.

    Returns a frozen :class:`ShareManifest` on success.

    Args:
        data: Dict produced by ``tomllib.load`` (or constructed in tests).

    Raises:
        :class:`ManifestError`: On any validation failure, with a message
            naming the offending table or key.
    """
    # ------------------------------------------------------------------
    # 1. Check for unknown top-level tables
    # ------------------------------------------------------------------
    for key in data:
        if key not in _KNOWN_TABLES:
            raise ManifestError(
                f"unknown top-level table '{key}' in share-manifest; "
                f"allowed tables: {sorted(_KNOWN_TABLES)}"
            )

    # ------------------------------------------------------------------
    # 2. Within each known table that is present, check for unknown keys
    # ------------------------------------------------------------------
    for table_name, allowed_keys in _KNOWN_KEYS.items():
        table = data.get(table_name)
        if table is None:
            continue
        if not isinstance(table, dict):
            raise ManifestError(
                f"[{table_name}] must be a TOML table, got {type(table).__name__}"
            )
        for key in table:
            if key not in allowed_keys:
                raise ManifestError(
                    f"unknown key '{table_name}.{key}' in share-manifest; "
                    f"allowed keys for [{table_name}]: {sorted(allowed_keys)}"
                )

    # ------------------------------------------------------------------
    # 3. Required: [client] table
    # ------------------------------------------------------------------
    client = data.get("client")
    if not isinstance(client, dict):
        raise ManifestError("share-manifest is missing the required [client] table")

    # 3a. Required: [client].slug
    client_slug = client.get("slug")
    if not client_slug:
        raise ManifestError(
            "share-manifest is missing required field [client].slug"
        )
    client_slug = str(client_slug)

    # 3b. Required: [client].mode
    mode = client.get("mode")
    if not mode:
        raise ManifestError(
            "share-manifest is missing required field [client].mode"
        )
    mode = str(mode)

    # 3c. Enum: mode
    if mode not in _VALID_MODES:
        raise ManifestError(
            f"invalid mode '{mode}' in share-manifest [client]; "
            f"allowed values: {sorted(_VALID_MODES)}"
        )

    # Optional display name
    client_display: str | None = client.get("display")
    if client_display is not None:
        client_display = str(client_display)

    # ------------------------------------------------------------------
    # 4. Required: [include] table
    # ------------------------------------------------------------------
    include = data.get("include")
    if include is None:
        raise ManifestError(
            "share-manifest is missing required [include] table"
        )
    if not isinstance(include, dict):
        raise ManifestError("[include] must be a TOML table")

    include_paths: list[str] = list(include.get("paths") or [])
    include_tags: list[str] = list(include.get("tags") or [])

    # ------------------------------------------------------------------
    # 5. Required: [on_closure_breach] and its policy field
    # ------------------------------------------------------------------
    ocb = data.get("on_closure_breach")
    if not isinstance(ocb, dict):
        raise ManifestError(
            "share-manifest is missing the required [on_closure_breach] table"
        )
    policy = ocb.get("policy")
    if not policy:
        raise ManifestError(
            "share-manifest is missing required field [on_closure_breach].policy"
        )
    policy = str(policy)

    # 5a. Enum: policy
    if policy not in _VALID_POLICIES:
        raise ManifestError(
            f"invalid policy '{policy}' in share-manifest [on_closure_breach]; "
            f"allowed values: {sorted(_VALID_POLICIES)}"
        )

    # ------------------------------------------------------------------
    # 6. mode == "direct" requires [direct] table
    # ------------------------------------------------------------------
    direct_raw = data.get("direct")
    if mode == "direct" and direct_raw is None:
        raise ManifestError(
            "share-manifest has mode='direct' but the required [direct] table is missing"
        )
    direct: dict | None = dict(direct_raw) if direct_raw is not None else None

    # ------------------------------------------------------------------
    # 7. Optional tables
    # ------------------------------------------------------------------
    intermediary_raw = data.get("intermediary")
    intermediary: dict | None = dict(intermediary_raw) if intermediary_raw is not None else None

    exclude_raw = data.get("exclude")
    exclude_paths: list[str] = []
    if isinstance(exclude_raw, dict):
        exclude_paths = list(exclude_raw.get("paths") or [])

    license_raw = data.get("license")
    license_block: dict | None = dict(license_raw) if license_raw is not None else None

    return ShareManifest(
        client_slug=client_slug,
        client_display=client_display,
        mode=mode,
        direct=direct,
        intermediary=intermediary,
        include_paths=include_paths,
        include_tags=include_tags,
        exclude_paths=exclude_paths,
        policy=policy,
        license=license_block,
    )


# ---------------------------------------------------------------------------
# File-level loader
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> ShareManifest:
    """Parse and validate a ``share-manifest.toml`` file.

    Mirrors the ``tomllib`` binary-read pattern used in ``lib/vault.py``
    (``_read_config``).

    Args:
        path: Filesystem path to the ``share-manifest.toml``.

    Returns:
        A validated :class:`ShareManifest`.

    Raises:
        :class:`ManifestError`: On TOML parse errors (wrapping the original)
            or schema validation failures.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(
            f"failed to parse share-manifest at {path}: {exc}"
        ) from exc
    return validate_manifest_dict(data)
