"""Wave 0 contract tests for Phase 38 — RED phase.

Tests describe the public API Phase 02 (Python core) implements:
  - mnemosyne_cli.lib.phase_card.apply_event(card, event, reason=None) -> PhaseCard
  - mnemosyne_cli.lib.phase_card.resolve_phase_dir(vault, phase_id, project=None) -> Path | None
  - mnemosyne_cli.lib.phase_card.card_from_frontmatter(metadata) -> PhaseCard
  - mnemosyne_cli.lib.phase_card.write_phase_md_atomic(target, card, body) -> None
  - mnemosyne_cli.lib.phase_card.read_current_phase_from_state(vault, project) -> str | None  (Pitfall 2 fallback)
  - mnemosyne_cli.commands.phase.app's `update` Typer command (with OPTIONAL --phase)

Acceptance traceability (VALIDATION.md):
  ACC-38-01 → test_event_added_creates_planned
  ACC-38-02 → test_event_in_progress
  ACC-38-03 → test_event_complete
  ACC-38-04 → test_event_block_unblock
  impl-1    → test_resolve_phase_dir
  impl-2    → test_apply_event_idempotent_in_progress
  impl-3    → test_self_heal_missing_phase_md
  impl-4    → test_user_body_preserved_on_update
  impl-5    → test_summary_doc_short_wikilink
  sec-1     → test_project_path_traversal_rejected
  sec-2     → test_event_enum_rejects_unknown
  sec-3     → test_reason_frontmatter_injection_safe
  Pitfall-2 → test_phase_optional_for_blocker_falls_back_to_state
              test_phase_required_for_non_blocker_events
  Precondition → test_synthetic_vault_fixture_provides_expected_phase_dirs

Import pattern: production symbols are imported inside each test function so that
pytest --collect-only succeeds even before P02 lands. Tests FAIL at execution time
with ImportError when the symbols don't exist yet (RED phase). P02 makes them green.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter
import pytest
from typer.testing import CliRunner

runner = CliRunner()


# ---------- Fixture-validation precondition (revision iter 1 warning resolution) ----------

def test_synthetic_vault_fixture_provides_expected_phase_dirs(synthetic_vault):
    """Fail-fast: Phase 37 P2's synthetic_vault fixture must provide the phase dirs this file references.

    If Phase 37 P2's conftest.py shape changes (renames, reorganization), this test
    fails FIRST with a clear diagnostic, instead of every other test failing
    opaquely with FileNotFoundError or ResolutionError.
    """
    # These imports are from existing Phase 37 P2 symbols — should work now.
    from mnemosyne_cli.lib.phase_card import PhaseCard  # noqa: F401 — collect-only check

    # Mnemosyne project dirs (empiria/mnemosyne)
    base = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases"
    required_mneme = [
        "37-planned-empty",
        "27-complete-via-summaries",
        "29-ready",
    ]
    # friendly-fox / IW project dirs — 195-02-decimal-sub and empiria-01-explore
    # live under empiria/friendly-fox per Phase 37 P2 conftest.py layout
    ff_base = synthetic_vault / "projects/empiria/friendly-fox/gsd-planning/phases"
    required_ff = [
        "195-02-decimal-sub",
        "empiria-01-explore",
    ]
    missing_mneme = [r for r in required_mneme if not (base / r).is_dir()]
    assert not missing_mneme, (
        f"Phase 37 P2 synthetic_vault fixture missing phase dirs under empiria/mnemosyne: {missing_mneme}. "
        f"Update Phase 37 P2's conftest.py to include them, or update this test if the fixture intentionally changed."
    )
    missing_ff = [r for r in required_ff if not (ff_base / r).is_dir()]
    assert not missing_ff, (
        f"Phase 37 P2 synthetic_vault fixture missing phase dirs under empiria/friendly-fox: {missing_ff}. "
        f"Update Phase 37 P2's conftest.py to include them, or update this test if the fixture intentionally changed."
    )


# ---------- ACC-38-01: `--event added` creates phase.md with status: planned ----------

def test_event_added_creates_planned(synthetic_vault, monkeypatch):
    """Adding a phase via the update command creates phase.md with status: planned (D-03 row 1)."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/37-planned-empty"
    phase_md = phase_dir / "phase.md"
    assert not phase_md.exists(), "Precondition: phase.md absent"

    result = runner.invoke(phase_app, ["update", "--phase", "37", "--event", "added", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    assert phase_md.exists(), "Update with --event added must create phase.md (self-heal D-07)"
    post = frontmatter.load(phase_md)
    assert post["status"] == "planned"
    assert post["phase_number"] == "37"


# ---------- ACC-38-02: `--event in-progress` sets status + started_at ----------

def test_event_in_progress(existing_phase_md, synthetic_vault, monkeypatch):
    """In-progress event sets status + today's started_at (D-03 row 3)."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    # existing_phase_md was created with status: complete and started_at: 2026-04-01
    # The event in-progress should overwrite status but PRESERVE started_at if present (idempotency
    # is tested separately; here the started_at field IS present and must not be clobbered).
    result = runner.invoke(phase_app, ["update", "--phase", "27", "--event", "in-progress", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    post = frontmatter.load(existing_phase_md)
    assert post["status"] == "in-progress"
    assert post["started_at"] == "2026-04-01", (
        f"Existing started_at clobbered — got {post['started_at']!r}, expected 2026-04-01"
    )


# ---------- ACC-38-03: `--event complete` sets status + completed_at + summary_doc ----------

def test_event_complete(synthetic_vault, monkeypatch):
    """Complete event sets status, completed_at=today, summary_doc=[[<padded>-SUMMARY]] (D-03 row 4)."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    # synthetic_vault includes phases/27-complete-via-summaries/27-01-SUMMARY.md per Phase 37 P2 fixture
    # Also create a phase-level SUMMARY file so summary_doc can resolve to short form.
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/27-complete-via-summaries"
    (phase_dir / "27-SUMMARY.md").write_text("# Phase 27 Summary\n")

    result = runner.invoke(phase_app, ["update", "--phase", "27", "--event", "complete", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    post = frontmatter.load(phase_dir / "phase.md")
    assert post["status"] == "complete"
    assert post["completed_at"] == date.today().isoformat()
    # impl-5 will assert short-form wikilink; here only that the field is populated.
    assert post["summary_doc"] is not None and "27" in post["summary_doc"]


# ---------- ACC-38-04: `--event blocked` and `--event unblocked` ----------

def test_event_block_unblock(existing_phase_md, synthetic_vault, monkeypatch):
    """Block sets status=blocked + blocked_on=reason; unblock clears blocked_on and restores prior status.

    Tightened (revision iter 1 warning resolution): assert SPECIFIC cascade-derived
    status, not a generous allow-list. The Phase 37 P2 synthetic_vault fixture
    creates phases/27-complete-via-summaries/ WITH summaries for all plans, so the
    cascade derivation logic returns status="complete" for this directory.

    If this test starts failing because the fixture's shape changed, update
    Phase 37 P2's conftest.py fixture comment AND this test together.

    Phase 27 fixture has summaries for all plans — cascade derives "complete".
    """
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))

    result_block = runner.invoke(
        phase_app,
        ["update", "--phase", "27", "--event", "blocked", "--reason", "waiting on review", "--project", "empiria/mnemosyne"],
    )
    assert result_block.exit_code == 0, result_block.output

    post = frontmatter.load(existing_phase_md)
    assert post["status"] == "blocked"
    assert post["blocked_on"] == "waiting on review"

    result_unblock = runner.invoke(
        phase_app,
        ["update", "--phase", "27", "--event", "unblocked", "--project", "empiria/mnemosyne"],
    )
    assert result_unblock.exit_code == 0, result_unblock.output

    post = frontmatter.load(existing_phase_md)
    assert post["blocked_on"] is None or post["blocked_on"] == ""
    # Phase 27 fixture has summaries for all plans → cascade derives "complete".
    # This is a SPECIFIC expected value pinned to the Phase 37 P2 fixture shape.
    # If Phase 37 P2's fixture changes (e.g. drops summaries from this dir), update
    # both the fixture comment and this assertion together.
    assert post["status"] == "complete", (
        f"Unblock did not restore via re-derive. Expected 'complete' "
        f"(Phase 27 fixture has summaries for all plans, so cascade derives 'complete'). "
        f"Got {post['status']!r}. "
        f"If this changes, update synthetic_vault fixture comment in Phase 37 P2's conftest.py."
    )


# ---------- impl-1: resolve_phase_dir for 38, 195.02, empiria-01 ----------

def test_resolve_phase_dir(synthetic_vault):
    """resolve_phase_dir handles integer, decimal-sub, and empiria-prefix IDs."""
    from mnemosyne_cli.lib.phase_card import resolve_phase_dir

    # Integer
    result = resolve_phase_dir(synthetic_vault, "37", project="empiria/mnemosyne")
    assert result is not None
    assert result.name == "37-planned-empty"

    # Decimal sub-phase — Phase 37 P2 synthetic_vault includes phases/195-02-decimal-sub/
    result = resolve_phase_dir(synthetic_vault, "195.02", project="empiria/friendly-fox")
    assert result is not None
    assert result.name == "195-02-decimal-sub"

    # empiria- prefix
    result = resolve_phase_dir(synthetic_vault, "empiria-01", project="empiria/friendly-fox")
    assert result is not None
    assert result.name == "empiria-01-explore"

    # Not-found — silent None (per RESEARCH.md open question 3)
    result = resolve_phase_dir(synthetic_vault, "999", project="empiria/mnemosyne")
    assert result is None


# ---------- impl-2: apply_event idempotent on in-progress ----------

def test_apply_event_idempotent_in_progress():
    """apply_event(card, 'in-progress') preserves an existing started_at on re-application."""
    from mnemosyne_cli.lib.phase_card import PhaseCard, apply_event

    card = PhaseCard(
        project="[[mnemosyne]]",
        milestone=None,
        phase_number="38",
        status="ready",
        title="Test",
        started_at="2026-04-01",
    )
    result = apply_event(card, "in-progress")
    assert result.status == "in-progress"
    assert result.started_at == "2026-04-01", (
        "started_at must not be overwritten if already set — first-start-wins"
    )

    # apply_event is pure (no I/O) — verify by checking no file is created in cwd
    card_no_start = PhaseCard(
        project="[[mnemosyne]]",
        milestone=None,
        phase_number="38",
        status="ready",
        title="Test",
    )
    result2 = apply_event(card_no_start, "in-progress")
    assert result2.started_at == date.today().isoformat()
    # Idempotent if started_at already set:
    result3 = apply_event(result2, "in-progress")
    assert result3.started_at == result2.started_at


# ---------- impl-3: self-heal missing phase.md → derive → apply event → write ----------

def test_self_heal_missing_phase_md(synthetic_vault, monkeypatch):
    """When phase.md is absent, update derives via derive_phase_card and writes (D-07)."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/29-ready"
    phase_md = phase_dir / "phase.md"
    assert not phase_md.exists(), "Precondition: phase.md absent"

    result = runner.invoke(phase_app, ["update", "--phase", "29", "--event", "planned", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    assert phase_md.exists()
    post = frontmatter.load(phase_md)
    assert post["status"] == "ready"  # planned event → status: ready per D-03 row 2
    # derive_phase_card populates the rest of the fields
    assert post["project"] == "[[mnemosyne]]"
    assert post["phase_number"] == "29"


# ---------- impl-4: user-edited body preserved across update ----------

def test_user_body_preserved_on_update(existing_phase_md, synthetic_vault, monkeypatch):
    """existing_phase_md has body 'Manual notes'; update mutates frontmatter only."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = runner.invoke(phase_app, ["update", "--phase", "27", "--event", "in-progress", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    post = frontmatter.load(existing_phase_md)
    assert "Manual notes" in post.content
    assert "edited by a human" in post.content
    assert post["status"] == "in-progress"  # Frontmatter still updated


# ---------- impl-5: summary_doc wikilink is short form [[<padded>-SUMMARY]] ----------

def test_summary_doc_short_wikilink(synthetic_vault, monkeypatch):
    """summary_doc value matches /^\\[\\[\\d+-SUMMARY\\]\\]$/ — short form (Phase 37 D-17)."""
    import re
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/27-complete-via-summaries"
    (phase_dir / "27-SUMMARY.md").write_text("# Phase 27 Summary\n")

    result = runner.invoke(phase_app, ["update", "--phase", "27", "--event", "complete", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    post = frontmatter.load(phase_dir / "phase.md")
    summary_doc = post["summary_doc"]
    assert summary_doc is not None
    assert re.match(r"^\[\[[^\]/.]+\]\]$", summary_doc), (
        f"summary_doc not short form (no slashes, no '.md'): {summary_doc!r}"
    )
    # Specifically: '[[27-SUMMARY]]' shape
    assert summary_doc == "[[27-SUMMARY]]", f"Expected '[[27-SUMMARY]]', got {summary_doc!r}"


# ---------- sec-1: --project path traversal rejected ----------

def test_project_path_traversal_rejected(synthetic_vault, monkeypatch):
    """--project containing '..' or absolute path is rejected (T-37-01 carry-forward)."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))

    # The shim semantics (D-08) are "silent no-op" — the command should exit 0
    # but emit a warning to stderr. We assert non-crash and warning presence.
    result = runner.invoke(
        phase_app,
        ["update", "--phase", "38", "--event", "added", "--project", "../../etc/passwd"],
    )
    # Either exit 0 silently (per D-08) OR exit non-zero with error message.
    # Both shapes are acceptable; what's NOT acceptable is silent success with a write.
    # Verify no file written outside vault:
    assert not Path("/etc/passwd.phase.md").exists()

    # Stronger check: the message includes a rejection signal.
    combined_output = result.output + (result.stderr or "")
    assert "skipped" in combined_output.lower() or "invalid" in combined_output.lower() or result.exit_code != 0


# ---------- sec-2: --event validated against enum set ----------

def test_event_enum_rejects_unknown(synthetic_vault, monkeypatch):
    """Unknown --event value is rejected (validation gate before any FS op)."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = runner.invoke(
        phase_app,
        ["update", "--phase", "37", "--event", "MERGE-INTO-MAIN", "--project", "empiria/mnemosyne"],
    )
    # Per D-08 semantics: silent skip with warning. Per typer enum: non-zero exit.
    # Either is acceptable; what's NOT acceptable is an unknown event mutating a card.
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/37-planned-empty"
    phase_md = phase_dir / "phase.md"
    if phase_md.exists():
        post = frontmatter.load(phase_md)
        assert post["status"] != "MERGE-INTO-MAIN", "Unknown event mutated status — enum check missing"

    combined_output = result.output + (result.stderr or "")
    assert (
        result.exit_code != 0
        or "invalid" in combined_output.lower()
        or "unknown" in combined_output.lower()
        or "skipped" in combined_output.lower()
    )


# Direct unit test of apply_event enum guard (independent of Typer):
def test_apply_event_rejects_unknown_event():
    """apply_event raises ValueError on unknown event."""
    from mnemosyne_cli.lib.phase_card import PhaseCard, apply_event

    card = PhaseCard(
        project="[[mnemosyne]]", milestone=None, phase_number="38", status="ready", title="x",
    )
    with pytest.raises(ValueError):
        apply_event(card, "rebase-on-main")


# ---------- sec-3: --reason frontmatter injection safety ----------

def test_reason_frontmatter_injection_safe(synthetic_vault, monkeypatch):
    """--reason text containing YAML metacharacters round-trips safely via python-frontmatter.dumps()."""
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    hostile = ": injected\n---\nrogue_key: bad\n"

    result = runner.invoke(
        phase_app,
        ["update", "--phase", "37", "--event", "added", "--project", "empiria/mnemosyne"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        phase_app,
        ["update", "--phase", "37", "--event", "blocked", "--reason", hostile, "--project", "empiria/mnemosyne"],
    )
    assert result.exit_code == 0, result.output

    phase_md = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/37-planned-empty/phase.md"
    # Round-trip via python-frontmatter — if YAML escaping is broken, .load() raises or
    # produces a second 'rogue_key' field at top level.
    post = frontmatter.load(phase_md)
    assert post["blocked_on"] == hostile, (
        f"YAML injection: blocked_on did not round-trip verbatim. Got {post['blocked_on']!r}"
    )
    assert "rogue_key" not in post.metadata, (
        "YAML injection: rogue key leaked into frontmatter top level"
    )
    assert post["status"] == "blocked"


# ---------- Pitfall 2 / Open Question 4: --phase OPTIONAL for blocker/unblocker events (STATE.md fallback) ----------

def _seed_state_md_current_phase(synthetic_vault, project_relative, phase_id):
    """Helper: write a minimal STATE.md with a Current Phase field for the given project."""
    state_md = synthetic_vault / "projects" / project_relative / "gsd-planning" / "STATE.md"
    state_md.parent.mkdir(parents=True, exist_ok=True)
    state_md.write_text(
        f"# Project State\n\n**Current Phase:** {phase_id}\n\n## Decisions\n\n(none yet)\n"
    )
    return state_md


def test_phase_optional_for_blocker_falls_back_to_state(existing_phase_md, synthetic_vault, monkeypatch):
    """--phase MAY be omitted for blocked event; Python reads STATE.md's `Current Phase` (Pitfall 2).

    This covers the contract for RESEARCH.md Open Question 4 + Pitfall 2: the JS
    shim's cmdStateAddBlocker / cmdStateResolveBlocker functions don't track which
    phase a blocker applies to, so they pass null for --phase. Python falls back
    to STATE.md's `Current Phase` field via read_current_phase_from_state.
    """
    from mnemosyne_cli.commands.phase import app as phase_app
    from mnemosyne_cli.lib.phase_card import read_current_phase_from_state  # noqa: F401

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    # Seed STATE.md so the fallback can resolve to phase 27 (where existing_phase_md lives)
    _seed_state_md_current_phase(synthetic_vault, "empiria/mnemosyne", "27")

    # Invoke with NO --phase, event=blocked, reason set, project specified.
    result = runner.invoke(
        phase_app,
        ["update", "--event", "blocked", "--reason", "waiting on Pitfall 2 test", "--project", "empiria/mnemosyne"],
    )
    assert result.exit_code == 0, (
        f"Update with no --phase + event=blocked must succeed via STATE.md fallback. "
        f"Got exit_code={result.exit_code}, output={result.output!r}"
    )

    # The phase 27 phase.md should now be marked blocked with the reason
    post = frontmatter.load(existing_phase_md)
    assert post["status"] == "blocked", (
        f"STATE.md fallback failed to mutate the correct phase. Got status={post['status']!r}"
    )
    assert post["blocked_on"] == "waiting on Pitfall 2 test"


def test_phase_required_for_non_blocker_events(synthetic_vault, monkeypatch):
    """--phase IS required for non-blocker events; omitting it = D-08 silent skip with stderr warning.

    Covers the gate side of Pitfall 2: the STATE.md fallback only applies for
    blocked/unblocked events. For all other events, omitting --phase produces a
    silent skip (exit 0 + stderr warning) per D-08.
    """
    from mnemosyne_cli.commands.phase import app as phase_app

    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    # Even with STATE.md present, non-blocker events MUST require --phase explicitly.
    _seed_state_md_current_phase(synthetic_vault, "empiria/mnemosyne", "29")

    # Take a snapshot of phases/29-ready/ before invocation
    phase_dir = synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases/29-ready"
    phase_md = phase_dir / "phase.md"
    pre_existed = phase_md.exists()

    result = runner.invoke(
        phase_app,
        ["update", "--event", "in-progress", "--project", "empiria/mnemosyne"],
    )
    # D-08 silent skip: exit 0 with stderr warning. Typer's missing-option behaviour
    # (exit 2) is also acceptable since both prevent a wrong-phase mutation. What's
    # NOT acceptable is silent success that mutates an inferred phase.
    assert result.exit_code in (0, 2), (
        f"Non-blocker event without --phase must exit 0 (silent skip) or 2 (Typer missing-option); "
        f"got {result.exit_code}, output={result.output!r}"
    )
    combined_output = result.output + (result.stderr or "")
    # If exit 0, must include a "skipped" / "required" stderr signal.
    if result.exit_code == 0:
        assert (
            "skipped" in combined_output.lower()
            or "required" in combined_output.lower()
            or "missing" in combined_output.lower()
        ), f"Silent skip path must emit a stderr warning. Got: {combined_output!r}"

    # Critical: no phase.md created/mutated for phase 29 via inferred phase number.
    if not pre_existed:
        assert not phase_md.exists(), (
            "Non-blocker event without --phase must NOT create phase.md via STATE.md inference "
            "(the STATE.md fallback only applies to blocked/unblocked events)"
        )


def test_read_current_phase_from_state_helper(synthetic_vault):
    """Direct unit test of the STATE.md parser (no Typer, no apply_event)."""
    from mnemosyne_cli.lib.phase_card import read_current_phase_from_state

    # Seed STATE.md with `**Current Phase:** 38`
    _seed_state_md_current_phase(synthetic_vault, "empiria/mnemosyne", "38")
    result = read_current_phase_from_state(synthetic_vault, "empiria/mnemosyne")
    assert result == "38", f"Expected '38', got {result!r}"

    # No project given → None (cannot locate STATE.md deterministically)
    result_no_project = read_current_phase_from_state(synthetic_vault, None)
    assert result_no_project is None

    # Missing STATE.md → None (silent, no exception)
    result_missing = read_current_phase_from_state(synthetic_vault, "empiria/does-not-exist")
    assert result_missing is None


# ---------- Extra: card_from_frontmatter symmetry with card_to_dict ----------

def test_card_from_frontmatter_roundtrip():
    """card_from_frontmatter(card_to_dict(card)) == card."""
    from mnemosyne_cli.lib.phase_card import PhaseCard, card_from_frontmatter, card_to_dict

    card = PhaseCard(
        project="[[mnemosyne]]",
        milestone="v1.0",
        phase_number="38",
        status="blocked",
        title="Test",
        depends_on=["[[37]]"],
        blocked_on="waiting",
        started_at="2026-05-13",
        completed_at=None,
        summary="x",
        plan="[[38-01-PLAN]]",
        summary_doc=None,
        validation=None,
    )
    d = card_to_dict(card)
    reconstructed = card_from_frontmatter(dict(d))
    assert reconstructed == card


# ---------- Extra: apply_event blocked requires --reason ----------

def test_apply_event_blocked_requires_reason():
    """apply_event(card, 'blocked') without reason raises ValueError."""
    from mnemosyne_cli.lib.phase_card import PhaseCard, apply_event

    card = PhaseCard(
        project="[[mnemosyne]]", milestone=None, phase_number="38", status="ready", title="x",
    )
    with pytest.raises(ValueError):
        apply_event(card, "blocked")
    # With reason: succeeds
    result = apply_event(card, "blocked", reason="something")
    assert result.status == "blocked"
    assert result.blocked_on == "something"
