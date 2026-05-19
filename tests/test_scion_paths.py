"""Tests for lib/scion_paths.py grove enumeration helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemosyne_cli.lib import scion_paths


def test_iter_grove_settings_skips_default_prefixes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    base = tmp_path / ".scion" / "grove-configs"
    for name in ("auto-foo", "test-bar", "infinite-worlds__abc",
                 "mnemosyne__def", "cleanup-x"):
        (base / name / ".scion").mkdir(parents=True)
        (base / name / ".scion" / "settings.yaml").write_text("default_template: x\n")

    found = sorted(p.parent.parent.name for p in scion_paths.iter_grove_settings_paths())
    assert found == ["infinite-worlds__abc", "mnemosyne__def"]


def test_iter_grove_settings_empty_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # No ~/.scion/grove-configs/ exists
    assert list(scion_paths.iter_grove_settings_paths()) == []


def test_iter_grove_settings_all_when_no_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    base = tmp_path / ".scion" / "grove-configs"
    for name in ("auto-foo", "real-project"):
        (base / name / ".scion").mkdir(parents=True)
        (base / name / ".scion" / "settings.yaml").write_text("k: v\n")

    found = sorted(p.parent.parent.name for p in scion_paths.iter_grove_settings_paths(skip_prefixes=()))
    assert found == ["auto-foo", "real-project"]
