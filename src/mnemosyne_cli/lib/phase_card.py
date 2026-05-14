"""Phase card derivation library.

Public API for Phase 37 backfill (P3) and Phase 38 lifecycle hooks.

Phase 38 contract — these names are stable:

- ``PhaseCard`` (dataclass)
- ``derive_phase_card(phase_dir, vault_path, console=None) -> PhaseCard``
- ``write_phase_md(phase_dir, card, *, dry_run=False) -> action``
- ``card_to_dict(card) -> OrderedDict``  — Phase 38 must use this exact
  field order to keep re-writes idempotent.
- ``discover_phase_dirs(vault_path, project_scope) -> list[Path]``
- ``validate_project_slug(slug, vault_path) -> Path``
- ``parse_phase_number``, ``derive_status``, ``git_first_add_in_dir``,
  ``git_last_summary_commit``, ``is_phase_marked_complete``,
  ``read_state_md``
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import frontmatter
from rich.console import Console


_CANONICAL_RE = re.compile(
    r"^- \[(?P<box>[ xX])\] \*\*(?:Phase )?(?P<num>\S+?)(?::|\*\*)",
    re.MULTILINE,
)

_IW_SECTION_RE = re.compile(
    r"^### Phase (?P<num>\S+?)(?::| ).*?(?:^\*\*Plans:\*\* (?P<n>\d+)/(?P<total>\d+))",
    re.MULTILINE | re.DOTALL,
)

_PREFIX_RE = re.compile(r"^(empiria-\d+|\d+(?:[.-]\d+)?)")


@dataclass
class PhaseCard:
    """13-field schema per Phase 37 D-16."""

    project: str
    milestone: str | None
    phase_number: str
    status: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    blocked_on: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    summary: str = ""
    plan: str | None = None
    summary_doc: str | None = None
    validation: str | None = None


def parse_phase_number(dir_name: str) -> str:
    """Extract canonical phase_number from a phase directory slug (D-15)."""
    m = _PREFIX_RE.match(dir_name)
    if not m:
        return dir_name
    raw = m.group(1)
    if raw.startswith("empiria-"):
        return raw
    return raw.replace("-", ".")


def read_state_md(state_path: Path) -> dict:
    """Parse STATE.md frontmatter to a dict; missing or unreadable → {}."""
    if not state_path.is_file():
        return {}
    try:
        post = frontmatter.load(str(state_path))
        return dict(post.metadata)
    except Exception:
        return {}


def _read_roadmap_text(roadmap_path: Path) -> str:
    if not roadmap_path.is_file():
        return ""
    try:
        return roadmap_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def is_phase_marked_complete(roadmap_text: str, phase_number: str) -> bool:
    """True iff ROADMAP marks this phase complete.

    Canonical checkbox format wins; IW `**Plans:** N/N plans complete` is the
    fallback for projects that pre-date the canonical roadmap shape.
    """
    if not roadmap_text:
        return False
    for m in _CANONICAL_RE.finditer(roadmap_text):
        if m.group("num") == phase_number:
            return m.group("box").lower() == "x"
    for m in _IW_SECTION_RE.finditer(roadmap_text):
        if m.group("num") == phase_number:
            try:
                n = int(m.group("n"))
                total = int(m.group("total"))
            except (TypeError, ValueError):
                return False
            return n > 0 and n == total
    return False


def derive_status(
    phase_dir: Path,
    state: dict,
    roadmap_text: str,
    phase_number: str,
) -> str:
    """D-02 status cascade.

    Rules (in order): 5 (ROADMAP), 1 (every PLAN has SUMMARY), 2 (STATE
    current_phase), 3 (PLAN-no-SUMMARY), 4 (planned).
    """
    if is_phase_marked_complete(roadmap_text, phase_number):
        return "complete"

    plans: list[Path] = []
    summaries: list[Path] = []
    if phase_dir.is_dir():
        for p in phase_dir.iterdir():
            if not p.is_file():
                continue
            if p.name.endswith("-PLAN.md"):
                plans.append(p)
            elif p.name.endswith("-SUMMARY.md"):
                summaries.append(p)

    if plans:
        plan_ids = {p.name[: -len("-PLAN.md")] for p in plans}
        summary_ids = {s.name[: -len("-SUMMARY.md")] for s in summaries}
        if plan_ids and plan_ids.issubset(summary_ids):
            return "complete"

    current_phase = state.get("current_phase")
    if current_phase is not None and str(current_phase) == phase_number:
        return "in-progress"

    if plans:
        return "ready"

    return "planned"


def _git_log_first(args: list[str], cwd: Path) -> str | None:
    """Run `git log <args>` in `cwd`; return YYYY-MM-DD from first output line or None."""
    try:
        result = subprocess.run(
            ["git", "log", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    first = out.splitlines()[0].strip()
    if len(first) < 10:
        return None
    return first[:10]


def git_first_add_in_dir(phase_dir: Path, vault_path: Path) -> str | None:
    """First commit (oldest) that added any file under `phase_dir.name`.

    Uses `--all --diff-filter=A --reverse` with a `:(glob)**/<dir>/*` pathspec to
    survive the Phase 32 hard-cut migration (RESEARCH.md §Pitfall 1). Renames
    are excluded because `--diff-filter=A` selects additions only.
    """
    dir_name = phase_dir.name
    return _git_log_first(
        [
            "--all",
            "--diff-filter=A",
            "--reverse",
            "--format=%aI",
            "--",
            f":(glob)**/{dir_name}/*",
        ],
        cwd=vault_path,
    )


def git_last_summary_commit(phase_dir: Path, vault_path: Path) -> str | None:
    """Most recent commit (newest) adding or modifying a SUMMARY in `phase_dir.name`.

    Falls back to the last add/modify of any file in the dir if no SUMMARY exists.
    """
    dir_name = phase_dir.name
    date = _git_log_first(
        [
            "--all",
            "--diff-filter=AM",
            "-1",
            "--format=%aI",
            "--",
            f":(glob)**/{dir_name}/*-SUMMARY.md",
        ],
        cwd=vault_path,
    )
    if date:
        return date
    return _git_log_first(
        [
            "--all",
            "--diff-filter=AM",
            "-1",
            "--format=%aI",
            "--",
            f":(glob)**/{dir_name}/*",
        ],
        cwd=vault_path,
    )


def _vault_relative_parts(phase_dir: Path, vault_path: Path) -> tuple[str, ...] | None:
    try:
        rel = phase_dir.resolve().relative_to(vault_path.resolve())
    except ValueError:
        return None
    return rel.parts


def _resolve_project_slug(phase_dir: Path, vault_path: Path) -> str | None:
    """projects/<org>/<slug>/gsd-planning/phases/<phase> → returns <slug>."""
    parts = _vault_relative_parts(phase_dir, vault_path)
    if parts is None or len(parts) < 6:
        return None
    if parts[0] != "projects" or parts[3] != "gsd-planning" or parts[4] != "phases":
        return None
    return parts[2]


def _project_note_path(phase_dir: Path, vault_path: Path, slug: str) -> Path:
    """Locate the project note.

    Three conventions tried in order:
      1. ``projects/<org>/<slug>/<slug>.md``        (inside the project dir, slug-named)
      2. ``projects/<org>/<slug>/<Display Name>.md`` (inside, any .md tagged `project`)
      3. ``projects/<org>/<slug>.md``                (next to the project dir, legacy)

    Convention 2 covers vaults where project notes use Obsidian display-name
    filenames (e.g. ``Infinite Worlds.md``, ``Proteus.md``, ``ORDS.md``) rather
    than the directory slug. We pick the first ``*.md`` in the project root
    whose frontmatter has ``project`` in its ``tags``.

    Returns the chosen path; if no match found, returns the convention-1 path
    (so the caller can emit its "missing project note" warning).
    """
    parts = _vault_relative_parts(phase_dir, vault_path)
    if parts is None or len(parts) < 4 or parts[0] != "projects":
        return vault_path / "projects" / f"{slug}.md"

    project_root = vault_path / "projects" / parts[1] / parts[2]
    slug_named = project_root / f"{parts[2]}.md"
    if slug_named.is_file():
        return slug_named

    if project_root.is_dir():
        for candidate in sorted(project_root.glob("*.md")):
            try:
                post = frontmatter.load(str(candidate))
            except Exception:
                continue
            tags = post.metadata.get("tags")
            tag_list = (
                tags if isinstance(tags, list)
                else [tags] if isinstance(tags, str)
                else []
            )
            if any(str(t).strip() == "project" for t in tag_list):
                return candidate

    return vault_path / "projects" / parts[1] / f"{parts[2]}.md"


def _derive_title(dir_name: str, phase_number: str) -> str:
    """Strip the phase_number prefix from `dir_name`; dashes → spaces."""
    rest = dir_name
    if phase_number.startswith("empiria-"):
        prefix = phase_number + "-"
    elif "." in phase_number:
        prefix = phase_number.replace(".", "-") + "-"
    else:
        prefix = phase_number + "-"
    if rest.startswith(prefix):
        rest = rest[len(prefix):]
    return rest.replace("-", " ").strip()


_ROADMAP_LINE_RE = re.compile(
    r"^- \[(?P<box>[ xX])\] \*\*(?:Phase )?(?P<num>\S+?)(?::|\*\*).*?(?:—|--)\s*(?P<rest>.+)$",
    re.MULTILINE,
)


def _extract_roadmap_summary(roadmap_text: str, phase_number: str) -> str | None:
    """Pull the post-em-dash text from the ROADMAP line for this phase, if any.

    Used by ``derive_phase_card`` to populate ``summary:`` with explanatory
    text for CLOSED/MIGRATED phases (D-14). Returns None if no match.
    """
    if not roadmap_text:
        return None
    for m in _ROADMAP_LINE_RE.finditer(roadmap_text):
        if m.group("num") == phase_number:
            text = m.group("rest").strip()
            return text or None
    return None


def _derive_artifact_wikilinks(phase_dir: Path) -> tuple[str | None, str | None, str | None]:
    """Pick the first ``*-PLAN.md``, ``*-SUMMARY.md`` (any plan), and the
    single ``*-VALIDATION.md`` if present.

    Returns ``(plan_wikilink, summary_doc_wikilink, validation_wikilink)``
    where each value is either a display-name short-form wikilink
    (``[[XX-YY-PLAN]]``) or None.

    Summary selection: prefers a phase-level summary (``<phase>-SUMMARY.md``,
    no plan number component) over plan-level summaries (``<phase>-NN-SUMMARY.md``).
    A phase-level summary stem has exactly one numeric/prefix component before
    ``-SUMMARY``; a plan-level summary has two (phase + plan). This ensures
    ``[[27-SUMMARY]]`` beats ``[[27-01-SUMMARY]]`` when both exist (impl-5 / D-17).
    """
    if not phase_dir.is_dir():
        return (None, None, None)

    plans = sorted(p for p in phase_dir.iterdir() if p.name.endswith("-PLAN.md"))
    summaries = sorted(
        p for p in phase_dir.iterdir() if p.name.endswith("-SUMMARY.md")
    )
    validations = sorted(
        p for p in phase_dir.iterdir() if p.name.endswith("-VALIDATION.md")
    )

    # Prefer a phase-level summary (e.g. 27-SUMMARY.md) over plan-level ones
    # (e.g. 27-01-SUMMARY.md).  A phase-level summary stem has the shape
    # ``<phase_prefix>-SUMMARY`` — the part before ``-SUMMARY`` does NOT contain
    # a second hyphen-separated plan number.  Concretely: stem.count('-') == 1
    # for ``27-SUMMARY``; stem.count('-') >= 2 for ``27-01-SUMMARY``.
    _PLAN_NUM_RE = re.compile(r"^.+-\d{2}-SUMMARY$")
    phase_level_summaries = [s for s in summaries if not _PLAN_NUM_RE.match(s.stem)]
    chosen_summary = (phase_level_summaries or summaries)

    def _wikilink(p: Path | None) -> str | None:
        if p is None:
            return None
        return f"[[{p.stem}]]"

    return (
        _wikilink(plans[0] if plans else None),
        _wikilink(chosen_summary[0] if chosen_summary else None),
        _wikilink(validations[0] if validations else None),
    )


def derive_phase_card(
    phase_dir: Path,
    vault_path: Path,
    console: Console | None = None,
) -> PhaseCard:
    """Derive a PhaseCard from a single phase directory.

    T-37-01 path-traversal guard: rejects `phase_dir` not under `vault_path`.
    """
    err_console = console if console is not None else Console(stderr=True)

    resolved_phase = phase_dir.resolve()
    resolved_vault = vault_path.resolve()
    if not resolved_phase.is_relative_to(resolved_vault):
        raise ValueError(
            f"phase_dir {phase_dir!s} is not within vault_path {vault_path!s}"
        )

    dir_name = phase_dir.name
    phase_number = parse_phase_number(dir_name)
    slug = _resolve_project_slug(phase_dir, vault_path) or dir_name
    project_note = _project_note_path(phase_dir, vault_path, slug)
    if project_note.is_file():
        wikilink_target = project_note.stem
    else:
        try:
            rel_note = project_note.relative_to(vault_path)
        except ValueError:
            rel_note = project_note
        err_console.print(
            f"[yellow]warning:[/yellow] missing project note for [bold]{slug}[/bold] "
            f"(expected at {rel_note})"
        )
        wikilink_target = slug
    project_wikilink = f"[[{wikilink_target}]]"

    # gsd-planning is the parent of `phases/`, which is the parent of phase_dir.
    project_root = phase_dir.parent.parent
    state = read_state_md(project_root / "STATE.md")
    roadmap_text = _read_roadmap_text(project_root / "ROADMAP.md")

    status = derive_status(phase_dir, state, roadmap_text, phase_number)

    milestone = state.get("milestone")
    if milestone is not None:
        milestone = str(milestone)

    started_at = git_first_add_in_dir(phase_dir, vault_path)
    completed_at = git_last_summary_commit(phase_dir, vault_path) if status == "complete" else None

    plan_wl, summary_doc_wl, validation_wl = _derive_artifact_wikilinks(phase_dir)

    # D-14 — CLOSED/MIGRATED phases inherit explanatory ROADMAP text into
    # ``summary:`` so the card surfaces the reason for completion.
    roadmap_summary = _extract_roadmap_summary(roadmap_text, phase_number) or ""

    return PhaseCard(
        project=project_wikilink,
        milestone=milestone,
        phase_number=phase_number,
        status=status,
        title=_derive_title(dir_name, phase_number),
        depends_on=[],
        blocked_on=None,
        started_at=started_at,
        completed_at=completed_at,
        summary=roadmap_summary,
        plan=plan_wl,
        summary_doc=summary_doc_wl,
        validation=validation_wl,
    )


# --------------------------------------------------------------------------- #
# Plan 37-03 — writer, discovery, validation                                  #
# --------------------------------------------------------------------------- #


def card_to_dict(card: PhaseCard) -> OrderedDict:
    """Convert PhaseCard → ordered dict for YAML serialisation.

    Field order matches the schema in ``docs/reference/vault-taxonomy.md``
    §Phase Cards so on-disk diffs are predictable. Phase 38 MUST use this
    same ordering or re-writes will appear changed even when content is
    semantically identical.
    """
    return OrderedDict(
        [
            ("tags", ["phase"]),
            ("project", card.project),
            ("milestone", card.milestone),
            ("phase_number", card.phase_number),
            ("status", card.status),
            ("title", card.title),
            ("depends_on", list(card.depends_on or [])),
            ("blocked_on", card.blocked_on),
            ("started_at", card.started_at),
            ("completed_at", card.completed_at),
            ("summary", card.summary or ""),
            ("plan", card.plan),
            ("summary_doc", card.summary_doc),
            ("validation", card.validation),
        ]
    )


def _metadata_equal(existing: dict, new: OrderedDict) -> bool:
    """Compare frontmatter dicts semantically (key set + values).

    python-frontmatter's ``existing.metadata`` is a plain dict; comparing
    OrderedDict to dict with ``==`` works because Python's dict equality
    ignores ordering. Lists are compared element-wise.
    """
    return dict(existing) == dict(new)


def write_phase_md(
    phase_dir: Path,
    card: PhaseCard,
    *,
    dry_run: bool = False,
) -> str:
    """Idempotent write — returns 'created' | 'updated' | 'unchanged' | 'dry-run'.

    Pattern (RESEARCH §Pattern 2):

    1. Load existing ``phase.md`` (if any) via python-frontmatter.
    2. Derive the new metadata dict via ``card_to_dict()``.
    3. If no file exists → 'created' (or 'dry-run' if ``dry_run=True``).
    4. If file exists and metadata is identical → 'unchanged' (no write).
    5. Otherwise → 'updated' — preserve the existing body content verbatim
       and replace only the frontmatter dict.

    Phase 38 reuses this entry point for lifecycle transitions.
    """
    phase_md_path = phase_dir / "phase.md"
    new_metadata = card_to_dict(card)

    existing = frontmatter.load(str(phase_md_path)) if phase_md_path.exists() else None

    if existing is None:
        new_post = frontmatter.Post("", **new_metadata)
        action = "created"
    elif not _metadata_equal(existing.metadata, new_metadata):
        # Preserve user-edited body (RESEARCH §Pattern 2 — load → replace
        # meta → dump). Only the frontmatter dict is replaced.
        new_post = frontmatter.Post(existing.content, **new_metadata)
        action = "updated"
    else:
        return "unchanged"

    if dry_run:
        return "dry-run"

    phase_md_path.write_text(frontmatter.dumps(new_post) + "\n")
    return action


# Project slug for ``--project`` flag. Mirrors the org/code shape used by
# every vault project directory. ``a-zA-Z0-9`` plus internal dashes;
# never starts with a dash; no dots, slashes, or whitespace.
_PROJECT_SLUG_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9-]*/[a-zA-Z0-9][a-zA-Z0-9-]*$"
)


def validate_project_slug(slug: str, vault_path: Path) -> Path:
    """Validate the ``--project`` flag value (T-37-01 path-traversal mitigation).

    Accepts shape ``org/code`` (e.g. ``empiria/mnemosyne``). Rejects:

    - Anything containing ``..``
    - Anything starting with ``/`` or ``~``
    - Anything not matching the strict slug regex
    - Anything that, after resolution, falls outside the vault root

    Returns the resolved absolute path to the project directory. The
    directory itself need not exist (callers may surface that as a separate
    error) — the guarantee is only that the path is *inside* the vault.
    """
    if (
        ".." in slug
        or slug.startswith("/")
        or slug.startswith("~")
        or not _PROJECT_SLUG_RE.match(slug)
    ):
        raise ValueError(
            f"Invalid --project value: {slug!r}. Expected 'org/code' shape "
            "(letters, digits, dashes only; no '..', no leading slash)."
        )

    vault_root = vault_path.resolve()
    candidate = (vault_root / "projects" / slug).resolve()
    if not candidate.is_relative_to(vault_root):
        raise ValueError(
            f"--project {slug!r} resolves outside vault root {vault_root}"
        )
    return candidate


def discover_phase_dirs(
    vault_path: Path,
    project_scope: str | None,
) -> list[Path]:
    """Return all phase directories in the vault, sorted.

    If ``project_scope`` is set, restricts to that one project (after
    validating via ``validate_project_slug``). Otherwise enumerates every
    ``projects/<org>/<code>/gsd-planning/phases/*`` directory.

    Skips non-directory entries (e.g. stray .md files at phases/ root).
    """
    if project_scope is not None:
        project_dir = validate_project_slug(project_scope, vault_path)
        phases_root = project_dir / "gsd-planning" / "phases"
        if not phases_root.is_dir():
            return []
        return sorted(d for d in phases_root.iterdir() if d.is_dir())

    return sorted(
        d
        for d in (vault_path / "projects").glob("*/*/gsd-planning/phases/*")
        if d.is_dir()
    )


# --------------------------------------------------------------------------- #
# Phase 38 additions — lifecycle event application, atomic write, helpers     #
# --------------------------------------------------------------------------- #

_VALID_EVENTS = {"added", "planned", "in-progress", "complete", "blocked", "unblocked"}

# Events for which --phase is allowed to be omitted (STATE.md fallback applies).
# Per RESEARCH.md Pitfall 2: cmdStateAddBlocker / cmdStateResolveBlocker don't
# receive a phase number; Python infers from STATE.md's `Current Phase` field.
_PHASE_OPTIONAL_EVENTS = {"blocked", "unblocked"}


def apply_event(card: PhaseCard, event: str, reason: str | None = None) -> PhaseCard:
    """Pure function: produce a new card with the event's mutations applied.

    No I/O. Caller is responsible for writing the result. Raises ValueError
    on unknown event or missing reason for 'blocked'.

    Event → mutation map (CONTEXT.md D-03, copied verbatim):
      added       → status=planned
      planned     → status=ready (plan field set by derive)
      in-progress → status=in-progress; started_at=today (preserve if already set)
      complete    → status=complete; completed_at=today (summary_doc set by caller via re-derive)
      blocked     → status=blocked; blocked_on=reason (reason required)
      unblocked   → blocked_on=None (status restored by caller via re-derive)
    """
    if event not in _VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(_VALID_EVENTS)}")

    today = date.today().isoformat()  # YYYY-MM-DD

    if event == "added":
        return replace(card, status="planned")
    if event == "planned":
        return replace(card, status="ready")
    if event == "in-progress":
        new_started = card.started_at or today  # first-start-wins
        return replace(card, status="in-progress", started_at=new_started)
    if event == "complete":
        return replace(card, status="complete", completed_at=today)
    if event == "blocked":
        if not reason:
            raise ValueError("--reason is required for --event blocked")
        return replace(card, status="blocked", blocked_on=reason)
    if event == "unblocked":
        return replace(card, blocked_on=None)

    raise AssertionError(f"unreachable: {event}")


_PHASE_DIR_PREFIX_RE = re.compile(r"^(\d+(?:-\d+)?|empiria-\d+)-")


def resolve_phase_dir(vault: Path, phase_id: str, project: str | None = None) -> Path | None:
    """Find the phase directory for a given phase identifier.

    Args:
      vault: vault root Path
      phase_id: '38' | '195.02' | 'empiria-01'
      project: vault-relative project path (e.g. 'empiria/mnemosyne'); None = search all

    Returns: Path to the phase directory, or None if not found or ambiguous (>=2 matches).

    Path traversal mitigation (T-37-01 carry-forward): reject project containing
    '..' or starting with '/'.
    """
    phase_dir_prefix = phase_id.replace(".", "-")

    if project is not None:
        if ".." in project or project.startswith("/"):
            return None
        # Reuse Phase 37's validator for stronger checks (raises ValueError on bad slug).
        try:
            validated = validate_project_slug(project, vault)
        except (ValueError, Exception):
            return None
        search_root = validated / "gsd-planning" / "phases"
        if not search_root.is_dir():
            return None
        candidates = [
            d for d in search_root.iterdir()
            if d.is_dir() and d.name.startswith(phase_dir_prefix + "-")
        ]
    else:
        candidates = [
            c for c in vault.glob(f"projects/*/*/gsd-planning/phases/{phase_dir_prefix}-*")
            if c.is_dir()
        ]

    if len(candidates) == 1:
        return candidates[0]
    return None  # 0 or >=2 — caller decides (D-08: silent skip)


# Match lines like "**Current Phase:** 38" or "Current Phase: 38-some-slug"
# in the STATE.md frontmatter or body. Tolerate bold/asterisks and trailing text.
_CURRENT_PHASE_RE = re.compile(
    r"^\s*(?:\*{0,2}\s*)?Current Phase\s*(?:\*{0,2})\s*:\s*(?:\*{0,2}\s*)?(?P<id>[\w.-]+)",
    re.MULTILINE,
)


def read_current_phase_from_state(vault: Path, project: str | None = None) -> str | None:
    """Read STATE.md and return the phase identifier of the current phase.

    Used by `mnemosyne phase update` when --phase is omitted for blocker/unblocker
    events (RESEARCH.md Open Question 4 + Pitfall 2 resolution).

    Args:
      vault: vault root Path
      project: vault-relative project slug (e.g. 'empiria/mnemosyne'). If None,
        STATE.md cannot be located deterministically (we have no single-project
        STATE.md path), so we return None.

    Returns: phase identifier string ('38', '195.02', 'empiria-01'), or None if
      STATE.md cannot be read, parsed, or has no `Current Phase` field.

    Silent-no-op semantics: never raises. Returns None on any I/O or parse failure
    so the caller can emit a stderr warning and exit 0 (D-08).
    """
    if project is None:
        return None
    try:
        validated = validate_project_slug(project, vault)
    except Exception:
        return None
    state_md = validated / "gsd-planning" / "STATE.md"
    if not state_md.is_file():
        return None
    try:
        contents = state_md.read_text(encoding="utf-8")
    except Exception:
        return None

    # Try frontmatter first (a structured `current_phase:` key wins if present).
    try:
        post = frontmatter.loads(contents)
        for key in ("current_phase", "currentPhase", "Current Phase"):
            value = post.metadata.get(key)
            if value:
                # Strip wikilink decoration if any: [[38-foo]] → 38-foo → 38
                raw = str(value).strip("[] ")
                # Take the leading phase ID (digits or empiria-NN) before any '-slug'
                m = re.match(r"^(empiria-\d+|\d+(?:\.\d+)?)", raw)
                if m:
                    return m.group(1)
                return raw or None
    except Exception:
        pass  # fall through to regex body scan

    # Fallback: scan the body for `Current Phase: X` lines.
    m = _CURRENT_PHASE_RE.search(contents)
    if m:
        raw = m.group("id").strip("[] ")
        m2 = re.match(r"^(empiria-\d+|\d+(?:\.\d+)?)", raw)
        if m2:
            return m2.group(1)
        return raw or None

    return None


def write_phase_md_atomic(target: Path, card: PhaseCard, body: str = "") -> None:
    """Atomic write: temp file in same dir → fsync → os.replace.

    POSIX + Windows atomic (Pitfall 5: os.replace, NOT os.rename).
    Body preserved verbatim — frontmatter-only mutation (D-06).
    """
    metadata = dict(card_to_dict(card))
    post = frontmatter.Post(body, **metadata)
    serialized = frontmatter.dumps(post) + "\n"

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".phase.md.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())  # durability before rename
        os.replace(str(tmp_path), str(target))  # atomic on POSIX + Windows (Python 3.3+)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def card_from_frontmatter(metadata: dict) -> PhaseCard:
    """Reverse of card_to_dict — reconstruct PhaseCard from a parsed frontmatter dict.

    Tolerates missing optional fields (returns dataclass defaults).
    """
    return PhaseCard(
        project=metadata.get("project", ""),
        milestone=metadata.get("milestone"),
        phase_number=str(metadata.get("phase_number", "")),
        status=metadata.get("status", "planned"),
        title=metadata.get("title", ""),
        depends_on=list(metadata.get("depends_on") or []),
        blocked_on=metadata.get("blocked_on"),
        started_at=metadata.get("started_at"),
        completed_at=metadata.get("completed_at"),
        summary=metadata.get("summary", "") or "",
        plan=metadata.get("plan"),
        summary_doc=metadata.get("summary_doc"),
        validation=metadata.get("validation"),
    )
