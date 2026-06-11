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
    # One .claude.json + config.yaml pair per managed harness-config
    # (claude + claude-anvil).
    assert len(paths) == 4
    assert all(str(p).startswith(str(tmp_path)) for p in paths)
    names = sorted(p.name for p in paths)
    assert names == [".claude.json", ".claude.json", "config.yaml", "config.yaml"]
    parents = {p.parent.parts[-2] if p.name == ".claude.json" else p.parent.name for p in paths}
    assert parents == {"claude", "claude-anvil"}


def test_atomic_write_no_orphan_on_failed_replace(tmp_path, monkeypatch):
    """_atomic_write must leave no .mnemosyne-tmp-* orphan when os.replace fails.

    Success path: target written, no orphan tempfile in parent dir.
    Failure path: monkeypatched os.replace raises OSError — exception propagated
    AND no .mnemosyne-tmp-* file remains in the parent directory.
    """
    from mnemosyne_cli.lib import broker

    # --- Success path ---
    target = tmp_path / "target.yaml"
    broker._atomic_write(target, "content")
    assert target.read_text() == "content"
    orphans = list(tmp_path.glob(".mnemosyne-tmp-*"))
    assert orphans == [], f"success path left orphan(s): {orphans}"

    # --- Failure path: os.replace raises ---
    target2 = tmp_path / "target2.yaml"
    monkeypatch.setattr("mnemosyne_cli.lib.broker.os.replace", lambda src, dst: (_ for _ in ()).throw(OSError("simulated replace failure")))
    with pytest.raises(OSError, match="simulated replace failure"):
        broker._atomic_write(target2, "will fail")
    orphans_after = list(tmp_path.glob(".mnemosyne-tmp-*"))
    assert orphans_after == [], f"failure path left orphan(s): {orphans_after}"


def _seed_vault_template_root(root: Path, with_anvil: bool = True) -> Path:
    """Build a fake agents/scion-template/ seed root; return the claude seed dir."""
    claude_seed = root / "claude-harness-config"
    claude_seed.mkdir(parents=True)
    (claude_seed / ".claude.json").write_text((FIXTURES / ".claude.json").read_text())
    (claude_seed / "config.yaml").write_text((FIXTURES / "config.yaml").read_text())
    if with_anvil:
        anvil_seed = root / "claude-anvil-harness-config"
        (anvil_seed / "home" / ".claude").mkdir(parents=True)
        (anvil_seed / "config.yaml").write_text(
            "harness: claude\nimage: ghcr.io/empiria/empiria-claude-anvil:latest\nuser: scion\n"
        )
        (anvil_seed / "home" / ".bashrc").write_text("# scion agent bashrc\n")
        (anvil_seed / "home" / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
    return claude_seed


def test_overlay_seeds_claude_anvil_variant(tmp_path, monkeypatch):
    """With the claude-anvil seed dir present as a sibling, the overlay writes the
    variant's config.yaml AND inherits .claude.json from the claude seed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    claude_seed = _seed_vault_template_root(tmp_path / "scion-template")

    from mnemosyne_cli.lib.broker import apply_harness_config_overlay

    result = apply_harness_config_overlay(seed_dir=claude_seed)

    base = fake_home / ".scion" / "harness-configs"
    anvil_yaml = base / "claude-anvil" / "config.yaml"
    anvil_json = base / "claude-anvil" / "home" / ".claude.json"
    assert anvil_yaml.is_file(), f"missing {anvil_yaml}; written={result.written}"
    assert "empiria-claude-anvil" in anvil_yaml.read_text()
    assert anvil_json.is_file(), f"missing {anvil_json}; written={result.written}"
    assert anvil_json.read_text() == (claude_seed / ".claude.json").read_text(), \
        "variant .claude.json must inherit the claude seed's copy"
    # home/ tree (bashrc + lifecycle-hook settings) deployed alongside
    assert (base / "claude-anvil" / "home" / ".bashrc").is_file()
    assert (base / "claude-anvil" / "home" / ".claude" / "settings.json").is_file()
    assert (base / "claude" / "config.yaml").is_file()
    assert len(result.written) == 6

    # Idempotent: second run writes nothing.
    second = apply_harness_config_overlay(seed_dir=claude_seed)
    assert not second.written, f"expected idempotent no-op; wrote {second.written}"


def test_overlay_skips_anvil_when_variant_seed_absent(tmp_path, monkeypatch):
    """Older vault checkouts without the claude-anvil seed must not break the
    overlay — claude is seeded, the variant is silently skipped."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    claude_seed = _seed_vault_template_root(tmp_path / "scion-template", with_anvil=False)

    from mnemosyne_cli.lib.broker import apply_harness_config_overlay

    result = apply_harness_config_overlay(seed_dir=claude_seed)

    base = fake_home / ".scion" / "harness-configs"
    assert (base / "claude" / "config.yaml").is_file()
    assert not (base / "claude-anvil").exists()
    assert len(result.written) == 2
