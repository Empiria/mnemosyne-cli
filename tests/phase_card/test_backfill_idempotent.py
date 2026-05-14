"""Idempotency tests for ``mnemosyne phase backfill`` (ACC-37-06).

Running backfill twice on the same vault must produce zero file mutations
on the second run — every entry reports ``unchanged`` and on-disk content
hashes are identical.
"""

from __future__ import annotations

import hashlib

from typer.testing import CliRunner

from mnemosyne_cli.main import app as cli_app


def _run(args):
    return CliRunner().invoke(cli_app, ["phase", *args])


def _hash_tree(vault_root):
    """Return ``{path: sha256-hex}`` for every phase.md under the vault."""
    return {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (vault_root / "projects").rglob(
            "gsd-planning/phases/*/phase.md"
        )
    }


def test_second_run_makes_no_changes(synthetic_vault, monkeypatch):
    """ACC-37-06 — re-running backfill leaves every file byte-identical."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))

    result1 = _run(["backfill"])
    assert result1.exit_code == 0, result1.output

    before = _hash_tree(synthetic_vault)
    assert before, "First backfill produced no phase.md files"

    result2 = _run(["backfill"])
    assert result2.exit_code == 0, result2.output
    assert "unchanged" in result2.output.lower(), (
        f"Second-run output should report 'unchanged'; got:\n{result2.output}"
    )

    after = _hash_tree(synthetic_vault)
    assert before == after, (
        "Second backfill mutated files — idempotency broken"
    )


def test_second_run_emits_only_unchanged_actions(synthetic_vault, monkeypatch):
    """Second-run output should NOT mention ``created`` or ``updated`` actions."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    _run(["backfill"])  # populate
    result2 = _run(["backfill"])
    assert result2.exit_code == 0
    lower = result2.output.lower()
    # We expect zero created/updated tallies on the second pass.
    assert "created=" not in lower, (
        "Second run reports created= entries — non-idempotent"
    )
    assert "updated=" not in lower, (
        "Second run reports updated= entries — non-idempotent"
    )
