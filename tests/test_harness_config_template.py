"""RED tests for SBR-2.2 canonical harness-config template in vault.

Plan 33.2-02 commits the canonical file at:
  <vault>/agents/scion-template/claude-harness-config/.claude.json

Operators copy this over ~/.scion/harness-configs/claude/home/.claude.json
and run `scion harness-config reset claude` to re-push to brokers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


CANONICAL_REL = "agents/scion-template/claude-harness-config/.claude.json"


def _resolve_vault_root() -> Path | None:
    """Resolve the vault root: $MNEMOSYNE_VAULT, else sibling ../mnemosyne/."""
    env = os.environ.get("MNEMOSYNE_VAULT")
    if env:
        candidate = Path(env)
        if (candidate / CANONICAL_REL).exists():
            return candidate
    # Fallback: mnemosyne-cli is /…/mnemosyne-cli, vault is /…/mnemosyne
    cli_root = Path(__file__).resolve().parent.parent
    sibling = cli_root.parent / "mnemosyne"
    if (sibling / CANONICAL_REL).exists():
        return sibling
    return None


@pytest.fixture(scope="module")
def canonical_json() -> dict:
    vault = _resolve_vault_root()
    if vault is None:
        pytest.fail(
            f"Cannot find vault canonical {CANONICAL_REL}. "
            f"Set $MNEMOSYNE_VAULT or clone mnemosyne next to mnemosyne-cli."
        )
    return json.loads((vault / CANONICAL_REL).read_text())


def test_canonical_file_exists():
    vault = _resolve_vault_root()
    assert vault is not None, f"Canonical {CANONICAL_REL} not found"
    assert (vault / CANONICAL_REL).is_file()


def test_versions_current(canonical_json):
    assert canonical_json.get("lastOnboardingVersion") == "2.1.144"
    assert canonical_json.get("lastReleaseNotesSeen") == "2.1.144"


def test_acknowledgement_flags(canonical_json):
    assert canonical_json.get("hasCompletedOnboarding") is True
    assert canonical_json.get("bypassPermissionsModeAccepted") is True
    assert canonical_json.get("effortCalloutDismissed") is True
    assert canonical_json.get("effortCalloutV2Dismissed") is True
