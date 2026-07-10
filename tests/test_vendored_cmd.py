"""CliRunner tests for the `mnemosyne refresh` named-component selector.

Phase 54 Plan 05 (Task 1): CLI surface tests for the D-05 unified named-component
selector extending the existing refresh verb. Covers:
  - No-arg refresh runs all sections including all vendored entries
  - Named-entry refresh syncs only the specified entry (not others)
  - Unknown component name exits non-zero with a clear message

These tests complement test_vendoring.py (which tests the engine) by focusing
on the CLI surface of the named-component selector added in Plan 54-04/54-05.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mnemosyne_cli.commands import refresh
from mnemosyne_cli.lib import vault
from mnemosyne_cli.main import app

from conftest import shallow_clone_run


runner = CliRunner()

FIXTURES_VENDORED = Path(__file__).parent / "fixtures" / "vendored"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_vault_with_manifest(tmp_path: Path) -> Path:
    """Seed a tmp vault with a minimal vendored.toml.

    Returns the vault root path.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    manifest_src = FIXTURES_VENDORED / "vendored.toml"
    manifest_dst = vault_path / "agents" / "vendored.toml"
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    manifest_dst.write_text(manifest_src.read_text())

    return vault_path


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point refresh at a tmp vault with vendored.toml; capture subprocess calls.

    Returns (vault_path, calls) — vault_path is the seeded tmp vault root,
    calls is the list of captured subprocess invocation arg lists.
    """
    vault_path = _seed_vault_with_manifest(tmp_path)
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)
    monkeypatch.setattr(refresh.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[list[str]] = []
    fake_run = shallow_clone_run("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    def capturing_run(args, *a, **k):
        calls.append(list(args))
        return fake_run(args, *a, **k)

    monkeypatch.setattr(refresh.subprocess, "run", capturing_run)

    # Also patch subprocess in the vendoring lib
    import mnemosyne_cli.lib.vendoring as vendoring_mod
    monkeypatch.setattr(vendoring_mod.subprocess, "run", capturing_run)

    return vault_path, calls


# ---------------------------------------------------------------------------
# Named-component selector — D-05 (mnemosyne refresh [name...])
# ---------------------------------------------------------------------------


def test_refresh_no_args_runs_vendored_section(fake_env):
    """'mnemosyne refresh' (no args) processes all sections including all vendored entries.

    The no-arg form is the unified all-refresh path (D-05): images + qmd +
    manifests + all vendored.toml entries.
    """
    vault_path, calls = fake_env
    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 0, f"Expected exit 0; got {result.exit_code}\n{result.output}"

    clone_calls = [c for c in calls if "clone" in c]
    assert len(clone_calls) >= 2, (
        f"refresh (no args) must clone each vendored.toml entry "
        f"(found {len(clone_calls)} clone calls); calls: {calls}"
    )

    # Both entries from the fixture vendored.toml must be cloned
    cloned_upstreams = [
        arg for c in clone_calls for arg in c if arg.startswith("https://")
    ]
    assert any("anvil" in u for u in cloned_upstreams), (
        f"anvil-agent-references upstream must be cloned; clones: {cloned_upstreams}"
    )
    assert any("obsidian" in u for u in cloned_upstreams), (
        f"obsidian-skills upstream must be cloned; clones: {cloned_upstreams}"
    )


def test_refresh_named_entry_syncs_only_that_entry(fake_env):
    """'mnemosyne refresh anvil-agent-references' syncs ONLY that entry, not obsidian-skills.

    The named-selector (D-05) allows refreshing a subset of vendored entries.
    """
    vault_path, calls = fake_env
    result = runner.invoke(app, ["refresh", "anvil-agent-references"])
    assert result.exit_code == 0, f"Expected exit 0; got {result.exit_code}\n{result.output}"

    clone_calls = [c for c in calls if "clone" in c]

    # obsidian-skills must NOT be cloned when only anvil-agent-references is named
    obsidian_clones = [
        c for c in clone_calls if any("obsidian" in arg for arg in c)
    ]
    assert obsidian_clones == [], (
        f"Named selector must not clone other entries; found obsidian: {obsidian_clones}"
    )

    # anvil-agent-references MUST be cloned
    anvil_clones = [
        c for c in clone_calls if any("anvil" in arg for arg in c)
    ]
    assert len(anvil_clones) >= 1, (
        f"refresh anvil-agent-references must clone that entry; calls: {calls}"
    )


def test_refresh_named_entry_skips_images_and_qmd(fake_env):
    """'mnemosyne refresh anvil-agent-references' does not pull images or update qmd."""
    vault_path, calls = fake_env
    result = runner.invoke(app, ["refresh", "anvil-agent-references"])
    assert result.exit_code == 0, result.output

    podman_pulls = [c for c in calls if c[:2] == ["podman", "pull"]]
    assert podman_pulls == [], (
        f"Named vendored-entry refresh must not pull images; got: {podman_pulls}"
    )

    qmd_calls = [c for c in calls if c[:1] == ["qmd"]]
    assert qmd_calls == [], (
        f"Named vendored-entry refresh must not update qmd; got: {qmd_calls}"
    )


def test_refresh_unknown_component_exits_nonzero(tmp_path, monkeypatch):
    """'mnemosyne refresh bogus-component' exits non-zero with a clear message (D-05).

    Unknown component names must error clearly, not silently do nothing.
    """
    vault_path = _seed_vault_with_manifest(tmp_path)
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)
    monkeypatch.setattr(refresh.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, *a, **k):
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)

    result = runner.invoke(app, ["refresh", "bogus-component"])
    assert result.exit_code != 0, (
        f"refresh with unknown component must exit non-zero; got {result.exit_code}"
    )
    output = result.output.lower()
    assert "bogus-component" in output or "unknown" in output, (
        f"Error message must mention the unknown name; got: {result.output!r}"
    )


def test_refresh_fixed_component_images_only(fake_env):
    """'mnemosyne refresh images' pulls images and skips all other sections (D-05)."""
    vault_path, calls = fake_env
    result = runner.invoke(app, ["refresh", "images"])
    assert result.exit_code == 0, result.output

    clone_calls = [c for c in calls if "clone" in c]
    assert clone_calls == [], (
        f"refresh images must not clone vendored entries; got: {clone_calls}"
    )

    qmd_calls = [c for c in calls if c[:1] == ["qmd"]]
    assert qmd_calls == [], (
        f"refresh images must not update qmd; got: {qmd_calls}"
    )

    podman_pulls = [c for c in calls if c[:2] == ["podman", "pull"]]
    assert len(podman_pulls) >= 1, (
        f"refresh images must pull SCION images; calls: {calls}"
    )
