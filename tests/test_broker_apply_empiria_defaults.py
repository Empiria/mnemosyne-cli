"""RED tests for SBR-3.7 apply-empiria-defaults verb in lib/broker.py.

Plan 33.3-03 Task 03.2 (Wave 2) implements `apply_empiria_defaults()` —
idempotent, overwrite-on-mismatch convergence across user settings.yaml,
every grove settings.yaml, and the harness-config dir.

All tests use lazy imports inside test bodies (Phase 38 P02 convention) so
--collect-only succeeds even before the implementation lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import yaml


FIXTURES_SETTINGS = Path(__file__).parent / "fixtures" / "settings_yaml"
FIXTURES_GROVE = Path(__file__).parent / "fixtures" / "grove_settings"


def _seed_user_settings(home: Path, source: Path) -> Path:
    path = home / ".scion" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.read_text())
    return path


def _seed_grove(home: Path, name: str, source: Path) -> Path:
    path = home / ".scion" / "grove-configs" / name / ".scion" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.read_text())
    return path


def test_apply_empiria_defaults_writes_oauth_token_on_drifted_user_settings(tmp_path, monkeypatch):
    """User ~/.scion/settings.yaml with auth-file drift converges to oauth-token.

    The claude-anvil block (absent in the drifted fixture) is created with the
    canonical auth type too — every managed harness-config converges.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    user_settings = _seed_user_settings(fake_home, FIXTURES_SETTINGS / "drifted_auth.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults()

    data = yaml.safe_load(user_settings.read_text()) or {}
    assert data["harness_configs"]["claude"]["auth_selected_type"] == "oauth-token"
    assert data["harness_configs"]["claude-anvil"]["auth_selected_type"] == "oauth-token"


def test_apply_empiria_defaults_writes_canonical_grove_settings(tmp_path, monkeypatch):
    """Each grove's settings.yaml is rewritten with default_template=empiria-agent,
    default_harness_config=claude.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    grove_a = _seed_grove(fake_home, "infinite-worlds__abc", FIXTURES_GROVE / "non_empiria.yaml")
    grove_b = _seed_grove(fake_home, "mnemosyne__def", FIXTURES_GROVE / "non_empiria.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults()

    for grove_path in (grove_a, grove_b):
        data = yaml.safe_load(grove_path.read_text()) or {}
        assert data["default_template"] == "empiria-agent", f"{grove_path}: {data}"
        assert data["default_harness_config"] == "claude", f"{grove_path}: {data}"


def test_apply_empiria_defaults_idempotent_on_canonical_state(tmp_path, monkeypatch):
    """Second call on already-canonical state returns an empty changes list."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    _seed_grove(fake_home, "infinite-worlds__abc", FIXTURES_GROVE / "empiria.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults()                       # first call (already canonical)
    changes = apply_empiria_defaults()             # second call must be no-op
    # apply_empiria_defaults returns a sequence of changes (path, target) tuples
    # OR similar. The contract: no writes on already-canonical state.
    assert not changes, f"expected idempotent no-op; got {changes}"


def test_apply_empiria_defaults_dry_run_does_not_write(tmp_path, monkeypatch):
    """dry_run=True must compute changes without persisting them."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    user_settings = _seed_user_settings(fake_home, FIXTURES_SETTINGS / "drifted_auth.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    original = user_settings.read_text()

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    changes = apply_empiria_defaults(dry_run=True)

    assert changes, "expected change list under dry-run"
    assert user_settings.read_text() == original, "dry-run must not write"


def test_apply_empiria_defaults_skips_scion_test_groves(tmp_path, monkeypatch):
    """auto-*, test-*, etc. groves are skipped per DEFAULT_SKIP_PREFIXES."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    drifted_test = _seed_grove(fake_home, "auto-foo", FIXTURES_GROVE / "non_empiria.yaml")
    drifted_real = _seed_grove(fake_home, "mnemosyne__abc", FIXTURES_GROVE / "non_empiria.yaml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults()

    # SCION test grove left untouched
    test_data = yaml.safe_load(drifted_test.read_text()) or {}
    assert test_data["default_template"] == "default", \
        f"SCION test grove should be skipped; got {test_data}"
    # Real grove converged
    real_data = yaml.safe_load(drifted_real.read_text()) or {}
    assert real_data["default_template"] == "empiria-agent"


def test_apply_empiria_defaults_preflight_no_user_settings(tmp_path, monkeypatch):
    """When ~/.scion/settings.yaml is absent, pre-flight returns error / raises (RESEARCH Pitfall 5)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # No ~/.scion/settings.yaml
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    with pytest.raises((FileNotFoundError, RuntimeError)):
        apply_empiria_defaults()


def test_grove_convergence_preserves_extra_keys(tmp_path, monkeypatch):
    """Grove convergence is a field-level merge — extra operator keys survive.

    Plan 33.3-09 (CR-01 gap-closure): compute_canonical_changes() must build
    the grove target as dict(grove_data) with the two Empiria-managed keys
    overwritten, not as the bare two-key dict from _build_grove_settings_target().
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")

    # Grove with drifted default_template PLUS extra operator keys
    grove_path = (
        fake_home / ".scion" / "grove-configs" / "myproject__abc" / ".scion" / "settings.yaml"
    )
    grove_path.parent.mkdir(parents=True, exist_ok=True)
    grove_path.write_text(
        "default_template: default\n"
        "default_harness_config: gemini\n"
        "profiles:\n"
        "  fast:\n"
        "    model: claude-3-5-haiku-20241022\n"
        "  thorough:\n"
        "    model: claude-opus-4-5\n"
        "custom_field: operator-configured-value\n"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults(dry_run=False)

    data = yaml.safe_load(grove_path.read_text()) or {}
    # Empiria-managed keys corrected
    assert data["default_template"] == "empiria-agent", f"template wrong: {data}"
    assert data["default_harness_config"] == "claude", f"harness wrong: {data}"
    # Operator keys preserved verbatim
    assert "profiles" in data, f"profiles lost after convergence: {data}"
    assert data["profiles"]["fast"]["model"] == "claude-3-5-haiku-20241022"
    assert data["profiles"]["thorough"]["model"] == "claude-opus-4-5"
    assert data.get("custom_field") == "operator-configured-value", f"custom_field lost: {data}"


def test_grove_convergence_preserves_anvil_variant(tmp_path, monkeypatch):
    """A grove deliberately on empiria-agent-anvil/claude-anvil is canonical —
    convergence must NOT flatten it back to empiria-agent/claude."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    anvil_grove = _seed_grove(
        fake_home, "infinite-worlds__abc", FIXTURES_GROVE / "empiria_anvil.yaml"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults()

    data = yaml.safe_load(anvil_grove.read_text()) or {}
    assert data["default_template"] == "empiria-agent-anvil", data
    assert data["default_harness_config"] == "claude-anvil", data


def test_grove_convergence_repairs_mismatched_anvil_pair(tmp_path, monkeypatch):
    """An anvil-template grove with the wrong harness-config gets the PAIRED
    harness (claude-anvil), not the global claude default."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_user_settings(fake_home, FIXTURES_SETTINGS / "canonical.yaml")
    grove = _seed_grove(fake_home, "infinite-worlds__abc", FIXTURES_GROVE / "empiria.yaml")
    grove.write_text("default_template: empiria-agent-anvil\ndefault_harness_config: claude\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mnemosyne_cli.lib.broker import apply_empiria_defaults
    apply_empiria_defaults()

    data = yaml.safe_load(grove.read_text()) or {}
    assert data["default_template"] == "empiria-agent-anvil", data
    assert data["default_harness_config"] == "claude-anvil", data
