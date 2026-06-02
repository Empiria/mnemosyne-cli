"""Wikilink-closure walker — the leak-detector engine for Phase 48.

Given a validated :class:`~mnemosyne_cli.share.manifest.ShareManifest` and a
vault root, resolves the seed set (D-15 exclude-wins precedence), walks the
wikilink graph using the Phase 48 parser, resolves each link to a note file
(D-01 ambiguity rules), and classifies every reachable note into three labels
plus a separate broken-links category.

Public API
----------
- :class:`AmbiguousLinkError`   — raised on >=2 bare-basename matches (D-01)
- :class:`WalkResult`           — frozen dataclass carrying classification + metadata
- :func:`resolve_seed`          — compute the seed set from include/exclude (D-15)
- :func:`walk_manifest`         — walk from seed, classify, return WalkResult

Decision references (48-CONTEXT.md)
-------------------------------------
- D-01: bare basename → vault-wide filename match; >=2 matches raises
        AmbiguousLinkError.  Path-qualified link resolves directly.
- D-02: broken/dangling links → result.broken only, never breach.
- D-03: Obsidian wikilinks only (handled by the parser).
- D-04: all wikilink forms are edges (body, frontmatter-list, alias, embed).
        Visited-set terminates cycles.
- D-05: heading/block anchors stripped by the parser.
- D-10: three classification labels: in_set / excluded / breach.
- D-11: policy acts on BOTH excluded and breach; has_breaches covers both.
- D-12: strip is report-only (no file writes); strip_candidates records edges.
- D-14: pathspec gitignore-semantics for glob matching.
- D-15: seed = (include.paths OR include.tags) MINUS exclude.paths.
"""

from __future__ import annotations

import frontmatter
import pathspec
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from mnemosyne_cli.share.manifest import ShareManifest
from mnemosyne_cli.share.wikilinks import extract_frontmatter_wikilinks, extract_wikilinks


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AmbiguousLinkError(Exception):
    """Raised when a bare basename resolves to >=2 notes in the vault (D-01).

    The message names the target and lists all candidate vault-relative paths
    so the operator can qualify the link.
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkResult:
    """Structured output of a wikilink-closure walk.

    All path strings are vault-root-relative POSIX paths (e.g.
    ``"technologies/demo/reference/a.md"``).

    Attributes
    ----------
    client_slug:
        The ``[client].slug`` value from the manifest.
    policy:
        The ``[on_closure_breach].policy`` value from the manifest.
    manifest_path:
        Absolute filesystem path to the manifest that was walked, or ``None``
        if the manifest was constructed in-memory (e.g. in tests).
    in_set:
        Notes that are included (match include path or tag) and not excluded.
    excluded:
        Notes that match ``exclude.paths`` — reachable but explicitly excluded.
    breach:
        Notes that match neither include nor exclude — true closure breaches.
    broken:
        Wikilink targets that resolved to no file in the vault (D-02).
        Never in breach.
    strip_candidates:
        List of ``(source_vault_rel_path, target_vault_rel_path)`` pairs: edges
        from an ``in_set`` note to a non-in_set (excluded or breach) target.
        The ``strip`` policy would flatten these links to alias text (D-12).
    """

    client_slug: str
    policy: str
    manifest_path: Path | None

    in_set: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    breach: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    strip_candidates: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_breaches(self) -> bool:
        """True iff any excluded or breach notes exist (D-11).

        Policy acts on BOTH non-in_set classes — a single in-set→excluded edge
        is a violation that must be consciously resolved.
        """
        return bool(self.excluded or self.breach)


# ---------------------------------------------------------------------------
# Vault basename index (D-01)
# ---------------------------------------------------------------------------


def _vault_md_files(vault_root: Path) -> Iterator[Path]:
    """Yield all ``.md`` files under *vault_root*, skipping hidden directories.

    Hidden directories (any path component starting with ``.``) are skipped so
    that git worktrees (``.claude/worktrees/``), SCION agent workspaces
    (``.scion/agents/``), and other dot-prefixed tooling directories are never
    included in the vault index or seed scan.  Duplicate copies of vault notes
    in worktrees would otherwise cause spurious :class:`AmbiguousLinkError`
    exceptions when a bare ``[[name]]`` resolves to multiple candidates.

    Args:
        vault_root: Absolute path to the vault root directory.

    Yields:
        Absolute :class:`~pathlib.Path` objects for each non-hidden ``.md``
        file beneath *vault_root*.
    """
    for md_file in vault_root.rglob("*.md"):
        rel_parts = md_file.relative_to(vault_root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        yield md_file


def _index_vault(vault_root: Path) -> dict[str, list[str]]:
    """Build a basename → [vault-rel paths] index for all .md files.

    Used to resolve bare ``[[name]]`` links (D-01): one match → resolved;
    zero matches → broken; two or more → :class:`AmbiguousLinkError`.

    Hidden directories are excluded via :func:`_vault_md_files` so that
    git worktrees and SCION agent workspace copies of vault notes do not
    cause spurious ambiguity errors.

    Args:
        vault_root: Absolute path to the vault root directory.

    Returns:
        Dict mapping note basename (without ``.md``) to a list of vault-
        relative POSIX path strings.  Keys are case-sensitive.
    """
    index: dict[str, list[str]] = {}
    for md_file in _vault_md_files(vault_root):
        rel = md_file.relative_to(vault_root).as_posix()
        stem = md_file.stem  # basename without .md
        index.setdefault(stem, []).append(rel)
    return index


# ---------------------------------------------------------------------------
# Link resolver (D-01, D-02)
# ---------------------------------------------------------------------------


def _resolve(
    target: str,
    vault_root: Path,
    index: dict[str, list[str]],
) -> str | None:
    """Resolve a wikilink target to a vault-relative path string.

    Implements D-01 resolution rules:

    - **Path-qualified** (contains ``/``): treat as vault-relative path; append
      ``.md`` if not already present; return if the file exists in the vault,
      else ``None`` (broken link, D-02).
    - **Bare basename** (no ``/``): look up in the vault index.  Zero matches →
      ``None`` (broken).  One match → return it.  Two or more matches → raise
      :class:`AmbiguousLinkError` naming the target and candidates.

    Args:
        target:     Normalised wikilink target (anchors/aliases already stripped).
        vault_root: Absolute path to the vault root.
        index:      Basename index from :func:`_index_vault`.

    Returns:
        Vault-relative POSIX path string if resolved, or ``None`` if broken.

    Raises:
        :class:`AmbiguousLinkError`: For bare basenames matching >=2 notes.
    """
    if "/" in target:
        # Path-qualified resolution
        rel = target if target.endswith(".md") else f"{target}.md"
        # Security: resolved path must remain inside the vault root (T-48-04-01)
        candidate = vault_root / rel
        try:
            candidate.resolve().relative_to(vault_root.resolve())
        except ValueError:
            return None  # path traversal attempt → treat as broken
        return rel if candidate.exists() else None
    else:
        # Bare basename resolution
        candidates = index.get(target, [])
        if len(candidates) == 0:
            return None  # broken
        if len(candidates) == 1:
            return candidates[0]
        raise AmbiguousLinkError(
            f"bare link '[[{target}]]' matches {len(candidates)} notes: "
            + ", ".join(sorted(candidates))
        )


# ---------------------------------------------------------------------------
# Seed set computation (D-13, D-14, D-15)
# ---------------------------------------------------------------------------


def resolve_seed(manifest: ShareManifest, vault_root: Path) -> set[str]:
    """Compute the seed set: (include.paths | include.tags) MINUS exclude.paths.

    Steps (D-15 exclude-wins):

    1. Collect all vault-relative .md paths.
    2. Match against ``include.paths`` (pathspec gitignore-semantics, D-14).
    3. Add notes whose frontmatter ``tags:`` list intersects ``include.tags``
       (exact string match, D-13).
    4. Remove notes matching ``exclude.paths``.

    Args:
        manifest:   Validated :class:`~mnemosyne_cli.share.manifest.ShareManifest`.
        vault_root: Absolute vault root path.

    Returns:
        Set of vault-relative POSIX path strings forming the seed.
    """
    # Build path spec matchers
    include_spec = (
        pathspec.PathSpec.from_lines("gitignore", manifest.include_paths)
        if manifest.include_paths
        else None
    )
    exclude_spec = (
        pathspec.PathSpec.from_lines("gitignore", manifest.exclude_paths)
        if manifest.exclude_paths
        else None
    )

    include_tag_set: set[str] = set(manifest.include_tags)

    # Collect all vault .md files (hidden directories excluded via _vault_md_files)
    all_md: list[str] = [
        p.relative_to(vault_root).as_posix()
        for p in _vault_md_files(vault_root)
    ]

    seed: set[str] = set()

    for rel in all_md:
        # Does it match include.paths?
        by_path = include_spec is not None and include_spec.match_file(rel)

        # Does it carry an include tag?
        by_tag = False
        if include_tag_set:
            try:
                post = frontmatter.load(str(vault_root / rel))
                note_tags = post.metadata.get("tags") or []
                if isinstance(note_tags, list):
                    by_tag = bool(include_tag_set.intersection(note_tags))
                elif isinstance(note_tags, str):
                    by_tag = note_tags in include_tag_set
            except Exception:
                pass  # malformed frontmatter → treat as no tags

        if by_path or by_tag:
            seed.add(rel)

    # Exclude-wins (D-15): remove any seed note matching exclude.paths
    if exclude_spec is not None:
        seed = {rel for rel in seed if not exclude_spec.match_file(rel)}

    return seed


# ---------------------------------------------------------------------------
# Main walker (D-04, D-10, D-11, D-12)
# ---------------------------------------------------------------------------


def walk_manifest(manifest: ShareManifest, vault_root: Path) -> WalkResult:
    """Walk the wikilink closure from the manifest seed and classify all notes.

    Algorithm:

    1. Resolve seed set via :func:`resolve_seed`.
    2. Build the vault basename index via :func:`_index_vault`.
    3. BFS from the seed; for each note:
       a. Extract body wikilinks via :func:`~mnemosyne_cli.share.wikilinks.extract_wikilinks`.
       b. Extract frontmatter wikilinks via
          :func:`~mnemosyne_cli.share.wikilinks.extract_frontmatter_wikilinks`.
       c. For each target, resolve via :func:`_resolve`.
       d. Broken targets → ``result.broken``, skip further traversal (D-02).
       e. Resolved targets not yet visited → enqueue.
    4. Classify each visited note:
       - Matches exclude.paths → ``excluded``.
       - Matches include (path or tag) and not excluded → ``in_set``.
       - Matches neither → ``breach``.
    5. Record strip_candidates: edges from an in_set source to a non-in_set
       target (D-12, report-only — no writes).

    Args:
        manifest:   Validated :class:`~mnemosyne_cli.share.manifest.ShareManifest`.
        vault_root: Absolute path to the vault root.

    Returns:
        A frozen :class:`WalkResult` with all four classified lists and metadata.

    Raises:
        :class:`AmbiguousLinkError`: Propagated from :func:`_resolve` when a
            bare link matches >=2 notes (D-01).
    """
    vault_root = vault_root.resolve()

    # Build matching specs once (share with classification)
    include_spec = (
        pathspec.PathSpec.from_lines("gitignore", manifest.include_paths)
        if manifest.include_paths
        else None
    )
    exclude_spec = (
        pathspec.PathSpec.from_lines("gitignore", manifest.exclude_paths)
        if manifest.exclude_paths
        else None
    )
    include_tag_set: set[str] = set(manifest.include_tags)

    # Build basename index once for bare-link resolution (D-01)
    index = _index_vault(vault_root)

    # Compute seed
    seed = resolve_seed(manifest, vault_root)

    # BFS state
    visited: set[str] = set()
    broken: list[str] = []
    strip_candidates: list[tuple[str, str]] = []

    queue: deque[str] = deque(seed)
    visited.update(seed)

    while queue:
        current_rel = queue.popleft()
        current_path = vault_root / current_rel

        if not current_path.exists():
            # Seed note itself is broken (shouldn't normally happen)
            broken.append(current_rel)
            continue

        # Extract all wikilink targets from this note
        try:
            post = frontmatter.load(str(current_path))
            body_targets = extract_wikilinks(post.content)
            fm_targets = extract_frontmatter_wikilinks(post.metadata)
        except Exception:
            body_targets = []
            fm_targets = []

        all_targets = list(dict.fromkeys(body_targets + fm_targets))  # dedup, order

        for target in all_targets:
            resolved = _resolve(target, vault_root, index)

            if resolved is None:
                # Broken link (D-02) — record as broken vault-rel path attempt
                # Use the path-qualified form if "/" in target for clarity
                broken_key = (
                    f"{target}.md" if "/" in target and not target.endswith(".md")
                    else target
                )
                if broken_key not in broken:
                    broken.append(broken_key)
                continue

            # Record strip_candidate before visited check: source is current_rel
            # We'll classify later, but record the edge if source ends up in_set
            # and resolved ends up non-in_set. We defer classification to after
            # the full walk; store all edges for post-processing.
            # (We track them in a separate structure below.)

            if resolved not in visited:
                visited.add(resolved)
                queue.append(resolved)

    # -----------------------------------------------------------------------
    # Classify every visited note into in_set / excluded / breach
    # -----------------------------------------------------------------------

    def _is_excluded(rel: str) -> bool:
        return exclude_spec is not None and exclude_spec.match_file(rel)

    def _is_included(rel: str) -> bool:
        """True iff the note matches include.paths OR carries an include tag."""
        by_path = include_spec is not None and include_spec.match_file(rel)
        if by_path:
            return True
        if include_tag_set:
            try:
                post = frontmatter.load(str(vault_root / rel))
                note_tags = post.metadata.get("tags") or []
                if isinstance(note_tags, list):
                    return bool(include_tag_set.intersection(note_tags))
                elif isinstance(note_tags, str):
                    return note_tags in include_tag_set
            except Exception:
                pass
        return False

    in_set: list[str] = []
    excluded: list[str] = []
    breach: list[str] = []

    for rel in visited:
        if _is_excluded(rel):
            excluded.append(rel)
        elif _is_included(rel):
            in_set.append(rel)
        else:
            breach.append(rel)

    # -----------------------------------------------------------------------
    # Compute strip_candidates: edges from in_set → non-in_set (D-12)
    # -----------------------------------------------------------------------

    in_set_set = set(in_set)
    excluded_set = set(excluded)
    breach_set = set(breach)

    for source_rel in in_set_set:
        source_path = vault_root / source_rel
        if not source_path.exists():
            continue
        try:
            post = frontmatter.load(str(source_path))
            body_targets = extract_wikilinks(post.content)
            fm_targets = extract_frontmatter_wikilinks(post.metadata)
        except Exception:
            body_targets = []
            fm_targets = []

        for target in dict.fromkeys(body_targets + fm_targets):
            resolved = _resolve(target, vault_root, index)
            if resolved is not None and (
                resolved in excluded_set or resolved in breach_set
            ):
                strip_candidates.append((source_rel, resolved))

    return WalkResult(
        client_slug=manifest.client_slug,
        policy=manifest.policy,
        manifest_path=None,  # caller may set; walker doesn't hold the path
        in_set=in_set,
        excluded=excluded,
        breach=breach,
        broken=broken,
        strip_candidates=strip_candidates,
    )
