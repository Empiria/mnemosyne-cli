"""Shared fixtures for mnemosyne-cli tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def shallow_clone_run(upstream_head: str):
    """Build a fake `subprocess.run` that behaves like a real shallow clone.

    `rev-parse HEAD` yields *upstream_head* after a clone, and only after the
    pinned ref has been fetched and checked out does it yield that pin. Vendoring
    refuses to sync when a hex pin does not resolve to itself, so a mock that
    always returns the same sha makes every entry look force-pushed.
    """
    state: dict[str, str | None] = {"fetched_pin": None, "head": upstream_head}

    def fake_run(args, *a, **k):
        if "clone" in args:
            state["fetched_pin"] = None
            state["head"] = upstream_head
        elif "fetch" in args and "origin" in args:
            state["fetched_pin"] = args[-1]
        elif "checkout" in args:
            state["head"] = state["fetched_pin"] or upstream_head
        elif "rev-parse" in args:
            return MagicMock(returncode=0, stdout=str(state["head"]) + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return fake_run


@pytest.fixture(autouse=True)
def isolated_home(request, tmp_path_factory, monkeypatch) -> Path | None:
    """Point Path.home() at a throwaway directory for every test.

    init and doctor both reconcile ``~/.claude/skills/`` against the vault. With
    the real home in scope a test that wires a tmp_path vault will link its
    fixture skills into the operator's actual skill surface, and clobber the
    real symlink wherever a fixture reuses a genuine skill name. Tests that need
    to assert on the home directory take this fixture and read from it.

    Mark a test ``@pytest.mark.real_home`` when it genuinely needs the operator
    home, as the tilde-expansion tests in test_components.py do.
    """
    if request.node.get_closest_marker("real_home"):
        return None
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Create a minimal vault layout with a container.toml for infinite-worlds."""
    vault = tmp_path / "vault"
    project_dir = vault / "projects" / "friendly-fox" / "infinite-worlds"
    project_dir.mkdir(parents=True)

    toml_content = """\
[dependencies]
apt = ["chromium"]
pip = ["pytest-playwright"]
npm = ["typescript@5"]
cargo = ["cargo-watch"]
run = ["playwright install chromium"]
"""
    (project_dir / "container.toml").write_text(toml_content)
    return vault


@pytest.fixture
def mock_container_toml(vault_dir: Path) -> Path:
    """Return the path to the container.toml created by vault_dir."""
    return vault_dir / "projects" / "friendly-fox" / "infinite-worlds" / "container.toml"
