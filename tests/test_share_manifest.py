"""Tests for share/manifest.py — strict schema validation (D-19).

Covers:
  - valid §4.2 FF manifest round-trip
  - unknown top-level table → ManifestError
  - unknown key within a known table → ManifestError naming the key
  - missing [client].slug → ManifestError
  - missing [on_closure_breach].policy → ManifestError
  - mode=="direct" without [direct] table → ManifestError
  - mode enum: invalid value → ManifestError
  - policy enum: invalid value → ManifestError
  - mode=="intermediary" without [direct] table → valid
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mnemosyne_cli.share import manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FF_MANIFEST_TOML = """\
[client]
slug = "friendly-fox"
display = "Friendly Fox Games"
mode = "direct"

[direct]
target_vault = "friendly-fox-vault"
target_subtree = "imported/empiria"
deploy_key_ref = "ff-vault-deploy"

[intermediary]
package_repo = "git@github.com:empiria/empiria-for-friendly-fox.git"
package_branch = "main"

[include]
paths = [
  "technologies/anvil/reference/**",
  "technologies/anvil/learning/uplink-*.md",
  "technologies/playwright/**",
  "technologies/python/**",
]
tags = ["share:friendly-fox", "share:all-partners"]

[exclude]
paths = ["**/decision/**"]

[on_closure_breach]
policy = "refuse"

[license]
template = "empiria/clients/friendly-fox/license-template.md"
contract_ref = "Empiria-FriendlyFox MSA dated 2025-11-04"
copyright_holder = "Empiria Ltd."
spdx_license_ref = "LicenseRef-Empiria-FriendlyFox-2026"
"""

_INTERMEDIARY_MANIFEST_TOML = """\
[client]
slug = "some-client"
mode = "intermediary"

[intermediary]
package_repo = "git@github.com:empiria/empiria-for-some-client.git"
package_branch = "main"

[include]
paths = ["technologies/python/**"]

[on_closure_breach]
policy = "warn"
"""


def _write_toml(tmp_path: Path, content: str, name: str = "share-manifest.toml") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Valid round-trip: §4.2 FF manifest
# ---------------------------------------------------------------------------

def test_ff_manifest_round_trip(tmp_path: Path) -> None:
    """The §4.2 FF example validates and exposes the expected field values."""
    path = _write_toml(tmp_path, _FF_MANIFEST_TOML)
    sm = manifest.load_manifest(path)

    assert sm.client_slug == "friendly-fox"
    assert sm.client_display == "Friendly Fox Games"
    assert sm.mode == "direct"
    assert sm.policy == "refuse"
    assert "technologies/python/**" in sm.include_paths
    assert sm.include_tags == ["share:friendly-fox", "share:all-partners"]
    assert sm.exclude_paths == ["**/decision/**"]
    assert sm.license is not None
    assert sm.license["spdx_license_ref"] == "LicenseRef-Empiria-FriendlyFox-2026"


# ---------------------------------------------------------------------------
# 2. Unknown top-level table → hard error naming the key
# ---------------------------------------------------------------------------

def test_unknown_top_level_table_raises(tmp_path: Path) -> None:
    toml = _FF_MANIFEST_TOML + "\n[bogus]\nfoo = 1\n"
    path = _write_toml(tmp_path, toml)
    with pytest.raises(manifest.ManifestError, match="bogus"):
        manifest.load_manifest(path)


# ---------------------------------------------------------------------------
# 3. Unknown key inside a known table → hard error naming the key
# ---------------------------------------------------------------------------

def test_unknown_key_in_known_table_raises(tmp_path: Path) -> None:
    # Introduce a typo'd key in [direct]: targt_subtree instead of target_subtree
    bad = _FF_MANIFEST_TOML.replace(
        "target_subtree = \"imported/empiria\"",
        "targt_subtree = \"imported/empiria\"",
    )
    path = _write_toml(tmp_path, bad)
    with pytest.raises(manifest.ManifestError, match="targt_subtree"):
        manifest.load_manifest(path)


# ---------------------------------------------------------------------------
# 4. Missing [client].slug → hard error
# ---------------------------------------------------------------------------

def test_missing_client_slug_raises() -> None:
    data: dict = {
        "client": {"mode": "direct"},
        "direct": {"target_vault": "v", "target_subtree": "s", "deploy_key_ref": "k"},
        "include": {"paths": []},
        "on_closure_breach": {"policy": "warn"},
    }
    with pytest.raises(manifest.ManifestError, match="slug"):
        manifest.validate_manifest_dict(data)


# ---------------------------------------------------------------------------
# 5. Missing [on_closure_breach].policy → hard error
# ---------------------------------------------------------------------------

def test_missing_policy_raises() -> None:
    data: dict = {
        "client": {"slug": "c", "mode": "direct"},
        "direct": {"target_vault": "v", "target_subtree": "s", "deploy_key_ref": "k"},
        "include": {"paths": []},
        "on_closure_breach": {},
    }
    with pytest.raises(manifest.ManifestError, match="policy"):
        manifest.validate_manifest_dict(data)


# ---------------------------------------------------------------------------
# 6. mode=="direct" without [direct] table → hard error
# ---------------------------------------------------------------------------

def test_direct_mode_without_direct_table_raises() -> None:
    data: dict = {
        "client": {"slug": "c", "mode": "direct"},
        "include": {"paths": []},
        "on_closure_breach": {"policy": "refuse"},
    }
    with pytest.raises(manifest.ManifestError, match="direct"):
        manifest.validate_manifest_dict(data)


# ---------------------------------------------------------------------------
# 7. Invalid mode enum → hard error
# ---------------------------------------------------------------------------

def test_invalid_mode_raises() -> None:
    data: dict = {
        "client": {"slug": "c", "mode": "bogus"},
        "include": {"paths": []},
        "on_closure_breach": {"policy": "refuse"},
    }
    with pytest.raises(manifest.ManifestError, match="mode"):
        manifest.validate_manifest_dict(data)


# ---------------------------------------------------------------------------
# 8. Invalid policy enum → hard error
# ---------------------------------------------------------------------------

def test_invalid_policy_raises() -> None:
    data: dict = {
        "client": {"slug": "c", "mode": "intermediary"},
        "include": {"paths": []},
        "on_closure_breach": {"policy": "bogus"},
    }
    with pytest.raises(manifest.ManifestError, match="policy"):
        manifest.validate_manifest_dict(data)


# ---------------------------------------------------------------------------
# 9. mode=="intermediary" without [direct] table → valid
# ---------------------------------------------------------------------------

def test_intermediary_mode_without_direct_table_is_valid(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, _INTERMEDIARY_MANIFEST_TOML)
    sm = manifest.load_manifest(path)
    assert sm.mode == "intermediary"
    assert sm.direct is None
