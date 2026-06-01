"""RED tests for SBR-06 template-drift detection in mnemosyne doctor.

Plan 33.1-04 implements `lib/scion_cache.py` (or extends lib/checks.py).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "scion_cache" / "templates"


def test_find_broker_cache_root_returns_path_when_exists(tmp_path, monkeypatch):
    """find_broker_cache_root returns ~/.scion/cache/templates if it exists."""
    from mnemosyne_cli.lib.scion_cache import find_broker_cache_root

    fake_home = tmp_path / "home"
    cache = fake_home / ".scion" / "cache" / "templates"
    cache.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert find_broker_cache_root() == cache


def test_find_broker_cache_root_returns_none_when_missing(tmp_path, monkeypatch):
    from mnemosyne_cli.lib.scion_cache import find_broker_cache_root

    fake_home = tmp_path / "no_broker_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert find_broker_cache_root() is None


def test_read_template_index_parses_entries():
    from mnemosyne_cli.lib.scion_cache import read_template_index

    index = read_template_index(FIXTURE_CACHE)
    assert index is not None
    assert "empiria-agent" in index["entries"]
    assert index["entries"]["empiria-agent"]["contentHash"] == "abc123"


def test_read_template_index_returns_none_when_missing(tmp_path):
    from mnemosyne_cli.lib.scion_cache import read_template_index

    assert read_template_index(tmp_path) is None


def test_diff_template_against_vault_lists_changed_files(tmp_path):
    """Fixture cache has STALE versions of scion-agent.yaml and post-start.sh.
    Vault-side template has post-33-03 versions. Diff lists both files."""
    from mnemosyne_cli.lib.scion_cache import diff_template_against_vault

    # Build a fresh vault-side template that differs from the fixture
    vault_tpl = tmp_path / "agents" / "scion-template"
    (vault_tpl / "hooks").mkdir(parents=True)
    (vault_tpl / "scion-agent.yaml").write_text(
        'schema_version: "1"\nenv:\n  MNEMOSYNE_WORKSPACE: /workspace\n'
    )
    (vault_tpl / "hooks" / "post-start.sh").write_text(
        "#!/bin/bash\nmnemosyne init --container\n"
    )

    drift = diff_template_against_vault(FIXTURE_CACHE, "empiria-agent", vault_tpl)
    assert "scion-agent.yaml" in drift
    assert "hooks/post-start.sh" in drift


def test_diff_returns_empty_when_in_sync(tmp_path):
    """No drift when vault contents match cached contents byte-for-byte."""
    from mnemosyne_cli.lib.scion_cache import diff_template_against_vault

    vault_tpl = tmp_path / "agents" / "scion-template"
    (vault_tpl / "hooks").mkdir(parents=True)
    # Copy the fixture content verbatim
    shutil.copy(
        FIXTURE_CACHE / "abc123" / "scion-agent.yaml",
        vault_tpl / "scion-agent.yaml",
    )
    shutil.copy(
        FIXTURE_CACHE / "abc123" / "hooks" / "post-start.sh",
        vault_tpl / "hooks" / "post-start.sh",
    )

    drift = diff_template_against_vault(FIXTURE_CACHE, "empiria-agent", vault_tpl)
    assert drift == []


def test_doctor_template_drift_skips_when_no_broker(tmp_path, monkeypatch):
    """D-19: cache absent → check skips with explanatory message, not a failure."""
    from typer.testing import CliRunner

    from mnemosyne_cli.main import app

    fake_home = tmp_path / "home"
    fake_home.mkdir()  # no .scion/ inside
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    runner = CliRunner()
    with patch(
        "mnemosyne_cli.commands.doctor.lib_vault.resolve_vault_path",
        return_value=tmp_path,
    ), patch(
        "mnemosyne_cli.commands.doctor.lib_scion_cache.find_broker_cache_root",
        return_value=None,
    ):
        result = runner.invoke(app, ["doctor"])
    # No broker cache → the SCION Template Freshness category emits a single
    # graceful-skip check named "SCION broker cache" that passes (ok=True),
    # rather than a drift failure (D-19).
    output = result.stdout + result.stderr
    assert "SCION broker cache" in output
