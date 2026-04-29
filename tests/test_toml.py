"""DEP-06: container.toml TOML schema parses correctly."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_container_toml_schema(mock_container_toml: Path) -> None:
    """All five dependency sections are present and values are correct."""
    with mock_container_toml.open("rb") as f:
        data = tomllib.load(f)

    deps = data["dependencies"]
    assert "apt" in deps
    assert "pip" in deps
    assert "npm" in deps
    assert "cargo" in deps
    assert "run" in deps
    assert deps["run"] == ["playwright install chromium"]
