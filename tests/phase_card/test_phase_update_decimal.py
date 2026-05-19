"""End-to-end regression: `mnemosyne phase update` against a dot-form decimal phase.

Phase 44 D-09 second test fixture. Proves the JS shim → Python self-heal path produces
a valid phase.md for a dot-form decimal phase like 33.1. Replaces the roadmap's
"insert throwaway test phase" loop with a deterministic regression guard.

Depends on the Plan 01 resolver fix — without it this test would fail with the
"phase.md update skipped: cannot resolve phase dir" silent no-op.

See Phase 44 CONTEXT.md §D-09.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
from typer.testing import CliRunner


runner = CliRunner()


_VALID_STATUSES = {
    "planned", "ready", "in-progress", "review", "rework", "complete", "blocked",
}


def _setup_vault_with_decimal_phase(tmp_path: Path) -> Path:
    """Minimal vault with a 33.1-bar dot-form decimal phase dir, no phase.md."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # Project note
    (vault / "projects" / "empiria").mkdir(parents=True)
    (vault / "projects" / "empiria" / "mnemosyne.md").write_text(
        '---\ntags: [project]\norganisation: "[[Empiria]]"\n---\n\n# mnemosyne\n',
        encoding="utf-8",
    )
    # STATE.md — minimal cascade input
    state_dir = vault / "projects" / "empiria" / "mnemosyne" / "gsd-planning"
    state_dir.mkdir(parents=True)
    (state_dir / "STATE.md").write_text(
        '---\nmilestone: v4.5.2\ncurrent_phase: "33.1"\nstatus: executing\n---\n\n# State\n',
        encoding="utf-8",
    )
    # ROADMAP.md — minimal
    (state_dir / "ROADMAP.md").write_text(
        "# Roadmap\n\n### Phase 33.1: bar\n\n**Goal:** test\n",
        encoding="utf-8",
    )
    # The phase dir itself — dot-form, NO phase.md
    phase_dir = state_dir / "phases" / "33.1-bar"
    phase_dir.mkdir(parents=True)
    # Add a PLAN.md so the cascade can derive plan: wikilink
    (phase_dir / "33.1-01-PLAN.md").write_text("# Plan 33.1-01\n", encoding="utf-8")
    return vault


def test_phase_update_decimal_writes_phase_md(tmp_path, monkeypatch):
    """`phase update --phase 33.1 --event added` self-heals phase.md for dot-form decimal."""
    from mnemosyne_cli.commands.phase import app as phase_app

    vault = _setup_vault_with_decimal_phase(tmp_path)
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(vault))

    phase_md = (
        vault
        / "projects/empiria/mnemosyne/gsd-planning/phases/33.1-bar/phase.md"
    )
    assert not phase_md.exists(), "Precondition: phase.md absent before update"

    result = runner.invoke(
        phase_app,
        ["update", "--phase", "33.1", "--event", "added",
         "--project", "empiria/mnemosyne"],
    )
    assert result.exit_code == 0, (
        f"phase update exited {result.exit_code}; output:\n{result.output}\nstderr:\n"
        + (result.stderr if hasattr(result, "stderr") else "(no stderr)")
    )

    assert phase_md.exists(), (
        "Phase 44 regression: dot-form 33.1 phase dir must produce phase.md via self-heal. "
        "If this fails with file absent, Plan 01 resolver fix did not land or "
        "synthesise the resolve_phase_dir('33.1', ...) match correctly."
    )

    post = frontmatter.load(phase_md)
    # Phase 37 D-15: phase_number is a STRING preserving the dot
    assert post["phase_number"] == "33.1", (
        f"phase_number must be string '33.1' (dot preserved), got "
        f"{post['phase_number']!r} (type {type(post['phase_number']).__name__})"
    )
    assert isinstance(post["phase_number"], str), (
        f"phase_number must be str type, got {type(post['phase_number']).__name__}"
    )
    # Required schema fields
    assert post["tags"] == ["phase"]
    assert post["project"] == "[[mnemosyne]]"
    assert post["status"] in _VALID_STATUSES, (
        f"status must be a valid Phase 37 enum value, got {post['status']!r}"
    )


def test_phase_update_decimal_idempotent(tmp_path, monkeypatch):
    """Re-invoking phase update on the same dot-form decimal does not corrupt phase.md."""
    from mnemosyne_cli.commands.phase import app as phase_app

    vault = _setup_vault_with_decimal_phase(tmp_path)
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(vault))

    phase_md = (
        vault
        / "projects/empiria/mnemosyne/gsd-planning/phases/33.1-bar/phase.md"
    )

    # First invocation — self-heal
    r1 = runner.invoke(
        phase_app,
        ["update", "--phase", "33.1", "--event", "added",
         "--project", "empiria/mnemosyne"],
    )
    assert r1.exit_code == 0, r1.output
    assert phase_md.exists()
    post1 = frontmatter.load(phase_md)

    # Second invocation — should be a no-op or apply event idempotently
    r2 = runner.invoke(
        phase_app,
        ["update", "--phase", "33.1", "--event", "added",
         "--project", "empiria/mnemosyne"],
    )
    assert r2.exit_code == 0, r2.output
    post2 = frontmatter.load(phase_md)

    # phase_number / project / tags must be stable across re-runs
    assert post2["phase_number"] == "33.1" == post1["phase_number"]
    assert post2["tags"] == post1["tags"]
    assert post2["project"] == post1["project"]
