"""Unit tests for mnemosyne_cli.lib.phase_card."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from mnemosyne_cli.lib.phase_card import (
    derive_phase_card,
    git_first_add_in_dir,
    is_phase_marked_complete,
    parse_phase_number,
)


def test_parse_phase_number_integer():
    assert parse_phase_number("37-phase-md-schema-and-bases-kanban-view") == "37"


def test_parse_phase_number_decimal_subphase():
    assert parse_phase_number("195-02-fix-scroll-position-reset") == "195.02"


def test_parse_phase_number_empiria_prefix():
    assert parse_phase_number("empiria-01-explore-testing") == "empiria-01"


def test_status_cascade_complete_via_summaries(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/27-complete-via-summaries"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.status == "complete"


def test_status_cascade_complete_via_roadmap_checkbox(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/18-closed-roadmap-checkbox"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.status == "complete"


def test_status_cascade_in_progress_via_state(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/100-current-in-progress"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.status == "in-progress"


def test_status_cascade_ready_via_plan_no_summary(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/29-ready"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.status == "ready"


def test_status_cascade_planned_empty_dir(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/37-planned-empty"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.status == "planned"


def test_iw_roadmap_plans_n_of_n_complete(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/friendly-fox/gsd-planning/phases/142-iw-format"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.status == "complete"


def test_is_phase_marked_complete_iw_format():
    text = "### Phase 142: iw-format\n\n**Plans:** 4/4 plans complete\n"
    assert is_phase_marked_complete(text, "142") is True


def test_is_phase_marked_complete_unchecked_canonical():
    text = "- [ ] **Phase 37: not-yet-done** -- planned\n"
    assert is_phase_marked_complete(text, "37") is False


def test_started_at_after_migration(fake_git_repo):
    phase_dir = fake_git_repo / "projects/empiria/mnemosyne/gsd-planning/phases/01-original"
    date = git_first_add_in_dir(phase_dir, fake_git_repo)
    assert date == "2026-03-02"


def test_phase_number_string_decimal_preserved(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/friendly-fox/gsd-planning/phases/195-02-decimal-sub"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.phase_number == "195.02"
    assert isinstance(card.phase_number, str)


def test_phase_number_empiria_prefix_preserved(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/friendly-fox/gsd-planning/phases/empiria-01-explore"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.phase_number == "empiria-01"


def test_missing_milestone_in_state(synthetic_vault):
    state_path = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/STATE.md"
    state_path.write_text("---\ncurrent_phase: 27\nstatus: executing\n---\n")
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/27-complete-via-summaries"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.milestone is None


def test_milestone_inheritance_from_state(synthetic_vault):
    for slug in (
        "27-complete-via-summaries",
        "29-ready",
        "37-planned-empty",
        "100-current-in-progress",
    ):
        phase_dir = synthetic_vault / f"projects/empiria/mnemosyne/gsd-planning/phases/{slug}"
        card = derive_phase_card(phase_dir, synthetic_vault)
        assert card.milestone == "v1.0"


def test_wikilink_short_form(synthetic_vault):
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/27-complete-via-summaries"
    card = derive_phase_card(phase_dir, synthetic_vault)
    assert card.project == "[[mnemosyne]]"


def test_missing_project_warning(synthetic_vault):
    (synthetic_vault / "projects/empiria/mnemosyne.md").unlink()
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/27-complete-via-summaries"
    err_buf = io.StringIO()
    console = Console(file=err_buf, force_terminal=False, stderr=True)
    card = derive_phase_card(phase_dir, synthetic_vault, console=console)
    assert card.project == "[[mnemosyne]]"
    output = err_buf.getvalue().lower()
    assert "warning" in output or "missing" in output


def test_frontmatter_injection_safe():
    import frontmatter

    hostile = ": foo\nbar: baz\n---\nnot-frontmatter"
    post = frontmatter.Post("", title=hostile, project="[[mnemosyne]]", status="planned")
    serialised = frontmatter.dumps(post)
    round_trip = frontmatter.loads(serialised)
    assert round_trip["title"] == hostile
    assert round_trip["status"] == "planned"


def test_public_api_exports():
    from mnemosyne_cli.lib.phase_card import (  # noqa: F401
        PhaseCard,
        derive_phase_card,
        derive_status,
        git_first_add_in_dir,
        git_last_summary_commit,
        is_phase_marked_complete,
        parse_phase_number,
        read_state_md,
    )
