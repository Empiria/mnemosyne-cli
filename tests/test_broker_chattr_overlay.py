"""RED tests for SBR-3.1 chattr overlay helpers in lib/broker.py.

Plan 33.3-03 (Wave 2) implements:
  - writable(paths) ctxmgr  — chattr -i on enter, chattr +i on exit
  - get_protected_paths()   — lazy accessor returning the 2 harness-config paths

These tests MUST fail RED with ImportError / AttributeError on the missing
symbols (not on setup error). Lazy imports inside test bodies follow Phase 38
P02 lazy-import convention.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "harness_config_seed"


def _seed_two_protected_files(home: Path) -> tuple[Path, Path]:
    """Create the two harness-config files writable() / get_protected_paths() target."""
    base = home / ".scion" / "harness-configs" / "claude"
    (base / "home").mkdir(parents=True, exist_ok=True)
    claude_json = base / "home" / ".claude.json"
    config_yaml = base / "config.yaml"
    claude_json.write_text((FIXTURES / ".claude.json").read_text())
    config_yaml.write_text((FIXTURES / "config.yaml").read_text())
    return claude_json, config_yaml


def test_writable_toggles_chattr_around_block(tmp_path, monkeypatch):
    """writable() must call `chattr -i` on enter and `chattr +i` on exit for each existing path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    claude_json, config_yaml = _seed_two_protected_files(fake_home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    from mnemosyne_cli.lib.broker import writable

    with writable([claude_json, config_yaml]):
        pass

    # Expect chattr -i for each path on enter, then chattr +i for each path on exit
    # (4 subprocess.run invocations total).
    flat = [tuple(c) for c in calls]
    assert any(c[0] == "chattr" and c[1] == "-i" and str(claude_json) in c for c in flat), \
        f"expected chattr -i on {claude_json}; calls were {flat}"
    assert any(c[0] == "chattr" and c[1] == "-i" and str(config_yaml) in c for c in flat), \
        f"expected chattr -i on {config_yaml}; calls were {flat}"
    assert any(c[0] == "chattr" and c[1] == "+i" and str(claude_json) in c for c in flat), \
        f"expected chattr +i on {claude_json}; calls were {flat}"
    assert any(c[0] == "chattr" and c[1] == "+i" and str(config_yaml) in c for c in flat), \
        f"expected chattr +i on {config_yaml}; calls were {flat}"


def test_writable_noop_for_missing_paths(tmp_path, monkeypatch):
    """writable() must NOT call subprocess.run for paths that don't exist."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    from mnemosyne_cli.lib.broker import writable

    missing = fake_home / ".scion" / "absent" / "x.json"
    with writable([missing]):
        pass

    assert calls == [], f"expected no subprocess calls for missing path; got {calls}"


def test_writable_reapplies_plus_i_even_on_exception(tmp_path, monkeypatch):
    """writable() must re-apply chattr +i in try/finally — block raising must not skip cleanup."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    claude_json, _ = _seed_two_protected_files(fake_home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    from mnemosyne_cli.lib.broker import writable

    with pytest.raises(RuntimeError, match="boom"):
        with writable([claude_json]):
            raise RuntimeError("boom")

    flat = [tuple(c) for c in calls]
    assert any(c[0] == "chattr" and c[1] == "+i" and str(claude_json) in c for c in flat), \
        f"expected chattr +i to re-apply on cleanup; calls were {flat}"


def test_get_protected_paths_resolves_under_patched_home(tmp_path, monkeypatch):
    """get_protected_paths() must resolve lazily so monkeypatched Path.home works.

    Per Plan 03 Task 03.2's PROTECTED_PATHS lazy-evaluation pattern — DO NOT
    import a module-level PROTECTED_PATHS list; that would resolve against the
    real host at import time, breaking the monkeypatch.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from mnemosyne_cli.lib.broker import get_protected_paths
    paths = get_protected_paths()
    assert len(paths) == 2
    assert all(str(p).startswith(str(tmp_path)) for p in paths)
    # Two specific harness-config files:
    names = sorted(p.name for p in paths)
    assert names == [".claude.json", "config.yaml"]
