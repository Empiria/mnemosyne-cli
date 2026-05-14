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
- ``closed_phase_dir`` — extends ``synthetic_vault`` with a CLOSED-style
  phase directory (D-14 — ROADMAP entry has ``CLOSED:`` text) so the
  backfill emits ``status: complete`` with explanatory ``summary:``.
- ``phase_md_with_user_body`` — pre-creates a ``phase.md`` with user-edited
  body text under existing frontmatter. Used to verify backfill preserves
  body content verbatim (RESEARCH §Pattern 2).
- ``multivault_config`` — registers two vaults in a temp config.toml with
  no ``[[vault_rules]]`` (closed-by-default — Phase 19-03). Used to verify
  vault B is NOT iterated when ``can_read(active, secondary)`` is False
  (T-37-04 mitigation).
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


# --------------------------------------------------------------------------- #
# Fixtures added by Plan 37-03 (backfill writer)
# --------------------------------------------------------------------------- #


@pytest.fixture
def closed_phase_dir(synthetic_vault: Path) -> Path:
    """Add a CLOSED-style phase (D-14) to ``synthetic_vault``.

    Appends a ROADMAP entry whose post-em-dash text begins with ``CLOSED:``
    and creates the matching phase directory. Used by
    ``test_closed_phase_gets_complete_with_explanation``.

    Uses phase number ``38`` (unused by the rest of ``synthetic_vault``) to
    avoid colliding with the existing phase-18 ROADMAP entry, since
    ``parse_phase_number`` strips text after the leading number.

    Returns the path to the new phase dir.
    """
    mneme_root = synthetic_vault / "projects" / "empiria" / "mnemosyne"
    roadmap = mneme_root / "gsd-planning" / "ROADMAP.md"

    closed_line = (
        "- [x] **Phase 38: server-agent-infrastructure** "
        "— CLOSED: infrastructure goals superseded by SCION\n"
    )
    existing = roadmap.read_text()
    roadmap.write_text(existing + "\n" + closed_line)

    p_closed = (
        mneme_root
        / "gsd-planning"
        / "phases"
        / "38-server-agent-infrastructure"
    )
    p_closed.mkdir(parents=True, exist_ok=True)
    return p_closed


@pytest.fixture
def phase_md_with_user_body(synthetic_vault: Path) -> Path:
    """Pre-create a ``phase.md`` with valid frontmatter + a user-edited body.

    The body content must survive a backfill run unchanged
    (RESEARCH §Pattern 2 — frontmatter.Post(existing.content, **new_metadata)).

    Returns the path to the pre-created phase.md.
    """
    phase_md = (
        synthetic_vault
        / "projects"
        / "empiria"
        / "mnemosyne"
        / "gsd-planning"
        / "phases"
        / "27-complete-via-summaries"
        / "phase.md"
    )
    phase_md.parent.mkdir(parents=True, exist_ok=True)
    phase_md.write_text(
        "---\n"
        "tags:\n"
        "- phase\n"
        "project: \"[[mnemosyne]]\"\n"
        "milestone: v1.0\n"
        "phase_number: '27'\n"
        "status: complete\n"
        "title: complete via summaries\n"
        "depends_on: []\n"
        "blocked_on: null\n"
        "started_at: null\n"
        "completed_at: null\n"
        "summary: ''\n"
        "plan: null\n"
        "summary_doc: null\n"
        "validation: null\n"
        "---\n\n"
        "## Manual notes\n\n"
        "This was edited by a human.\n"
    )
    return phase_md


@pytest.fixture
def multivault_config(
    synthetic_vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Register two vaults in a temp XDG config.toml with NO cross-vault rules.

    Vault A is ``synthetic_vault`` (populated). Vault B is a sibling temp dir
    with its own minimal project layout. With no ``[[vault_rules]]`` entries,
    ``can_read(A, B)`` returns False (closed by default per Phase 19-03).

    Returns ``(vault_a_path, vault_b_path)``.

    The fixture monkey-patches ``_CONFIG_PATH`` on the already-loaded
    ``lib.vault`` module so the test sees a clean temp config without
    requiring an ``importlib.reload`` (which would leak across tests).
    """
    vault_a = synthetic_vault
    vault_b = tmp_path / "vault-b"
    vault_b.mkdir()
    # Minimal project layout in vault B so phase discovery has something
    # to find IF (counterfactually) backfill ever reached it.
    b_mneme = vault_b / "projects" / "personal" / "playground"
    b_mneme.mkdir(parents=True)
    (vault_b / "projects" / "personal" / "playground.md").write_text(
        "---\ntags: [project]\n---\n# playground\n"
    )
    (b_mneme / "gsd-planning").mkdir()
    (b_mneme / "gsd-planning" / "STATE.md").write_text(
        "---\nmilestone: v0.1\n---\n"
    )
    (b_mneme / "gsd-planning" / "ROADMAP.md").write_text("# Roadmap\n")
    (b_mneme / "gsd-planning" / "phases" / "01-secret").mkdir(parents=True)

    config_dir = tmp_path / ".config" / "mnemosyne"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "[vaults.empiria]\n"
        f'path = "{vault_a}"\n'
        'description = "Empiria primary"\n'
        'sync = "git"\n'
        "\n"
        "[vaults.personal]\n"
        f'path = "{vault_b}"\n'
        'description = "Personal sandbox"\n'
        'sync = "git"\n'
        "\n"
        "# No [[vault_rules]] — closed by default (Phase 19-03).\n"
    )

    # Re-target the module-level _CONFIG_PATH for this test only.
    # monkeypatch.setattr restores the original value at teardown, so the
    # next test sees the production config path again.
    from mnemosyne_cli.lib import vault as lib_vault
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    # MNEMOSYNE_VAULT pins the primary to vault A so resolve_primary_vault()
    # matches the "empiria" registry entry by path.
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(vault_a))

    return vault_a, vault_b
