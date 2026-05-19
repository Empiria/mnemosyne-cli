"""RED tests for SBR-3.7 a/b/c operator-state drift doctor checks.

Plan 33.3-02 (Wave 1) implements in `mnemosyne_cli.commands.doctor`:
  - _check_user_settings_auth_type    (SBR-3.7 a)  — ~/.scion/settings.yaml auth_selected_type
  - _check_grove_settings              (SBR-3.7 b)  — every grove's settings.yaml
  - _check_user_profile_env_no_overrides (SBR-3.7 c) — profiles.*.env MNEMOSYNE_VAULT override

Each test uses lazy imports inside the function body (Phase 38 P02 convention)
so `pytest --collect-only` succeeds even before implementation lands.
"""

from __future__ import annotations

from pathlib import Path


FIXTURES_SETTINGS = Path(__file__).parent / "fixtures" / "settings_yaml"
FIXTURES_GROVE = Path(__file__).parent / "fixtures" / "grove_settings"


def _seed_user_settings(home: Path, source: Path | None) -> Path | None:
    if source is None:
        return None
    path = home / ".scion" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.read_text())
    return path


def _seed_grove(home: Path, name: str, source: Path) -> Path:
    path = home / ".scion" / "grove-configs" / name / ".scion" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.read_text())
    return path


# ---------------------------------------------------------------------------
# SBR-3.7 (a) — _check_user_settings_auth_type
# ---------------------------------------------------------------------------


def test_user_settings_auth_pass(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_user_settings_auth_type
    result = _check_user_settings_auth_type()
    assert result.ok is True


def test_user_settings_auth_fail_drifted(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "drifted_auth.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_user_settings_auth_type
    result = _check_user_settings_auth_type()
    assert result.ok is False
    assert "auth-file" in result.message
    assert result.fix_cmd is not None
    assert "apply-empiria-defaults" in result.fix_cmd


def test_user_settings_auth_warn_missing(tmp_path, monkeypatch):
    """Per RESEARCH §Pattern 1 — file may be absent in greenfield; warn-only / ok=True."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # No ~/.scion/settings.yaml
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_user_settings_auth_type
    result = _check_user_settings_auth_type()
    assert result.ok is True


def test_user_settings_auth_fail_malformed(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "malformed.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_user_settings_auth_type
    result = _check_user_settings_auth_type()
    assert result.ok is False
    assert "Malformed" in result.message or "malformed" in result.message


# ---------------------------------------------------------------------------
# SBR-3.7 (b) — _check_grove_settings
# ---------------------------------------------------------------------------


def test_grove_settings_pass(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_grove(fake_home, "mnemosyne__abc", FIXTURES_GROVE / "empiria.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_grove_settings
    result = _check_grove_settings()
    assert result.ok is True


def test_grove_settings_fail_one_drifted(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_grove(fake_home, "mnemosyne__abc", FIXTURES_GROVE / "non_empiria.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_grove_settings
    result = _check_grove_settings()
    assert result.ok is False
    assert "mnemosyne__abc" in result.message


def test_grove_settings_pass_no_groves(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_grove_settings
    result = _check_grove_settings()
    assert result.ok is True


def test_grove_settings_skips_test_groves(tmp_path, monkeypatch):
    """Drifted auto-* test grove + canonical real grove → ok=True (test grove skipped)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_grove(fake_home, "auto-foo", FIXTURES_GROVE / "non_empiria.yaml")
    _seed_grove(fake_home, "real-project", FIXTURES_GROVE / "empiria.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_grove_settings
    result = _check_grove_settings()
    assert result.ok is True, f"test grove must be skipped; got {result.message}"


# ---------------------------------------------------------------------------
# SBR-3.7 (c) — _check_user_profile_env_no_overrides
# ---------------------------------------------------------------------------


def test_profile_env_no_override_pass(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_user_profile_env_no_overrides
    result = _check_user_profile_env_no_overrides()
    assert result.ok is True


def test_profile_env_no_override_fail(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "profile_env_override.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.commands.doctor import _check_user_profile_env_no_overrides
    result = _check_user_profile_env_no_overrides()
    assert result.ok is False
    assert "MNEMOSYNE_VAULT" in result.message
