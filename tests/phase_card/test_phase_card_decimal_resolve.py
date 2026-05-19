"""Regression coverage for resolve_phase_dir against dot-form decimal phase dirs.

Phase 44 backstory: the Phase 38 JS shim silent-no-ops every decimal phase because
`resolve_phase_dir` only globbed for hyphen-form (`33-1-*`). Phase 44 fixed it to
try both forms. This file is the deterministic guard so the regression cannot return.

See Phase 44 CONTEXT.md §D-05/D-06/D-07/D-09.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _make_phase(vault: Path, project: str, phase_dirname: str) -> Path:
    """Create projects/<project>/gsd-planning/phases/<phase_dirname>/ under vault."""
    phase_dir = vault / "projects" / project / "gsd-planning" / "phases" / phase_dirname
    phase_dir.mkdir(parents=True, exist_ok=True)
    # project note (required by validate_project_slug if it checks file existence)
    project_note = vault / "projects" / project + ".md" if False else None  # placeholder
    # Actual project note: vault/projects/<org>/<code>.md
    org, code = project.split("/")
    note = vault / "projects" / org / f"{code}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    if not note.exists():
        note.write_text(
            "---\ntags: [project]\norganisation: \"[[Empiria]]\"\n---\n\n# " + code + "\n",
            encoding="utf-8",
        )
    return phase_dir


def test_resolve_phase_dir_dot_form_decimal(tmp_path):
    """Phase 44 D-05/D-06: dot-form `33.1-*` must resolve from phase_id '33.1'."""
    from mnemosyne_cli.lib.phase_card import resolve_phase_dir

    vault = tmp_path / "vault"
    vault.mkdir()
    _make_phase(vault, "empiria/mnemosyne", "33.1-scion-agent-bootstrap-regressions")

    result = resolve_phase_dir(vault, "33.1", project="empiria/mnemosyne")
    assert result is not None, (
        "Phase 44 regression: resolve_phase_dir must find dot-form decimal dirs"
    )
    assert result.name == "33.1-scion-agent-bootstrap-regressions"


def test_resolve_phase_dir_hyphen_form_decimal_still_works(tmp_path):
    """Phase 44 D-06: hyphen-form `195-02-*` must still resolve from phase_id '195.02'."""
    from mnemosyne_cli.lib.phase_card import resolve_phase_dir

    vault = tmp_path / "vault"
    vault.mkdir()
    _make_phase(vault, "empiria/friendly-fox", "195-02-fix-scroll-position-reset")

    result = resolve_phase_dir(vault, "195.02", project="empiria/friendly-fox")
    assert result is not None, (
        "Phase 44 must not regress hyphen-form decimal support (D-06 compat reality)"
    )
    assert result.name == "195-02-fix-scroll-position-reset"


def test_resolve_phase_dir_ambiguous_across_forms_returns_none(tmp_path):
    """Phase 44 D-06: ≥2 candidates across dot- and hyphen-form → None (silent skip)."""
    from mnemosyne_cli.lib.phase_card import resolve_phase_dir

    vault = tmp_path / "vault"
    vault.mkdir()
    _make_phase(vault, "empiria/mnemosyne", "33.1-dot-form")
    _make_phase(vault, "empiria/mnemosyne", "33-1-hyphen-form")

    result = resolve_phase_dir(vault, "33.1", project="empiria/mnemosyne")
    assert result is None, (
        f"Ambiguous (dot+hyphen) must return None per Phase 37 D-08, got {result!r}"
    )


def test_resolve_phase_dir_not_found_returns_none(tmp_path):
    """Empty phases/ dir → None."""
    from mnemosyne_cli.lib.phase_card import resolve_phase_dir

    vault = tmp_path / "vault"
    vault.mkdir()
    # Create the project structure but no phase dirs
    phases_dir = vault / "projects/empiria/mnemosyne/gsd-planning/phases"
    phases_dir.mkdir(parents=True)
    note = vault / "projects/empiria/mnemosyne.md"
    note.write_text(
        "---\ntags: [project]\n---\n\n# mnemosyne\n",
        encoding="utf-8",
    )

    result = resolve_phase_dir(vault, "999", project="empiria/mnemosyne")
    assert result is None


def test_phase_dir_prefix_re_accepts_dot_form():
    """Phase 44 D-07: _PHASE_DIR_PREFIX_RE must match both '33.1-foo' and '33-1-foo'."""
    from mnemosyne_cli.lib.phase_card import _PHASE_DIR_PREFIX_RE

    assert _PHASE_DIR_PREFIX_RE.match("33.1-scion-agent-bootstrap-regressions"), (
        "Phase 44 D-07: regex must accept dot-form decimal prefix"
    )
    assert _PHASE_DIR_PREFIX_RE.match("33-1-scion-agent-bootstrap-regressions"), (
        "Phase 44 D-07: hyphen-form must still match (no regression)"
    )
    assert _PHASE_DIR_PREFIX_RE.match("empiria-01-explore"), (
        "empiria-prefix must still match (D-06 carry-forward)"
    )
    assert _PHASE_DIR_PREFIX_RE.match("37-planned-empty"), (
        "Integer phase prefix must still match"
    )
