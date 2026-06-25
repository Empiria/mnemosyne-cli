"""RED tests for mnemosyne_cli.lib.vendoring — vendoring engine contract.

Phase 54 Plan 03 (Wave 0): these tests lock the behavioural contract for the
vendoring engine and the 'mnemosyne refresh' vendored-section extension before
any implementation lands (Plans 04/05). They MUST fail RED until the Wave 2
implementation provides lib/vendoring.py and the refresh vendored section.

Contracts under test:
  - load_manifest(vault_path) — parse agents/vendored.toml into entry dicts
  - refresh_entry(entry, vault_path) — clone upstream, sync upstream_owned
    subpaths, preserve Empiria files, write .upstream-ref, stage (never commit)
  - CliRunner: refresh (all), refresh <name> (one), refresh <unknown> (error)

Lazy imports inside each test body (Phase 38 convention) so pytest --collect-only
succeeds before lib/vendoring.py exists.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mnemosyne_cli.commands import refresh
from mnemosyne_cli.lib import vault
from mnemosyne_cli.main import app


runner = CliRunner()

FIXTURES_VENDORED = Path(__file__).parent / "fixtures" / "vendored"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_vault_with_manifest(tmp_path: Path) -> Path:
    """Seed a tmp vault with a minimal vendored.toml and a committed copy.

    Returns the vault root. The committed copy simulates an already-vendored
    entry for anvil-agent-references, including an Empiria index.md and a
    .upstream-ref file that must be preserved by refresh_entry.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    # Copy fixture vendored.toml into vault
    manifest_src = FIXTURES_VENDORED / "vendored.toml"
    manifest_dst = vault_path / "agents" / "vendored.toml"
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    manifest_dst.write_text(manifest_src.read_text())

    # Seed committed copy for anvil-agent-references
    dest = vault_path / "agents" / "vendored" / "anvil-agent-references"
    dest.mkdir(parents=True, exist_ok=True)

    # Empiria-authored files that must NOT be overwritten by refresh
    index_src = FIXTURES_VENDORED / "committed_copy" / "index.md"
    (dest / "index.md").write_text(index_src.read_text())
    upstream_ref_src = FIXTURES_VENDORED / "committed_copy" / ".upstream-ref"
    (dest / ".upstream-ref").write_text(upstream_ref_src.read_text())

    # Upstream-owned skill file already in place
    skills_dir = dest / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_src = FIXTURES_VENDORED / "committed_copy" / "skills" / "SKILL.md"
    (skills_dir / "SKILL.md").write_text(skill_src.read_text())

    return vault_path


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point refresh at a tmp vault with a vendored.toml and capture subprocess calls.

    Returns (vault_path, calls) — vault_path is the seeded tmp vault root,
    calls is the list of captured subprocess invocation arg lists.

    Mirrors the test_refresh.py:24-37 fixture shape exactly (Phase 38 pattern).
    """
    vault_path = _seed_vault_with_manifest(tmp_path)
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)
    monkeypatch.setattr(refresh.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[list[str]] = []
    NEW_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    def fake_run(args, *a, **k):
        # Simulate rev-parse HEAD returning a fresh upstream SHA
        if args[:3] == ["git", "-C"] and "rev-parse" in args:
            return MagicMock(returncode=0, stdout=NEW_SHA + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    # Capture subprocess calls: patch the module-level subprocess in refresh
    # AND the vendoring lib (both use subprocess.run)
    calls_wrapper = calls

    def capturing_run(args, *a, **k):
        calls_wrapper.append(list(args))
        return fake_run(args, *a, **k)

    monkeypatch.setattr(refresh.subprocess, "run", capturing_run)

    # Also patch subprocess in the vendoring lib (lazy — vendoring may not exist yet)
    try:
        import mnemosyne_cli.lib.vendoring as vendoring_mod
        monkeypatch.setattr(vendoring_mod.subprocess, "run", capturing_run)
    except (ImportError, AttributeError):
        pass  # Expected RED state: lib/vendoring.py does not exist yet

    return vault_path, calls


# ---------------------------------------------------------------------------
# load_manifest tests
# ---------------------------------------------------------------------------


def test_load_manifest_parses_entries(tmp_path):
    """load_manifest returns a list of dicts with the required keys."""
    vault_path = _seed_vault_with_manifest(tmp_path)

    from mnemosyne_cli.lib.vendoring import load_manifest  # lazy import

    entries = load_manifest(vault_path)
    assert len(entries) == 2

    first = entries[0]
    assert first["name"] == "anvil-agent-references"
    assert "upstream" in first
    assert "path" in first
    assert "ref" in first
    assert "upstream_owned" in first
    assert isinstance(first["upstream_owned"], list)


def test_load_manifest_returns_all_vendored_sections(tmp_path):
    """Both [[vendored]] entries are present in the parsed manifest."""
    vault_path = _seed_vault_with_manifest(tmp_path)

    from mnemosyne_cli.lib.vendoring import load_manifest  # lazy import

    entries = load_manifest(vault_path)
    names = [e["name"] for e in entries]
    assert "anvil-agent-references" in names
    assert "obsidian-skills" in names


# ---------------------------------------------------------------------------
# refresh_entry tests (unit: mocked subprocess)
# ---------------------------------------------------------------------------


def test_refresh_entry_invokes_git_clone(tmp_path, monkeypatch):
    """refresh_entry triggers git clone --depth 1 (mocked — no network)."""
    vault_path = _seed_vault_with_manifest(tmp_path)
    NEW_SHA = "cafebabecafebabecafebabecafebabecafebabe"
    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        if "rev-parse" in args:
            return MagicMock(returncode=0, stdout=NEW_SHA + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from mnemosyne_cli.lib.vendoring import load_manifest, refresh_entry  # lazy import

    monkeypatch.setattr("mnemosyne_cli.lib.vendoring.subprocess.run", fake_run)

    entries = load_manifest(vault_path)
    anvil_entry = next(e for e in entries if e["name"] == "anvil-agent-references")
    refresh_entry(anvil_entry, vault_path)

    clone_calls = [c for c in calls if "clone" in c]
    assert len(clone_calls) >= 1, f"Expected git clone call; got calls: {calls}"
    assert "--depth" in clone_calls[0], "clone must be shallow (--depth 1)"


def test_refresh_entry_writes_upstream_ref(tmp_path, monkeypatch):
    """refresh_entry writes .upstream-ref with the resolved HEAD SHA."""
    vault_path = _seed_vault_with_manifest(tmp_path)
    NEW_SHA = "cafebabecafebabecafebabecafebabecafebabe"

    def fake_run(args, *a, **k):
        if "rev-parse" in args:
            return MagicMock(returncode=0, stdout=NEW_SHA + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from mnemosyne_cli.lib.vendoring import load_manifest, refresh_entry  # lazy import

    monkeypatch.setattr("mnemosyne_cli.lib.vendoring.subprocess.run", fake_run)

    entries = load_manifest(vault_path)
    anvil_entry = next(e for e in entries if e["name"] == "anvil-agent-references")
    refresh_entry(anvil_entry, vault_path)

    upstream_ref_path = vault_path / "agents" / "vendored" / "anvil-agent-references" / ".upstream-ref"
    assert upstream_ref_path.exists(), ".upstream-ref must be written by refresh_entry"
    content = upstream_ref_path.read_text().strip()
    assert content == NEW_SHA, f".upstream-ref must contain the resolved SHA; got {content!r}"


def test_refresh_entry_preserves_empiria_index_md(tmp_path, monkeypatch):
    """refresh_entry must NOT overwrite the Empiria-authored index.md."""
    vault_path = _seed_vault_with_manifest(tmp_path)
    original_index = (
        vault_path / "agents" / "vendored" / "anvil-agent-references" / "index.md"
    ).read_text()
    NEW_SHA = "cafebabecafebabecafebabecafebabecafebabe"

    def fake_run(args, *a, **k):
        if "rev-parse" in args:
            return MagicMock(returncode=0, stdout=NEW_SHA + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from mnemosyne_cli.lib.vendoring import load_manifest, refresh_entry  # lazy import

    monkeypatch.setattr("mnemosyne_cli.lib.vendoring.subprocess.run", fake_run)

    entries = load_manifest(vault_path)
    anvil_entry = next(e for e in entries if e["name"] == "anvil-agent-references")
    refresh_entry(anvil_entry, vault_path)

    preserved = (
        vault_path / "agents" / "vendored" / "anvil-agent-references" / "index.md"
    ).read_text()
    assert preserved == original_index, (
        "refresh_entry must preserve the Empiria-authored index.md (not in upstream_owned)"
    )


def test_refresh_entry_stages_but_never_commits(tmp_path, monkeypatch):
    """refresh_entry stages via git add but NEVER calls git commit (D-06 / T-54-03)."""
    vault_path = _seed_vault_with_manifest(tmp_path)
    NEW_SHA = "cafebabecafebabecafebabecafebabecafebabe"
    calls: list[list[str]] = []

    def fake_run(args, *a, **k):
        calls.append(list(args))
        if "rev-parse" in args:
            return MagicMock(returncode=0, stdout=NEW_SHA + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from mnemosyne_cli.lib.vendoring import load_manifest, refresh_entry  # lazy import

    monkeypatch.setattr("mnemosyne_cli.lib.vendoring.subprocess.run", fake_run)

    entries = load_manifest(vault_path)
    anvil_entry = next(e for e in entries if e["name"] == "anvil-agent-references")
    refresh_entry(anvil_entry, vault_path)

    # T-54-03: stage-not-commit invariant — no call must contain "commit"
    commit_calls = [c for c in calls if "commit" in c]
    assert commit_calls == [], (
        f"refresh_entry must NEVER invoke git commit; found: {commit_calls}"
    )

    # Must have at least one git add call
    add_calls = [c for c in calls if "add" in c]
    assert len(add_calls) >= 1, f"Expected git add call to stage the path; got calls: {calls}"


def test_refresh_entry_syncs_only_upstream_owned_subpaths(tmp_path, monkeypatch):
    """refresh_entry copies only upstream_owned subpaths (not the whole tree)."""
    vault_path = _seed_vault_with_manifest(tmp_path)
    NEW_SHA = "cafebabecafebabecafebabecafebabecafebabe"

    # Create a fake upstream clone dir that includes both an upstream_owned path
    # and a file that should NOT be copied (simulating non-upstream content)
    import tempfile

    upstream_dir = Path(tempfile.mkdtemp())
    (upstream_dir / "skills").mkdir()
    (upstream_dir / "skills" / "new-skill.md").write_text("# New upstream skill\n")
    (upstream_dir / "extra-non-upstream.md").write_text("# Should not be copied\n")

    def fake_run(args, *a, **k):
        if "clone" in args:
            # Simulate clone populating the tempdir — not needed since we use a pre-built dir
            return MagicMock(returncode=0, stdout="", stderr="")
        if "rev-parse" in args:
            return MagicMock(returncode=0, stdout=NEW_SHA + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from mnemosyne_cli.lib.vendoring import load_manifest, refresh_entry  # lazy import

    monkeypatch.setattr("mnemosyne_cli.lib.vendoring.subprocess.run", fake_run)
    # Patch the tempdir creation so refresh_entry uses our pre-built upstream_dir
    import tempfile as tempfile_mod
    monkeypatch.setattr(
        "mnemosyne_cli.lib.vendoring.tempfile.TemporaryDirectory",
        lambda: _FakeTempDir(str(upstream_dir)),
    )

    entries = load_manifest(vault_path)
    anvil_entry = next(e for e in entries if e["name"] == "anvil-agent-references")
    refresh_entry(anvil_entry, vault_path)

    dest = vault_path / "agents" / "vendored" / "anvil-agent-references"
    # extra-non-upstream.md is NOT in upstream_owned — must not be copied
    assert not (dest / "extra-non-upstream.md").exists(), (
        "refresh_entry must only copy upstream_owned subpaths, not the whole tree"
    )


class _FakeTempDir:
    """Context manager that exposes a pre-built directory path."""

    def __init__(self, path: str) -> None:
        self.name = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cleanup(self):
        pass


# ---------------------------------------------------------------------------
# CliRunner tests — named-selector contract
# ---------------------------------------------------------------------------


def test_cli_refresh_all_runs_vendored_section(fake_env):
    """'mnemosyne refresh' (no args) processes all sections including each vendored entry."""
    vault_path, calls = fake_env
    result = runner.invoke(app, ["refresh"])
    # The command should not crash (even if vendoring lib absent — test exits non-zero RED)
    # We assert that this test itself fails RED since lib/vendoring.py doesn't exist yet.
    # Once the implementation lands, this assert will check for clone calls.
    clone_calls = [c for c in calls if "clone" in c]
    assert len(clone_calls) >= 2, (
        f"refresh (no args) must clone each vendored.toml entry; got calls: {calls}"
    )


def test_cli_refresh_named_entry_syncs_only_that_entry(fake_env):
    """'mnemosyne refresh anvil-agent-references' syncs ONLY that entry."""
    vault_path, calls = fake_env
    result = runner.invoke(app, ["refresh", "anvil-agent-references"])
    # Named-selector: only anvil-agent-references is cloned, not obsidian-skills
    clone_calls = [c for c in calls if "clone" in c]
    # obsidian-skills URL must NOT appear in any clone call
    obsidian_clones = [
        c for c in clone_calls
        if any("obsidian" in arg for arg in c)
    ]
    assert obsidian_clones == [], (
        f"refresh <name> must clone ONLY the named entry; found obsidian clone: {obsidian_clones}"
    )
    # anvil-agent-references URL must appear
    anvil_clones = [
        c for c in clone_calls
        if any("anvil" in arg for arg in c)
    ]
    assert len(anvil_clones) >= 1, (
        f"refresh anvil-agent-references must clone anvil-agent-references; got calls: {calls}"
    )


def test_cli_refresh_unknown_name_exits_nonzero(tmp_path, monkeypatch):
    """'mnemosyne refresh nonsense' exits non-zero with a clear message (not silent no-op)."""
    vault_path = _seed_vault_with_manifest(tmp_path)
    monkeypatch.setattr(vault, "resolve_vault_path", lambda: vault_path)
    monkeypatch.setattr(refresh.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, *a, **k):
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)

    result = runner.invoke(app, ["refresh", "nonsense-component"])
    assert result.exit_code != 0, (
        "refresh with an unknown component name must exit non-zero"
    )
    output = result.output.lower()
    assert "nonsense-component" in output or "unknown" in output or "not found" in output, (
        f"Error message must mention the unknown name or 'unknown'; got: {result.output!r}"
    )
