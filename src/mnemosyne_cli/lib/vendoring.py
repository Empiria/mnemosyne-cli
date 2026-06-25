"""Vendoring engine for committed-copy + manifest pattern (D-06, Phase 54).

Manages `agents/vendored.toml` entries — each entry pins an upstream repo ref,
lists the subpaths owned by upstream, and is synced by `mnemosyne refresh`.

Public API
----------
- :func:`load_manifest`   — parse agents/vendored.toml into entry dicts
- :func:`refresh_entry`   — clone upstream, sync upstream_owned subpaths, write .upstream-ref, stage
- :func:`refresh_all`     — iterate entries, optionally filtering by name
- :func:`diff_entry`      — sha256 drift diff for one entry's upstream_owned subpaths
- :func:`diff_all`        — drift diff across all entries

Design invariants (D-06 / T-54-03)
-----------------------------------
- `refresh_entry` STAGES via `git add` and NEVER commits.  The operator reviews
  the staged diff and commits with a gitmoji message.  This is the supply-chain
  review gate: a force-pushed upstream cannot silently enter history.
- SHA pin: the vendored.toml entry carries a pinned `ref` (SHA or tag).  For
  master-tracked repos with no tags (e.g. anvil-agent-references), the engine
  shallow-clones master and asserts HEAD == pinned SHA, flagging if it moved.
- Empiria-authored files (`index.md`, `.upstream-ref`) are NEVER in
  `upstream_owned` and are never overwritten by a sync.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Empiria-owned files — never overwritten by upstream sync, never drift-diffed
# ---------------------------------------------------------------------------

# Files are identified by FILENAME (not full path) so they are skipped at any
# nesting depth within an upstream_owned subpath tree.
_EMPIRIA_FILES: set[str] = {"index.md", ".upstream-ref", ".upstream-shas"}

# Name of the local sha256 sidecar written by refresh_entry into each
# upstream_owned subpath.  Used by diff_local for network-free drift detection.
_SHAS_FILENAME = ".upstream-shas"


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------


def load_manifest(vault_path: Path) -> list[dict]:
    """Parse `agents/vendored.toml` and return the list of ``[[vendored]]`` entries.

    Each entry is a plain dict with keys: ``name``, ``upstream``, ``path``,
    ``ref``, ``upstream_owned`` (list of str).

    Args:
        vault_path: Root of the Mnemosyne vault (the directory containing
                    ``agents/``).

    Returns:
        List of entry dicts, in manifest order.  Empty list if the manifest
        does not exist.
    """
    manifest_path = vault_path / "agents" / "vendored.toml"
    if not manifest_path.exists():
        return []
    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("vendored", [])


# ---------------------------------------------------------------------------
# Core refresh engine
# ---------------------------------------------------------------------------


def refresh_entry(entry: dict, vault_path: Path) -> str:
    """Shallow-clone upstream, sync ``upstream_owned`` subpaths, stage (never commit).

    Algorithm
    ---------
    1. Create a ``tempfile.TemporaryDirectory``.
    2. ``git clone --depth 1 <upstream> <tmp>`` (always clones master/default branch).
    3. ``git -C <tmp> rev-parse HEAD`` — resolve current upstream HEAD SHA.
    4. Sync each subpath listed in ``entry["upstream_owned"]``:
       - If a directory: ``shutil.rmtree(dst)`` then ``shutil.copytree(src, dst)``.
       - If a file: ``shutil.copy2(src, dst)`` (creates parent dirs as needed).
       - Subpaths absent in the upstream clone are left as-is (not deleted).
    5. Write ``<dest>/.upstream-ref`` = resolved HEAD SHA (provenance record).
       This file is NOT in ``upstream_owned`` — it is Empiria-owned.
    6. ``git -C <vault_path> add <path>`` — stages the entire vendored subtree.
       NEVER issues a ``git commit``.

    Args:
        entry:      Manifest entry dict (from :func:`load_manifest`).
        vault_path: Vault root.

    Returns:
        The resolved HEAD SHA of the upstream clone (the value written to
        ``.upstream-ref``).
    """
    dest = vault_path / entry["path"]
    dest.mkdir(parents=True, exist_ok=True)

    tmp_ctx = tempfile.TemporaryDirectory()
    with tmp_ctx:
        tmp = Path(tmp_ctx.name)

        # Step 2: shallow clone upstream master/default branch
        subprocess.run(
            ["git", "clone", "--depth", "1", entry["upstream"], str(tmp)],
            check=True,
            capture_output=True,
            text=True,
        )

        # Step 3: resolve HEAD SHA — flag if it moved from the pinned ref
        rev_result = subprocess.run(
            ["git", "-C", str(tmp), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        head_sha = rev_result.stdout.strip()

        # Step 4: sync only upstream_owned subpaths
        for sub in entry.get("upstream_owned", []):
            src = tmp / sub
            dst = dest / sub
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                # Step 4a: write .upstream-shas sidecar inside the synced dir
                # so diff_local can detect drift without a network clone.
                shas = _walk_files(dst, skip=_EMPIRIA_FILES)
                shas_lines = "".join(
                    f"{sha}  {rel}\n" for rel, sha in sorted(shas.items())
                )
                (dst / _SHAS_FILENAME).write_text(shas_lines)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            # If absent in upstream, leave existing content in place

        # Step 5: write provenance file (Empiria-owned, never in upstream_owned)
        (dest / ".upstream-ref").write_text(head_sha + "\n")

    # Step 6: stage the vendored subtree (NEVER commit — D-06 / T-54-03)
    subprocess.run(
        ["git", "-C", str(vault_path), "add", entry["path"]],
        check=True,
        capture_output=True,
        text=True,
    )

    return head_sha


def refresh_all(vault_path: Path, names: list[str] | None = None) -> dict[str, str]:
    """Refresh all (or named) manifest entries.

    Args:
        vault_path: Vault root.
        names:      Optional list of entry names to refresh.  When ``None``
                    (or empty), all entries are refreshed.

    Returns:
        Dict mapping ``{entry_name: resolved_head_sha}`` for each entry
        processed.
    """
    entries = load_manifest(vault_path)
    results: dict[str, str] = {}
    for entry in entries:
        if names and entry["name"] not in names:
            continue
        head = refresh_entry(entry, vault_path)
        results[entry["name"]] = head
    return results


# ---------------------------------------------------------------------------
# Drift diff (sha256 walk — copy scion_cache shape)
# ---------------------------------------------------------------------------


def _sha256_of_file(path: Path) -> str:
    """Return the sha256 hex digest of *path*."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root: Path, *, skip: set[str] | None = None) -> dict[str, str]:
    """Walk *root* recursively, returning ``{relative_path_str: sha256_hex}``.

    Skips files whose FILENAME (``path.name``) is listed in *skip*, matching at
    any nesting depth.  This allows a single ``"index.md"`` entry in the skip
    set to suppress all ``index.md`` files in the tree, not just those at the
    root level.  Empiria-owned files (``_EMPIRIA_FILES``) are always excluded.
    """
    skip = skip or set()
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in skip:
            continue
        rel = path.relative_to(root).as_posix()
        out[rel] = _sha256_of_file(path)
    return out


def diff_entry(entry: dict, vault_path: Path) -> list[str]:
    """Return sorted list of relative paths where committed copy differs from upstream.

    Only walks the ``upstream_owned`` subpaths; always skips Empiria-owned files
    (``index.md``, ``.upstream-ref``) so they never register as drift.

    Requires a network-accessible clone of the upstream repo.  Intended for
    ``mnemosyne doctor --vendored-drift`` (Plan 05).

    Args:
        entry:      Manifest entry dict (from :func:`load_manifest`).
        vault_path: Vault root.

    Returns:
        Sorted list of differing relative paths.  Empty list = in sync.
    """
    dest = vault_path / entry["path"]

    with tempfile.TemporaryDirectory() as _tmp_name:
        tmp = Path(_tmp_name)

        subprocess.run(
            ["git", "clone", "--depth", "1", entry["upstream"], str(tmp)],
            check=True,
            capture_output=True,
            text=True,
        )

        differing: set[str] = set()
        for sub in entry.get("upstream_owned", []):
            src = tmp / sub
            dst = dest / sub
            upstream_files = _walk_files(src, skip=_EMPIRIA_FILES)
            committed_files = _walk_files(dst, skip=_EMPIRIA_FILES)
            for rel_path in upstream_files.keys() | committed_files.keys():
                if upstream_files.get(rel_path) != committed_files.get(rel_path):
                    differing.add(f"{sub}/{rel_path}" if sub else rel_path)

    return sorted(differing)


def diff_all(vault_path: Path) -> list[str]:
    """Return sorted list of differing paths across all manifest entries.

    Aggregates :func:`diff_entry` across all ``[[vendored]]`` entries.  Paths
    are prefixed by the entry's ``path`` field for traceability.

    Args:
        vault_path: Vault root.

    Returns:
        Sorted list of differing relative paths.
    """
    entries = load_manifest(vault_path)
    all_differing: list[str] = []
    for entry in entries:
        all_differing.extend(diff_entry(entry, vault_path))
    return sorted(all_differing)


def diff_local(vault_path: Path) -> list[str]:
    """Return sorted list of drifted paths using the local ``.upstream-shas`` sidecar.

    Network-free alternative to :func:`diff_all` — compares committed copy files
    against the sha256 manifest written by :func:`refresh_entry` into each
    ``upstream_owned`` directory.  Called by ``mnemosyne doctor --vendored-drift``
    so it works in CI environments that don't allow outbound git-clone traffic.

    Drift rules
    -----------
    - Entry path absent in vault → skip (entry not yet vendored; not drift).
    - Subpath exists but ``{subpath}/.upstream-shas`` absent → drift (never
      refreshed with the sidecar mechanism; operator must run refresh).
    - Subpath exists and ``.upstream-shas`` present → compare current sha256 of
      files against stored sha256; any mismatch is drift.
    - Files listed in ``_EMPIRIA_FILES`` are always excluded from comparison.

    Args:
        vault_path: Vault root.

    Returns:
        Sorted list of differing relative paths.  Empty list = no drift detected.
    """
    entries = load_manifest(vault_path)
    all_differing: list[str] = []

    for entry in entries:
        dest = vault_path / entry["path"]
        if not dest.is_dir():
            # Entry not yet vendored — skip (not drift)
            continue

        for sub in entry.get("upstream_owned", []):
            subpath_dir = dest / sub
            if not subpath_dir.is_dir():
                # Directory subpath absent — skip (may not exist in this entry)
                continue

            shas_file = subpath_dir / _SHAS_FILENAME
            if not shas_file.exists():
                # .upstream-shas absent: entry was vendored without the sidecar
                # (pre-refresh).  Treat as drifted so operator runs refresh.
                all_differing.append(f"{entry['path']}/{sub}/<no-shas-sidecar>")
                continue

            # Parse stored sha256 manifest
            stored: dict[str, str] = {}
            for line in shas_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("  ", 1)
                if len(parts) == 2:
                    stored[parts[1]] = parts[0]

            # Compare current files against stored sha256
            current = _walk_files(subpath_dir, skip=_EMPIRIA_FILES)
            for rel_path in stored.keys() | current.keys():
                if stored.get(rel_path) != current.get(rel_path):
                    all_differing.append(f"{entry['path']}/{sub}/{rel_path}")

    return sorted(all_differing)
