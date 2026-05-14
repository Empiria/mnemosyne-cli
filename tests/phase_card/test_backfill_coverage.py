"""Coverage tests for ``mnemosyne phase backfill`` (ACC-37-03, ACC-37-09).

Asserts that backfill writes exactly one ``phase.md`` per existing phase
directory, preserves user-edited body content, honours ``--project``
scoping, rejects path-traversal input, and emits ``status: complete`` +
explanatory ``summary:`` for CLOSED entries.
"""

from __future__ import annotations

import frontmatter
from typer.testing import CliRunner

# Invoke via the top-level CLI so the "phase backfill" subcommand path
# is exercised end-to-end (Typer collapses a single-command sub-Typer to
# the leaf command, which would otherwise reject the "backfill" arg).
from mnemosyne_cli.main import app as cli_app


def _run(args):
    return CliRunner().invoke(cli_app, ["phase", *args])


def _phase_dirs(vault_root):
    """Return every phase dir (one per ``gsd-planning/phases/<dir>/``)."""
    return [
        d
        for d in (vault_root / "projects").glob("*/*/gsd-planning/phases/*")
        if d.is_dir()
    ]


def _phase_mds(vault_root):
    return list((vault_root / "projects").rglob("gsd-planning/phases/*/phase.md"))


def test_backfill_writes_one_phase_md_per_dir(synthetic_vault, monkeypatch):
    """ACC-37-03 — phase.md count == phase dir count after backfill."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill"])
    assert result.exit_code == 0, result.output

    phase_dirs = _phase_dirs(synthetic_vault)
    phase_mds = _phase_mds(synthetic_vault)
    assert phase_dirs, "synthetic_vault fixture produced zero phase dirs"
    assert len(phase_mds) == len(phase_dirs), (
        f"Coverage gap: {len(phase_dirs)} phase dirs, "
        f"{len(phase_mds)} phase.md files written"
    )


def test_backfill_phase_md_is_valid_frontmatter(synthetic_vault, monkeypatch):
    """Every written phase.md parses cleanly with python-frontmatter."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    _run(["backfill"])
    for phase_md in _phase_mds(synthetic_vault):
        post = frontmatter.load(phase_md)
        # Required schema keys present (D-16 reference schema)
        for required in ("tags", "project", "status", "title", "phase_number"):
            assert required in post.metadata, (
                f"{phase_md}: missing required field {required!r}"
            )
        assert post["tags"] == ["phase"]


def test_user_body_preserved(phase_md_with_user_body, synthetic_vault, monkeypatch):
    """Re-runs preserve user-edited body content verbatim (RESEARCH §Pattern 2)."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    _run(["backfill"])
    post = frontmatter.load(phase_md_with_user_body)
    assert "Manual notes" in post.content
    assert "edited by a human" in post.content
    # Frontmatter is still derived correctly — status cascades to complete
    # because 27-complete-via-summaries has every PLAN matched by a SUMMARY.
    assert post["status"] == "complete"


def test_project_scope_flag(synthetic_vault, monkeypatch):
    """--project empiria/mnemosyne writes only under that project."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill", "--project", "empiria/mnemosyne"])
    assert result.exit_code == 0, result.output

    mnemosyne_files = list(
        (synthetic_vault / "projects/empiria/mnemosyne/gsd-planning/phases").rglob(
            "phase.md"
        )
    )
    friendly_fox_files = list(
        (synthetic_vault / "projects/empiria/friendly-fox/gsd-planning/phases").rglob(
            "phase.md"
        )
    )
    assert mnemosyne_files, "Scoped --project produced no files"
    assert friendly_fox_files == [], (
        "--project leaked into other project's phase dirs"
    )


def test_path_traversal_rejected(synthetic_vault, monkeypatch):
    """T-37-01 — ``--project ../../etc/passwd`` exits non-zero."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill", "--project", "../../etc/passwd"])
    assert result.exit_code != 0


def test_path_traversal_double_dot_rejected(synthetic_vault, monkeypatch):
    """Even slug-shaped traversal is rejected."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill", "--project", "..hidden/secret"])
    assert result.exit_code != 0


def test_closed_phase_gets_complete_with_explanation(
    closed_phase_dir, synthetic_vault, monkeypatch
):
    """ACC-37-09 — CLOSED phases → status: complete + summary mentions CLOSED."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill"])
    assert result.exit_code == 0, result.output

    phase_md = closed_phase_dir / "phase.md"
    assert phase_md.is_file(), f"backfill did not write {phase_md}"
    post = frontmatter.load(phase_md)
    assert post["status"] == "complete"
    summary_text = post.get("summary") or ""
    assert "CLOSED" in summary_text, (
        f"CLOSED phase summary should mention CLOSED — got {summary_text!r}"
    )


def test_multivault_can_read_filter(multivault_config, monkeypatch):
    """T-37-04 — vault B is silently skipped when can_read(A, B) is False.

    Setup: two vaults registered, no [[vault_rules]] → ``can_read`` is False
    for the secondary. Backfill must NOT iterate vault B.
    """
    vault_a, vault_b = multivault_config
    result = _run(["backfill"])
    assert result.exit_code == 0, result.output

    # Vault A should have phase.md files
    a_files = list((vault_a / "projects").rglob("gsd-planning/phases/*/phase.md"))
    assert a_files, "Active vault was not backfilled"

    # Vault B must have zero phase.md files — can_read denied
    b_files = list((vault_b / "projects").rglob("gsd-planning/phases/*/phase.md"))
    assert b_files == [], (
        f"can_read=False vault should be skipped; got {b_files}"
    )
