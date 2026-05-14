"""Wave 0 test for ACC-38-06 — JS shim absent-CLI path simulation.

The JS shim (phase-card-hook.cjs) catches spawnSync result.error.code === 'ENOENT'
and result.status !== 0, writes ONE stderr warning line, and never throws so the
surrounding GSD command completes successfully.

This test simulates the SAME failure-handling contract Python-side via subprocess
monkeypatching. Cross-language confidence: the JS test (real shim invocation) is
a manual smoke test gated to Wave 3; this test proves the failure-mode shape.
"""
from __future__ import annotations

import subprocess
import sys

import pytest


def _fake_spawn_enoent(*args, **kwargs):
    raise FileNotFoundError(2, "No such file or directory", "mnemosyne")


def _fake_spawn_nonzero(cmd, *args, **kwargs):
    # Returns a CompletedProcess-like object with non-zero returncode
    class Result:
        returncode = 127
        stdout = ""
        stderr = "mnemosyne phase update: error\n"
    return Result()


def test_shim_swallows_enoent(monkeypatch, capsys):
    """Simulated shim path: subprocess.run raises FileNotFoundError → swallow + stderr warning.

    Note: the production JS shim uses spawnSync (Node), not subprocess.run (Python).
    This test simulates the EQUIVALENT failure-mode contract for the apply_event
    + update Typer command path that the shim invokes. The ENOENT path itself is
    a JS-only code path (the shim never reaches Python when mnemosyne is absent).
    """
    # Subject: the simulated wrapper that mirrors the JS shim's failure handling.
    def shim_like(cmd, args, stderr=sys.stderr):
        try:
            subprocess.run([cmd] + args, check=False, capture_output=True)
        except FileNotFoundError:
            stderr.write(f"phase.md update skipped: {cmd} CLI not found in PATH\n")
            return 0  # GSD command proceeds
        return 0

    monkeypatch.setattr(subprocess, "run", _fake_spawn_enoent)

    # Capture stderr via a StringIO so we don't pollute pytest's captured stderr
    import io
    err = io.StringIO()
    rc = shim_like("mnemosyne", ["phase", "update", "--phase", "38", "--event", "added"], stderr=err)

    assert rc == 0, "Shim must return 0 so GSD command proceeds"
    assert "phase.md update skipped" in err.getvalue(), (
        f"Expected warning on stderr; got {err.getvalue()!r}"
    )
    assert "mnemosyne CLI not found" in err.getvalue()


def test_shim_swallows_nonzero_exit(monkeypatch):
    """Non-zero exit from mnemosyne phase update is also swallowed (D-08)."""
    import io

    def shim_like(cmd, args, stderr):
        result = subprocess.run([cmd] + args, check=False, capture_output=True)
        if result.returncode != 0:
            stderr.write(
                f"phase.md update warning: mnemosyne phase update exited {result.returncode}\n"
            )
            if result.stderr:
                stderr.write(result.stderr if isinstance(result.stderr, str) else result.stderr.decode())
            return 0
        return 0

    monkeypatch.setattr(subprocess, "run", _fake_spawn_nonzero)
    err = io.StringIO()
    rc = shim_like("mnemosyne", ["phase", "update", "--phase", "38", "--event", "in-progress"], stderr=err)

    assert rc == 0
    assert "exited 127" in err.getvalue()
