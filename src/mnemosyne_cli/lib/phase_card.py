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

import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
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
    """Project note lives at `projects/<org>/<slug>.md` next to the project dir."""
    parts = _vault_relative_parts(phase_dir, vault_path)
    if parts is None or len(parts) < 4 or parts[0] != "projects":
        return vault_path / "projects" / f"{slug}.md"
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

    def _wikilink(p: Path | None) -> str | None:
        if p is None:
            return None
        return f"[[{p.stem}]]"

    return (
        _wikilink(plans[0] if plans else None),
        _wikilink(summaries[0] if summaries else None),
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
    if not project_note.is_file():
        try:
            rel_note = project_note.relative_to(vault_path)
        except ValueError:
            rel_note = project_note
        err_console.print(
            f"[yellow]warning:[/yellow] missing project note for [bold]{slug}[/bold] "
            f"(expected at {rel_note})"
        )
    project_wikilink = f"[[{slug}]]"

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
