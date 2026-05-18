"""SCION broker template cache introspection (SBR-06).

The broker stores templates under ~/.scion/cache/templates/ in a
content-addressable layout:

    ~/.scion/cache/templates/
    ├── index.json                  # {"entries": {<template_id>: {"contentHash": ...}}}
    ├── <contentHash1>/
    │   ├── manifest.json
    │   └── <template files at flat or nested layout>
    └── <contentHash2>/...

This module reads (never writes) that layout so `mnemosyne doctor` can
diff vault-side template files against the broker's cached copy and
surface drift to the operator (D-18, D-19, D-20).

Path verified in scion fork pkg/runtimebroker/server.go:306-313;
on-disk shape from pkg/templatecache/cache.go:33-77 (RESEARCH §Q4).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def find_broker_cache_root() -> Path | None:
    """Return ~/.scion/cache/templates if it is a directory, else None.

    Returns None for machines that don't run a SCION broker — the doctor
    template-drift check skips silently in that case (D-19).
    """
    cache = Path.home() / ".scion" / "cache" / "templates"
    if cache.is_dir():
        return cache
    return None


def read_template_index(cache_root: Path) -> dict | None:
    """Parse cache_root/index.json. Returns None if missing or unparseable."""
    index_path = cache_root / "index.json"
    if not index_path.exists():
        return None
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root: Path, *, skip: set[str] | None = None) -> dict[str, str]:
    """Walk root recursively, returning {relative_path_str: sha256_hex}."""
    skip = skip or set()
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        out[rel] = _sha256_of_file(path)
    return out


def get_cached_file_hashes(cache_root: Path, template_id: str) -> dict[str, str]:
    """Return {relative_path: sha256_hex} for the cached template_id.

    Returns {} if:
    - index.json absent
    - template_id not in index
    - contentHash subdir missing on disk
    Skips manifest.json (cache metadata, not template content).
    """
    index = read_template_index(cache_root)
    if index is None:
        return {}
    entries = index.get("entries", {})
    entry = entries.get(template_id)
    if entry is None:
        return {}
    content_hash = entry.get("contentHash")
    if not content_hash:
        return {}
    template_dir = cache_root / content_hash
    if not template_dir.is_dir():
        return {}
    return _walk_files(template_dir, skip={"manifest.json"})


def diff_template_against_vault(
    cache_root: Path,
    template_id: str,
    vault_template_dir: Path,
) -> list[str]:
    """Return sorted list of relative file paths where vault differs from cache.

    Logic:
    - Cache-only files appear in the diff (they were removed from vault)
    - Vault-only files appear in the diff (they were added without sync)
    - Files in both with different sha256 appear in the diff
    Empty list means cache and vault are byte-for-byte in sync.
    """
    cached = get_cached_file_hashes(cache_root, template_id)
    vault = _walk_files(vault_template_dir)
    differing: set[str] = set()
    for path in cached.keys() | vault.keys():
        if cached.get(path) != vault.get(path):
            differing.add(path)
    return sorted(differing)
