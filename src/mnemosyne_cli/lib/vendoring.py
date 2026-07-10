"""Vendoring engine for committed-copy + manifest pattern (D-06, Phase 54).

Manages `agents/vendored.toml` entries — each entry pins an upstream repo ref,
lists the subpaths owned by upstream, and is synced by `mnemosyne refresh`.

Public API
----------
- :func:`load_manifest`   — parse agents/vendored.toml into entry dicts
- :func:`refresh_entry`   — sync one entry's upstream_owned subpaths at its pinned ref
- :func:`refresh_all`     — iterate entries, optionally filtering by name
- :func:`set_pin`         — rewrite one entry's `ref` in vendored.toml, preserving comments
- :func:`diff_entry`      — sha256 drift diff for one entry's upstream_owned subpaths
- :func:`diff_all`        — drift diff across all entries

Design invariants (D-06 / T-54-03, revised)
-------------------------------------------
- The `ref` in vendored.toml is a real pin: `refresh_entry` syncs the content at
  that exact ref, never whatever the default branch happens to be.  When upstream
  has advanced past the pin, the entry still syncs at the pin and the result
  reports `advanced=True` so the caller can tell the operator.  Advancing the pin
  is an explicit act (`refresh --accept-upstream`), never a side effect of refresh.
- `refresh_entry` NEITHER stages NOR commits.  It leaves changes in the working
  tree.  This is the supply-chain review gate: upstream content cannot enter
  history without the operator staging it deliberately, so it can never be swept
  into an unrelated `git commit`.
- Empiria-authored files (`index.md`, `.upstream-ref`) are NEVER in
  `upstream_owned` and are never overwritten by a sync.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
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


class VendoringError(RuntimeError):
    """Raised when an entry cannot be synced at its pinned ref."""


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of syncing one vendored entry.

    Attributes:
        name:          Entry name from the manifest.
        synced_sha:    Commit SHA whose content now sits in the working tree.
        upstream_head: Commit SHA at the tip of upstream's default branch.
        advanced:      True when upstream's tip has moved past the synced commit,
                       i.e. there is new upstream content the pin is holding back.
    """

    name: str
    synced_sha: str
    upstream_head: str
    advanced: bool


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


# A hex pin (full or abbreviated) must prefix the commit it resolves to.  Tag
# pins are opaque and cannot be checked this way.
_HEX_PIN_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _rev_parse(repo: Path, rev: str = "HEAD") -> str:
    """Return the commit SHA that *rev* resolves to inside *repo*."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _checkout_pin(repo: Path, pin: str, upstream: str) -> str:
    """Check out *pin* inside the shallow clone *repo*, returning its commit SHA.

    Fetching a bare SHA needs `uploadpack.allowAnySHA1InWant` on the server (GitHub
    and GitLab both allow it).  When the server refuses, deepen the clone instead
    so the pin becomes reachable by name.
    """
    fetched = subprocess.run(
        ["git", "-C", str(repo), "fetch", "--depth", "1", "origin", pin],
        capture_output=True,
        text=True,
    )
    if fetched.returncode == 0:
        # FETCH_HEAD is only trustworthy when the fetch actually succeeded.
        targets = ("FETCH_HEAD", pin)
    else:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "--unshallow"],
            capture_output=True,
            text=True,
        )
        targets = (pin,)

    for target in targets:
        checked = subprocess.run(
            ["git", "-C", str(repo), "checkout", "--detach", target],
            capture_output=True,
            text=True,
        )
        if checked.returncode == 0:
            return _rev_parse(repo)

    raise VendoringError(
        f"cannot check out pinned ref {pin!r} from {upstream} — "
        f"the ref may have been force-pushed away or garbage-collected"
    )


def refresh_entry(
    entry: dict, vault_path: Path, *, accept_upstream: bool = False
) -> RefreshResult:
    """Sync one entry's ``upstream_owned`` subpaths at its pinned ref.

    Algorithm
    ---------
    1. Shallow-clone the upstream default branch into a temp directory and
       resolve its tip (``upstream_head``).
    2. Unless *accept_upstream*, check out ``entry["ref"]`` — the pin — so the
       content copied out is the pinned content, not the default-branch tip.
    3. Sync each subpath listed in ``entry["upstream_owned"]``:
       - directory: ``shutil.rmtree(dst)`` then ``shutil.copytree(src, dst)``
       - file: ``shutil.copy2(src, dst)`` (creating parent dirs as needed)
       - absent upstream: left as-is (not deleted)
    4. Write ``<dest>/.upstream-ref`` = the synced SHA (provenance record).  This
       file is NOT in ``upstream_owned`` — it is Empiria-owned.

    Changes are left in the working tree: nothing is staged and nothing is
    committed.  Staging is the operator's deliberate act.

    Args:
        entry:           Manifest entry dict (from :func:`load_manifest`).
        vault_path:      Vault root.
        accept_upstream: Sync the default-branch tip instead of the pin.  The
                         caller is responsible for writing the new pin back via
                         :func:`set_pin`.

    Returns:
        A :class:`RefreshResult` describing what was synced and whether upstream
        has advanced past it.

    Raises:
        VendoringError: the entry has no ``ref``, or the pin cannot be checked out.
    """
    pin = str(entry.get("ref") or "").strip()
    if not pin and not accept_upstream:
        raise VendoringError(
            f"vendored entry {entry['name']!r} has no 'ref' pin — refusing to sync "
            f"an unpinned upstream.  Add a ref to agents/vendored.toml."
        )

    dest = vault_path / entry["path"]
    dest.mkdir(parents=True, exist_ok=True)

    tmp_ctx = tempfile.TemporaryDirectory()
    with tmp_ctx:
        tmp = Path(tmp_ctx.name)

        subprocess.run(
            ["git", "clone", "--depth", "1", entry["upstream"], str(tmp)],
            check=True,
            capture_output=True,
            text=True,
        )
        upstream_head = _rev_parse(tmp)

        if accept_upstream or pin == upstream_head:
            synced_sha = upstream_head
        else:
            synced_sha = _checkout_pin(tmp, pin, entry["upstream"])
            # A hex pin must resolve to the commit it names; anything else means
            # we copied content the operator never reviewed.
            if _HEX_PIN_RE.match(pin) and not synced_sha.startswith(pin):
                raise VendoringError(
                    f"{entry['name']}: pinned ref {pin} resolved to {synced_sha} — "
                    f"refusing to sync content that does not match the pin"
                )

        for sub in entry.get("upstream_owned", []):
            src = tmp / sub
            dst = dest / sub
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                # Sidecar of sha256s so diff_local detects drift without a clone.
                shas = _walk_files(dst, skip=_EMPIRIA_FILES)
                shas_lines = "".join(
                    f"{sha}  {rel}\n" for rel, sha in sorted(shas.items())
                )
                (dst / _SHAS_FILENAME).write_text(shas_lines)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            # If absent in upstream, leave existing content in place

        (dest / ".upstream-ref").write_text(synced_sha + "\n")

    return RefreshResult(
        name=entry["name"],
        synced_sha=synced_sha,
        upstream_head=upstream_head,
        advanced=upstream_head != synced_sha,
    )


def refresh_all(
    vault_path: Path,
    names: list[str] | None = None,
    *,
    accept_upstream: bool = False,
) -> dict[str, RefreshResult]:
    """Refresh all (or named) manifest entries.

    Args:
        vault_path:      Vault root.
        names:           Optional list of entry names to refresh.  When ``None``
                         (or empty), all entries are refreshed.
        accept_upstream: Passed through to :func:`refresh_entry`.

    Returns:
        Dict mapping ``{entry_name: RefreshResult}`` for each entry processed.
    """
    entries = load_manifest(vault_path)
    results: dict[str, RefreshResult] = {}
    for entry in entries:
        if names and entry["name"] not in names:
            continue
        results[entry["name"]] = refresh_entry(
            entry, vault_path, accept_upstream=accept_upstream
        )
    return results


def set_pin(vault_path: Path, name: str, new_ref: str) -> None:
    """Rewrite the ``ref`` of the ``[[vendored]]`` entry *name* in vendored.toml.

    Edits the quoted value in place rather than round-tripping through a TOML
    writer, because the manifest carries operator commentary that a serialiser
    would discard.

    Raises:
        VendoringError: no ``[[vendored]]`` block for *name*, or it has no ``ref``.
    """
    manifest_path = vault_path / "agents" / "vendored.toml"
    lines = manifest_path.read_text().splitlines(keepends=True)

    in_target = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[[vendored]]":
            in_target = False
            continue
        if re.match(r'^name\s*=\s*"' + re.escape(name) + r'"', stripped):
            in_target = True
            continue
        if in_target and re.match(r"^ref\s*=", stripped):
            lines[idx] = re.sub(r'(ref\s*=\s*")[^"]*(")', rf"\g<1>{new_ref}\g<2>", line)
            manifest_path.write_text("".join(lines))
            return

    raise VendoringError(f"no vendored entry named {name!r} with a 'ref' in {manifest_path}")


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
