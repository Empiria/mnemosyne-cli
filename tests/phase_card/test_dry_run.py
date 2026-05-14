"""Dry-run tests for ``mnemosyne phase backfill`` (ACC-37-07).

``--dry-run`` must print the proposed plan AND write zero files. After a
real backfill, a subsequent ``--dry-run`` must report every entry as
``unchanged``.
"""

from __future__ import annotations

from typer.testing import CliRunner

from mnemosyne_cli.main import app as cli_app


def _run(args):
    return CliRunner().invoke(cli_app, ["phase", *args])


def test_dry_run_writes_no_files(synthetic_vault, monkeypatch):
    """ACC-37-07 — --dry-run prints a plan AND writes nothing on disk."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry" in result.output.lower(), (
        f"--dry-run output should mention 'dry'; got:\n{result.output}"
    )

    phase_mds = list(
        (synthetic_vault / "projects").rglob("gsd-planning/phases/*/phase.md")
    )
    assert phase_mds == [], (
        f"--dry-run wrote files: {phase_mds!r}"
    )


def test_dry_run_after_real_run_reports_unchanged(synthetic_vault, monkeypatch):
    """After a real backfill, --dry-run shows every entry as unchanged."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    real = _run(["backfill"])
    assert real.exit_code == 0, real.output

    result = _run(["backfill", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output.lower(), (
        f"Second-pass --dry-run should report 'unchanged'; got:\n{result.output}"
    )


def test_dry_run_prints_a_table(synthetic_vault, monkeypatch):
    """The Rich table header/labels surface in --dry-run output."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    result = _run(["backfill", "--dry-run"])
    assert result.exit_code == 0
    # Look for at least one column header from the Rich table — the exact
    # rendering depends on terminal width, but "Phase" / "Project" / "Status"
    # / "Action" are stable strings.
    out_lower = result.output.lower()
    assert any(
        label in out_lower for label in ("phase", "project", "status", "action")
    ), f"No Rich table headers in dry-run output:\n{result.output}"
