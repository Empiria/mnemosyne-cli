"""RED tests for SBR-2.2 onboarding-drift detection in mnemosyne doctor.

Plan 33.2-04 implements `_check_claude_onboarding_drift()` in commands/doctor.py.
"""

from __future__ import annotations

import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "claude_config"


def _seed_template(home: Path, version: str = "2.1.144") -> Path:
    path = home / ".scion" / "harness-configs" / "claude" / "home" / ".claude.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lastOnboardingVersion": version, "hasCompletedOnboarding": True}))
    return path


def _seed_host(home: Path, version: str = "2.1.144") -> Path:
    path = home / ".claude.json"
    path.write_text(json.dumps({"lastOnboardingVersion": version}))
    return path


def test_version_match(tmp_path, monkeypatch):
    from mnemosyne_cli.commands.doctor import _check_claude_onboarding_drift

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_template(fake_home, "2.1.144")
    _seed_host(fake_home, "2.1.144")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = _check_claude_onboarding_drift()
    assert result.ok is True
    assert "2.1.144" in result.message


def test_template_older(tmp_path, monkeypatch):
    from mnemosyne_cli.commands.doctor import _check_claude_onboarding_drift

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_template(fake_home, "2.0.76")
    _seed_host(fake_home, "2.1.144")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = _check_claude_onboarding_drift()
    assert result.ok is False
    assert "2.0.76" in result.message
    assert "2.1.144" in result.message
    assert result.fix_cmd is not None
    assert "lastOnboardingVersion" in result.fix_cmd


def test_template_newer(tmp_path, monkeypatch):
    from mnemosyne_cli.commands.doctor import _check_claude_onboarding_drift

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_template(fake_home, "2.1.150")
    _seed_host(fake_home, "2.1.144")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = _check_claude_onboarding_drift()
    assert result.ok is True


def test_host_missing(tmp_path, monkeypatch):
    from mnemosyne_cli.commands.doctor import _check_claude_onboarding_drift

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_template(fake_home, "2.1.144")
    # No ~/.claude.json
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = _check_claude_onboarding_drift()
    assert result.ok is True
    assert "Host" in result.message and "not run" in result.message.lower()


def test_template_missing(tmp_path, monkeypatch):
    from mnemosyne_cli.commands.doctor import _check_claude_onboarding_drift

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # No template; host may or may not exist
    _seed_host(fake_home, "2.1.144")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = _check_claude_onboarding_drift()
    assert result.ok is True
    assert "template" in result.message.lower()
