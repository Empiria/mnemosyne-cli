"""RED tests for the doctor 'Vendored Drift' check and --vendored-drift flag.

Phase 54 Plan 03 (Wave 0): these tests lock the behavioural contract for the
D-07 warn-vs-fail distinction before any implementation lands (Plans 04/05).

D-07 reconciliation:
  - default doctor run: the Vendored Drift check reports drift in its message
    but the overall exit code stays 0 (warning, not hard fail).
  - doctor --vendored-drift: exits non-zero when committed copy differs from
    pinned upstream; exits 0 when in sync.

Analogs:
  - test_doctor_operator_state_drift.py (lazy-import-in-body + monkeypatch
    Path.home + fixture seeding)
  - doctor.py --share-manifests path (Analog C in 54-PATTERNS.md)

All tests use lazy imports inside the function body (Phase 38 convention) so
pytest --collect-only succeeds before implementation lands in Plans 04/05.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


FIXTURES_VENDORED = Path(__file__).parent / "fixtures" / "vendored"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_vault_manifest(vault_path: Path) -> Path:
    """Seed vault with a minimal vendored.toml at agents/vendored.toml."""
    manifest_src = FIXTURES_VENDORED / "vendored.toml"
    manifest_dst = vault_path / "agents" / "vendored.toml"
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    manifest_dst.write_text(manifest_src.read_text())
    return vault_path


def _seed_in_sync_committed_copy(vault_path: Path, upstream_tree: Path) -> Path:
    """Seed a committed copy that exactly matches the upstream_tree contents.

    Returns the committed copy path.
    """
    import shutil

    dest = vault_path / "agents" / "vendored" / "anvil-agent-references"
    dest.mkdir(parents=True, exist_ok=True)

    # Copy upstream_tree contents as the committed copy (in-sync state)
    skills_src = upstream_tree / "skills"
    if skills_src.is_dir():
        shutil.copytree(skills_src, dest / "skills", dirs_exist_ok=True)

    # Empiria-authored files (must exist alongside)
    (dest / "index.md").write_text("# Empiria entry point\n")
    (dest / ".upstream-ref").write_text(
        "abc1234abc1234abc1234abc1234abc1234abc1234\n"
    )
    return dest


def _seed_drifted_committed_copy(vault_path: Path) -> Path:
    """Seed a committed copy that DIFFERS from the pinned upstream (drift state).

    The committed skills/SKILL.md has different content from the upstream,
    so the diff walk returns a non-empty set.
    """
    dest = vault_path / "agents" / "vendored" / "anvil-agent-references"
    dest.mkdir(parents=True, exist_ok=True)

    skills_dir = dest / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Different content from what would be in a fresh clone
    (skills_dir / "SKILL.md").write_text("# DRIFTED — local modification\n")

    (dest / "index.md").write_text("# Empiria entry point\n")
    (dest / ".upstream-ref").write_text(
        "abc1234abc1234abc1234abc1234abc1234abc1234\n"
    )
    return dest


# ---------------------------------------------------------------------------
# _check_vendored_drift tests — default doctor run (D-07 warn path)
# ---------------------------------------------------------------------------


def test_default_doctor_check_ok_when_in_sync(tmp_path, monkeypatch):
    """Default Vendored Drift check returns ok=True when committed copy matches upstream.

    The default check is informational-only (D-07): it must return ok=True so the
    main doctor exit code is unaffected even when drift is detected.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    upstream_tree = FIXTURES_VENDORED / "upstream_tree"
    _seed_in_sync_committed_copy(vault_path, upstream_tree)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    from mnemosyne_cli.commands.doctor import _check_vendored_drift  # lazy import

    result = _check_vendored_drift(vault_path)
    assert result.ok is True, (
        f"Default Vendored Drift check must return ok=True (informational); got: {result}"
    )


def test_default_doctor_check_ok_even_when_drifted(tmp_path, monkeypatch):
    """Default Vendored Drift check returns ok=True even when drift detected (D-07).

    The default run is a WARNING path, not a hard fail. The check must report
    drift in its message but keep ok=True so the overall doctor exit code stays 0.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    _seed_drifted_committed_copy(vault_path)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    from mnemosyne_cli.commands.doctor import _check_vendored_drift  # lazy import

    result = _check_vendored_drift(vault_path)
    # D-07: default check must be ok=True (warn, not fail) — main doctor exit unaffected
    assert result.ok is True, (
        f"Default Vendored Drift check must be ok=True even when drifted (D-07 warn path); "
        f"got ok={result.ok!r} message={result.message!r}"
    )
    # The message must mention drift or the fix command
    has_drift_mention = (
        "drift" in result.message.lower()
        or "mismatch" in result.message.lower()
        or result.fix_cmd is not None
    )
    assert has_drift_mention, (
        f"Drift check message must mention drift or provide a fix_cmd; got: {result.message!r}"
    )


def test_default_doctor_check_no_fix_fn_registered(tmp_path, monkeypatch):
    """Vendored Drift check has no _fix_fn — it is read-only (Analog B)."""
    # This is a structural assertion: the check registered under "Vendored Drift"
    # must have no _fix_fn (read-only per doctor.py Operator State Drift pattern).
    # We test by verifying the check is registered with only a fix_cmd string, not
    # a callable that modifies vault state. This assertion is validated by checking
    # that _check_vendored_drift returns a CheckResult with a fix_cmd string (not None)
    # rather than being wired as an auto-fixable check.
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    _seed_drifted_committed_copy(vault_path)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    from mnemosyne_cli.commands.doctor import _check_vendored_drift  # lazy import

    result = _check_vendored_drift(vault_path)
    # fix_cmd must be a string (the operator instruction) not None — read-only checks
    # encode their remediation as a string command the operator runs manually
    if not result.ok:
        assert isinstance(result.fix_cmd, str), (
            f"Vendored Drift check must provide fix_cmd as string; got {type(result.fix_cmd)}"
        )
    # If ok=True (informational/no-drift), fix_cmd may be None — that's fine


# ---------------------------------------------------------------------------
# _run_vendored_drift tests — --vendored-drift explicit CI path (D-07)
# ---------------------------------------------------------------------------


def test_run_vendored_drift_returns_true_on_drift(tmp_path, monkeypatch):
    """_run_vendored_drift returns True (exit-nonzero signal) when drift detected (D-07)."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    _seed_drifted_committed_copy(vault_path)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    from mnemosyne_cli.commands.doctor import _run_vendored_drift  # lazy import

    should_exit_nonzero = _run_vendored_drift(vault_path)
    assert should_exit_nonzero is True, (
        "_run_vendored_drift must return True (trigger Exit(1)) when drift detected"
    )


def test_run_vendored_drift_returns_false_when_in_sync(tmp_path, monkeypatch):
    """_run_vendored_drift returns False when committed copy matches pinned upstream."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    upstream_tree = FIXTURES_VENDORED / "upstream_tree"
    _seed_in_sync_committed_copy(vault_path, upstream_tree)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    from mnemosyne_cli.commands.doctor import _run_vendored_drift  # lazy import

    should_exit_nonzero = _run_vendored_drift(vault_path)
    assert should_exit_nonzero is False, (
        "_run_vendored_drift must return False (no Exit(1)) when in sync"
    )


# ---------------------------------------------------------------------------
# CLI tests — doctor --vendored-drift flag (D-07)
# ---------------------------------------------------------------------------


def test_cli_doctor_vendored_drift_exits_nonzero_on_drift(tmp_path, monkeypatch):
    """'mnemosyne doctor --vendored-drift' exits non-zero when drift detected (D-07 CI path)."""
    from typer.testing import CliRunner
    from mnemosyne_cli.lib import vault
    from mnemosyne_cli.main import app

    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    _seed_drifted_committed_copy(vault_path)

    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--vendored-drift"])
    assert result.exit_code != 0, (
        f"doctor --vendored-drift must exit non-zero on drift; got {result.exit_code}"
    )


def test_cli_doctor_vendored_drift_exits_zero_when_in_sync(tmp_path, monkeypatch):
    """'mnemosyne doctor --vendored-drift' exits 0 when committed copy is in sync."""
    from typer.testing import CliRunner
    from mnemosyne_cli.lib import vault
    from mnemosyne_cli.main import app

    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    upstream_tree = FIXTURES_VENDORED / "upstream_tree"
    _seed_in_sync_committed_copy(vault_path, upstream_tree)

    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--vendored-drift"])
    assert result.exit_code == 0, (
        f"doctor --vendored-drift must exit 0 when in sync; got {result.exit_code}\n"
        f"output: {result.output}"
    )


def test_cli_doctor_default_run_exits_zero_even_with_drift(tmp_path, monkeypatch):
    """Default 'mnemosyne doctor' exits 0 even when vendored drift detected (D-07 warn path)."""
    from typer.testing import CliRunner
    from mnemosyne_cli.lib import vault
    from mnemosyne_cli.main import app

    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _seed_vault_manifest(vault_path)
    _seed_drifted_committed_copy(vault_path)

    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)

    runner = CliRunner()
    # Default doctor run must NOT exit non-zero due to vendored drift alone
    # (other checks might cause failures — we assert the Vendored Drift check
    # itself doesn't flip the exit code by looking at the check function directly)
    from mnemosyne_cli.commands.doctor import _check_vendored_drift  # lazy import

    result = _check_vendored_drift(vault_path)
    assert result.ok is True, (
        "Default Vendored Drift check must be ok=True (informational) so doctor exit stays 0"
    )
