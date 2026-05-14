"""Shared fixtures for tests/phase_card/.

Provides:

- ``synthetic_vault`` — a temp directory laid out like a real vault with
  enough phase dirs to exercise every D-02 status-cascade rule and every
  phase_number shape (canonical, decimal sub-phase, empiria- prefix).
- ``fake_git_repo`` — ``synthetic_vault`` with a git history simulating
  the Phase 32 hard-cut migration (``_planning/`` → ``projects/empiria/
  mnemosyne/gsd-planning/``). Used to verify
  ``git_first_add_in_dir`` returns the *original* add date, not the
  rename commit date (RESEARCH §Pitfall 1).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write(path: Path, content: str) -> None:
    """Create parents and write text content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    """Run a git subprocess; raise on failure."""
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=full_env,
    )


def _git_commit(
    cwd: Path,
    message: str,
    author_date: str,
) -> None:
    """Commit staged changes with a fixed author and committer date."""
    env = {
        "GIT_AUTHOR_DATE": author_date,
        "GIT_COMMITTER_DATE": author_date,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    _git(cwd, "commit", "-m", message, env=env)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def synthetic_vault(tmp_path: Path) -> Path:
    """Create a synthetic vault root with sample phase dirs for cascade testing.

    Layout::

        projects/empiria/mnemosyne.md
        projects/empiria/mnemosyne/gsd-planning/STATE.md          (milestone: v1.0, current_phase: 100)
        projects/empiria/mnemosyne/gsd-planning/ROADMAP.md         (canonical [x] format)
        projects/empiria/mnemosyne/gsd-planning/phases/
            27-complete-via-summaries/27-01-PLAN.md  + 27-01-SUMMARY.md  (rule 1)
            18-closed-roadmap-checkbox/                                  (rule 5, [x] in ROADMAP, no SUMMARY)
            29-ready/29-01-PLAN.md                                       (rule 3)
            37-planned-empty/                                            (rule 4)
            100-current-in-progress/100-01-PLAN.md                       (rule 2 — STATE current_phase=100)

        projects/empiria/friendly-fox.md
        projects/empiria/friendly-fox/gsd-planning/STATE.md            (milestone: v2.0)
        projects/empiria/friendly-fox/gsd-planning/ROADMAP.md           (IW format)
        projects/empiria/friendly-fox/gsd-planning/phases/
            142-iw-format/142-01-PLAN.md  + 142-01-SUMMARY.md            (rule 5 IW fallback)
            empiria-01-explore/empiria-01-01-PLAN.md                     (D-15)
            195-02-decimal-sub/195-02-01-PLAN.md                         (D-15 decimal)
    """
    vault = tmp_path / "vault"
    vault.mkdir()

    # ----- Project: mnemosyne (canonical) ----------------------------------
    mneme_root = vault / "projects" / "empiria" / "mnemosyne"
    _write(
        vault / "projects" / "empiria" / "mnemosyne.md",
        "---\n"
        "tags: [project]\n"
        'organisation: "[[Empiria]]"\n'
        "---\n\n# mnemosyne\n",
    )
    _write(
        mneme_root / "gsd-planning" / "STATE.md",
        "---\n"
        "milestone: v1.0\n"
        "current_phase: 100\n"
        "status: executing\n"
        "---\n\n# State\n",
    )
    _write(
        mneme_root / "gsd-planning" / "ROADMAP.md",
        "# Roadmap\n\n"
        "- [x] **Phase 18: closed-and-superseded** — done\n"
        "- [x] **Phase 27: complete-via-summaries** — done\n"
        "- [ ] **Phase 29: ready** — planned\n"
        "- [ ] **Phase 37: planned-empty** — planned\n"
        "- [ ] **Phase 100: current-in-progress** — in progress\n",
    )

    # Phase 27 — rule 1: every PLAN has SUMMARY
    p27 = mneme_root / "gsd-planning" / "phases" / "27-complete-via-summaries"
    _write(p27 / "27-01-PLAN.md", "# Plan 27-01\n")
    _write(p27 / "27-01-SUMMARY.md", "# Summary 27-01\n")

    # Phase 18 — rule 5 canonical: no SUMMARY but ROADMAP has [x]
    p18 = mneme_root / "gsd-planning" / "phases" / "18-closed-roadmap-checkbox"
    p18.mkdir(parents=True, exist_ok=True)

    # Phase 29 — rule 3: PLAN, no SUMMARY
    p29 = mneme_root / "gsd-planning" / "phases" / "29-ready"
    _write(p29 / "29-01-PLAN.md", "# Plan 29-01\n")

    # Phase 37 — rule 4: empty dir
    p37 = mneme_root / "gsd-planning" / "phases" / "37-planned-empty"
    p37.mkdir(parents=True, exist_ok=True)

    # Phase 100 — rule 2: STATE current_phase=100, status=executing
    p100 = mneme_root / "gsd-planning" / "phases" / "100-current-in-progress"
    _write(p100 / "100-01-PLAN.md", "# Plan 100-01\n")

    # ----- Project: friendly-fox (IW format, decimal, empiria-prefix) ------
    ff_root = vault / "projects" / "empiria" / "friendly-fox"
    _write(
        vault / "projects" / "empiria" / "friendly-fox.md",
        "---\n"
        "tags: [project]\n"
        'organisation: "[[Empiria]]"\n'
        "---\n\n# friendly-fox\n",
    )
    _write(
        ff_root / "gsd-planning" / "STATE.md",
        "---\n"
        "milestone: v2.0\n"
        "current_phase: 397\n"
        "status: executing\n"
        "---\n\n# State\n",
    )
    # IW-format ROADMAP: per-phase H3 with "Plans: N/N plans complete"
    _write(
        ff_root / "gsd-planning" / "ROADMAP.md",
        "# IW Roadmap\n\n"
        "| 142 | 2026-01-21 | IW format complete |\n\n"
        "### Phase 142: iw-format\n\n"
        "**Plans:** 1/1 plans complete\n\n"
        "### Phase empiria-01: explore\n\n"
        "Status: ready\n\n"
        "### Phase 195.02: decimal-sub\n\n"
        "Status: ready\n",
    )

    # IW phase 142 — rule 5 IW fallback
    p142 = ff_root / "gsd-planning" / "phases" / "142-iw-format"
    _write(p142 / "142-01-PLAN.md", "# Plan 142-01\n")
    _write(p142 / "142-01-SUMMARY.md", "# Summary 142-01\n")

    # empiria-01 — D-15 non-integer prefix
    pe1 = ff_root / "gsd-planning" / "phases" / "empiria-01-explore"
    _write(pe1 / "empiria-01-01-PLAN.md", "# Plan empiria-01-01\n")

    # 195-02 — D-15 decimal sub-phase
    p195 = ff_root / "gsd-planning" / "phases" / "195-02-decimal-sub"
    _write(p195 / "195-02-01-PLAN.md", "# Plan 195-02-01\n")

    return vault


@pytest.fixture
def fake_git_repo(tmp_path: Path) -> Path:
    """Init a temp vault with a git history simulating Phase 32 hard-cut.

    History (in order):

    1. **2026-03-02** — add ``_planning/phases/01-original/01-01-PLAN.md``
       (the original pre-migration path).
    2. **2026-04-17** — ``git mv _planning projects/empiria/mnemosyne/gsd-planning``
       (the Phase 32 hard-cut rename — registered as 'R' diff status).
    3. **2026-04-18** — touch a SUMMARY in another phase dir
       (unrelated; ensures the log has more than one commit).

    A correct ``git_first_add_in_dir`` for phase ``01-original`` returns
    **2026-03-02** — the original add. A naïve implementation returns
    2026-04-17 (the migration commit), which is the load-bearing failure
    Phase 32 introduced.
    """
    vault = tmp_path / "fake-vault"
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")

    # Commit 1 — original add at pre-migration path (2026-03-02)
    orig_dir = vault / "_planning" / "phases" / "01-original"
    _write(orig_dir / "01-01-PLAN.md", "# Plan 01-01 original\n")
    _git(vault, "add", "_planning/phases/01-original/01-01-PLAN.md")
    _git_commit(vault, "initial: add original 01 plan", "2026-03-02T12:00:00Z")

    # Commit 2 — Phase 32 hard-cut: rename _planning/ -> projects/empiria/mnemosyne/gsd-planning/
    new_root = vault / "projects" / "empiria" / "mnemosyne" / "gsd-planning"
    new_root.parent.mkdir(parents=True, exist_ok=True)
    _git(vault, "mv", "_planning", "projects/empiria/mnemosyne/gsd-planning")
    _git_commit(
        vault,
        "migrate _planning/ to projects/empiria/mnemosyne/gsd-planning/",
        "2026-04-17T17:42:05Z",
    )

    # Commit 3 — add an unrelated SUMMARY at the new path (2026-04-18)
    other = new_root / "phases" / "27-other" / "27-01-SUMMARY.md"
    _write(other, "# Summary 27-01\n")
    _git(vault, "add", str(other.relative_to(vault)))
    _git_commit(vault, "add 27-01-SUMMARY for phase 27", "2026-04-18T09:00:00Z")

    return vault
