"""Shared fixtures for mnemosyne-cli tests."""

from __future__ import annotations

from pathlib import Path

import pytest


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
