"""Tests for mnemosyne doctor --share-manifests (D-16, D-17, D-18).

Tests the _run_share_manifests helper directly (module-level, like the
_check_* helpers) against tmp vaults, covering:

- D-17 policy matrix: refuse+breach -> non-zero; warn+breach -> zero; strip+breach -> zero
- D-18 broken-never-gates: broken-only manifests never trigger non-zero exit
- D-16 --json: structured output with the four classified-list keys + metadata
- Malformed manifest: recorded as hard failure (non-zero) without crashing the run
- Clean manifest (no breaches): refuse -> zero

All tests call _run_share_manifests directly; no CLI invocation required.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mnemosyne_cli.commands.doctor import _run_share_manifests


# ---------------------------------------------------------------------------
# Fixture: copy the Plan-04 share_vault into a tmp clients/ layout
# ---------------------------------------------------------------------------

SHARE_VAULT_FIXTURE = Path(__file__).parent / "fixtures" / "share_vault"


@pytest.fixture()
def vault_with_breach(tmp_path: Path) -> Path:
    """Copy the full share_vault fixture into tmp_path.

    Returns the vault root (tmp_path / "vault") which already contains
    clients/testclient/share-manifest.toml with policy=refuse and breaches.
    """
    vault = tmp_path / "vault"
    shutil.copytree(SHARE_VAULT_FIXTURE, vault)
    return vault


@pytest.fixture()
def vault_warn_policy(tmp_path: Path) -> Path:
    """Share vault fixture with policy changed to 'warn'."""
    vault = tmp_path / "vault"
    shutil.copytree(SHARE_VAULT_FIXTURE, vault)
    manifest = vault / "clients" / "testclient" / "share-manifest.toml"
    original = manifest.read_text()
    manifest.write_text(original.replace('policy = "refuse"', 'policy = "warn"'))
    return vault


@pytest.fixture()
def vault_strip_policy(tmp_path: Path) -> Path:
    """Share vault fixture with policy changed to 'strip'."""
    vault = tmp_path / "vault"
    shutil.copytree(SHARE_VAULT_FIXTURE, vault)
    manifest = vault / "clients" / "testclient" / "share-manifest.toml"
    original = manifest.read_text()
    manifest.write_text(original.replace('policy = "refuse"', 'policy = "strip"'))
    return vault


@pytest.fixture()
def vault_broken_only(tmp_path: Path) -> Path:
    """A vault with a refuse-policy manifest whose seed has only broken links.

    Seed includes a note that links to a non-existent target only.
    No excluded or breach notes — only broken links.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    # Create a single note with a broken wikilink
    note_dir = vault / "clients" / "testclient"
    note_dir.mkdir(parents=True)
    tech_dir = vault / "technologies" / "clean"
    tech_dir.mkdir(parents=True)
    (tech_dir / "seed.md").write_text(
        "---\ntags: []\n---\n\n# Seed\n\n[[does-not-exist]]\n"
    )
    # Manifest with refuse policy — only broken link, no breach
    (note_dir / "share-manifest.toml").write_text(
        '[client]\nslug = "testclient"\ndisplay = "Test"\nmode = "direct"\n\n'
        "[direct]\ntarget_vault = \"tv\"\ntarget_subtree = \"sub\"\ndeploy_key_ref = \"key\"\n\n"
        "[include]\npaths = [\"technologies/clean/**\"]\ntags = []\n\n"
        "[exclude]\npaths = []\n\n"
        "[on_closure_breach]\npolicy = \"refuse\"\n"
    )
    return vault


@pytest.fixture()
def vault_clean_refuse(tmp_path: Path) -> Path:
    """A vault with a refuse-policy manifest and NO breaches at all."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note_dir = vault / "clients" / "testclient"
    note_dir.mkdir(parents=True)
    tech_dir = vault / "technologies" / "clean"
    tech_dir.mkdir(parents=True)
    (tech_dir / "a.md").write_text("---\ntags: []\n---\n\n# A\n")
    (tech_dir / "b.md").write_text("---\ntags: []\n---\n\n# B\n\n[[a]]\n")
    (note_dir / "share-manifest.toml").write_text(
        '[client]\nslug = "testclient"\ndisplay = "Test"\nmode = "direct"\n\n'
        "[direct]\ntarget_vault = \"tv\"\ntarget_subtree = \"sub\"\ndeploy_key_ref = \"key\"\n\n"
        "[include]\npaths = [\"technologies/clean/**\"]\ntags = []\n\n"
        "[exclude]\npaths = []\n\n"
        "[on_closure_breach]\npolicy = \"refuse\"\n"
    )
    return vault


@pytest.fixture()
def vault_malformed(tmp_path: Path) -> Path:
    """A vault with a manifest that has an unknown key (strict validation error, D-19)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note_dir = vault / "clients" / "testclient"
    note_dir.mkdir(parents=True)
    (note_dir / "share-manifest.toml").write_text(
        '[client]\nslug = "testclient"\ndisplay = "Test"\nmode = "direct"\n\n'
        "[direct]\ntarget_vault = \"tv\"\ntarget_subtree = \"sub\"\ndeploy_key_ref = \"key\"\n\n"
        "[include]\npaths = [\"technologies/**\"]\n\n"
        "[on_closure_breach]\npolicy = \"refuse\"\n\n"
        "[unknown_section]\nthis_should_fail = true\n"
    )
    return vault


# ---------------------------------------------------------------------------
# D-17 policy matrix tests
# ---------------------------------------------------------------------------


def test_refuse_with_breach_returns_true(vault_with_breach: Path, capsys) -> None:
    """D-17: refuse-policy manifest WITH breaches -> _run_share_manifests returns True.

    The testclient fixture has excluded notes (adr-1.md) and a closure breach
    (leaky.md) — has_breaches is True. Policy is refuse -> must exit non-zero.
    """
    result = _run_share_manifests(vault_with_breach, json_out=False)
    assert result is True, (
        "Expected _run_share_manifests to return True (non-zero exit) "
        "for a refuse-policy manifest with breaches"
    )


def test_warn_with_breach_returns_false(vault_warn_policy: Path, capsys) -> None:
    """D-17: warn-policy manifest WITH breaches -> _run_share_manifests returns False.

    Same vault content as test_refuse_with_breach but policy=warn.
    Reports violations but exits zero.
    """
    result = _run_share_manifests(vault_warn_policy, json_out=False)
    assert result is False, (
        "Expected _run_share_manifests to return False (zero exit) "
        "for a warn-policy manifest with breaches"
    )


def test_strip_with_breach_returns_false(vault_strip_policy: Path, capsys) -> None:
    """D-17: strip-policy manifest WITH breaches -> _run_share_manifests returns False."""
    result = _run_share_manifests(vault_strip_policy, json_out=False)
    assert result is False, (
        "Expected _run_share_manifests to return False (zero exit) "
        "for a strip-policy manifest with breaches"
    )


# ---------------------------------------------------------------------------
# D-18 broken-never-gates test
# ---------------------------------------------------------------------------


def test_broken_only_never_gates(vault_broken_only: Path, capsys) -> None:
    """D-18: broken/dangling links never affect the exit code, even with policy=refuse.

    A manifest that has only broken links (no excluded, no breach) must return
    False regardless of the policy value.
    """
    result = _run_share_manifests(vault_broken_only, json_out=False)
    assert result is False, (
        "Expected _run_share_manifests to return False (zero exit) "
        "for a refuse-policy manifest with ONLY broken links (no breach/excluded)"
    )


# ---------------------------------------------------------------------------
# Clean manifest test
# ---------------------------------------------------------------------------


def test_clean_manifest_refuse_returns_false(vault_clean_refuse: Path, capsys) -> None:
    """D-17: clean refuse-policy manifest (all in-set, no breaches) -> returns False."""
    result = _run_share_manifests(vault_clean_refuse, json_out=False)
    assert result is False, (
        "Expected _run_share_manifests to return False for a refuse-policy manifest "
        "with no breaches"
    )


# ---------------------------------------------------------------------------
# D-16 --json output test
# ---------------------------------------------------------------------------


def test_json_output_parses_and_has_required_keys(vault_with_breach: Path, capsys) -> None:
    """D-16: --json emits a JSON array; each item has the four classification keys + metadata."""
    result = _run_share_manifests(vault_with_breach, json_out=True)

    captured = capsys.readouterr()
    # Must be valid JSON
    data = json.loads(captured.out)
    # Must be a list
    assert isinstance(data, list), "Expected JSON output to be a list"
    assert len(data) >= 1, "Expected at least one manifest result in JSON output"

    # Each item must have the four classified lists + metadata keys
    required_keys = {"client_slug", "policy", "in_set", "excluded", "breach", "broken"}
    for item in data:
        missing = required_keys - item.keys()
        assert not missing, f"JSON item missing keys: {missing}"

    # The testclient result should reflect the breach state
    item = data[0]
    assert item["client_slug"] == "testclient"
    assert item["policy"] == "refuse"


# ---------------------------------------------------------------------------
# Malformed manifest test
# ---------------------------------------------------------------------------


def test_malformed_manifest_recorded_as_hard_failure(
    vault_malformed: Path, capsys
) -> None:
    """Malformed manifest (ManifestError) is reported and causes non-zero exit.

    The run must NOT raise an uncaught exception — it records the error and
    returns True (hard failure), but continues (does not crash).
    """
    # Should not raise
    result = _run_share_manifests(vault_malformed, json_out=False)
    assert result is True, (
        "Expected _run_share_manifests to return True (hard failure) "
        "for a malformed manifest (unknown section key)"
    )
    # Must print some error indication
    captured = capsys.readouterr()
    # The error should appear somewhere in the output (stdout or stderr)
    combined_output = captured.out + captured.err
    assert combined_output.strip(), (
        "Expected some output for the malformed manifest error"
    )
