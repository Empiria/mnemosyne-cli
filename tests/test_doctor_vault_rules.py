"""Tests for the 'Vault Consistency' doctor check category (D-F1).

All assertions go through doctor._build_checks() filtered by category —
never doctor.run() (Pitfall 1: host-state leakage).
"""

from __future__ import annotations

import tomli_w
from pathlib import Path

from mnemosyne_cli.commands import doctor
from mnemosyne_cli.lib import vault as lib_vault


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_toml(tmp_path: Path, vaults: dict[str, str], rules: list[dict]) -> Path:
    """Write a synthetic config.toml with vaults and vault_rules."""
    config_path = tmp_path / "config.toml"
    data: dict = {}
    if vaults:
        data["vaults"] = {
            name: {"path": str(path), "description": "", "sync": "git"}
            for name, path in vaults.items()
        }
    if rules:
        data["vault_rules"] = rules
    config_path.write_bytes(tomli_w.dumps(data).encode())
    return config_path


def _setup_env(tmp_path: Path):
    """Build a minimal vault + project under tmp_path."""
    vault_path = tmp_path / "empiria-vault"
    vault_path.mkdir(parents=True)

    project_path = tmp_path / "project"
    project_path.mkdir(parents=True)
    git_dir = project_path / ".git"
    (git_dir / "info").mkdir(parents=True)

    return vault_path, project_path


def _get_vc_checks(project_path: Path, vault_path: Path):
    """Return only the 'Vault Consistency' checks from _build_checks."""
    git_dir = project_path / ".git"
    checks = doctor._build_checks(project_path, vault_path, git_dir)
    return [c for c in checks if c.category == "Vault Consistency"]


# ---------------------------------------------------------------------------
# Test 1: Consistent rules → ok
# ---------------------------------------------------------------------------


def test_vault_consistency_all_ok(tmp_path, monkeypatch):
    """D-F1: consistent vault_rules (all from/can_read vaults registered) → ok."""
    vault_path, project_path = _setup_env(tmp_path)

    config_path = _make_config_toml(
        tmp_path,
        vaults={
            "empiria": str(vault_path),
            "friendly-fox-vault": str(tmp_path / "ff-vault"),
        },
        rules=[
            {"from": "empiria", "can_read": ["friendly-fox-vault"]},
        ],
    )
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    vc_checks = _get_vc_checks(project_path, vault_path)
    assert vc_checks, "Expected at least one Vault Consistency check"

    results = [(c.name, c.check()) for c in vc_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert not failures, f"Expected all Vault Consistency checks to pass; failures: {failures}"


# ---------------------------------------------------------------------------
# Test 2: Orphan 'from' → not ok, message names the orphan vault
# ---------------------------------------------------------------------------


def test_vault_consistency_orphan_from_fails(tmp_path, monkeypatch):
    """D-F1: orphan vault_rules 'from' entry (vault not registered) → FAIL."""
    vault_path, project_path = _setup_env(tmp_path)

    config_path = _make_config_toml(
        tmp_path,
        vaults={"empiria": str(vault_path)},
        # 'ghost-vault' is not registered in [vaults.*]
        rules=[{"from": "ghost-vault", "can_read": ["empiria"]}],
    )
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    vc_checks = _get_vc_checks(project_path, vault_path)
    assert vc_checks, "Expected Vault Consistency checks"

    results = [(c.name, c.check()) for c in vc_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert failures, "Expected at least one failing Vault Consistency check for orphan from"
    # The message must name the orphan vault
    assert any("ghost-vault" in r.message for _, r in failures), (
        f"Expected 'ghost-vault' in failure message; got {failures}"
    )


# ---------------------------------------------------------------------------
# Test 3: Unregistered 'can_read' target → not ok, message names the target
# ---------------------------------------------------------------------------


def test_vault_consistency_unregistered_can_read_fails(tmp_path, monkeypatch):
    """D-F1: unregistered can_read target → FAIL."""
    vault_path, project_path = _setup_env(tmp_path)

    config_path = _make_config_toml(
        tmp_path,
        vaults={"empiria": str(vault_path)},
        # 'nonexistent-vault' is not registered
        rules=[{"from": "empiria", "can_read": ["nonexistent-vault"]}],
    )
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    vc_checks = _get_vc_checks(project_path, vault_path)
    assert vc_checks, "Expected Vault Consistency checks"

    results = [(c.name, c.check()) for c in vc_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert failures, "Expected failing check for unregistered can_read target"
    assert any("nonexistent-vault" in r.message for _, r in failures), (
        f"Expected 'nonexistent-vault' in failure message; got {failures}"
    )


# ---------------------------------------------------------------------------
# Test 4: No vaults registered → ok "skipped"
# ---------------------------------------------------------------------------


def test_vault_consistency_empty_registry_ok(tmp_path, monkeypatch):
    """D-F1: when no vaults are registered, consistency check is ok (skipped)."""
    vault_path, project_path = _setup_env(tmp_path)

    # Config has no vaults at all
    config_path = _make_config_toml(tmp_path, vaults={}, rules=[])
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    vc_checks = _get_vc_checks(project_path, vault_path)
    assert vc_checks, "Expected Vault Consistency checks even with empty registry"

    results = [(c.name, c.check()) for c in vc_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert not failures, (
        f"Expected ok (skipped) when no vaults registered; failures: {failures}"
    )


# ---------------------------------------------------------------------------
# Test 5: Missing-but-expected rule is NOT flagged
# ---------------------------------------------------------------------------


def test_vault_consistency_missing_expected_rule_not_flagged(tmp_path, monkeypatch):
    """D-F1: a plausible-but-absent rule is NOT flagged as an error.

    Doctor only flags inconsistencies (orphan from / unregistered can_read) —
    it does NOT check for missing rules that 'should' exist.
    """
    vault_path, project_path = _setup_env(tmp_path)

    # Two registered vaults but NO vault_rules at all
    config_path = _make_config_toml(
        tmp_path,
        vaults={
            "empiria": str(vault_path),
            "friendly-fox-vault": str(tmp_path / "ff-vault"),
        },
        rules=[],
    )
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    vc_checks = _get_vc_checks(project_path, vault_path)
    assert vc_checks, "Expected Vault Consistency checks"

    results = [(c.name, c.check()) for c in vc_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert not failures, (
        "A missing (but plausible) vault rule should NOT be flagged; "
        f"unexpected failures: {failures}"
    )
