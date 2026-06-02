"""Integration test: wire-codebase-template.py equivalence guarantee (D-G2).

Proves that the canonical template in the mnemosyne vault produces EXACTLY the
same universal symlinks that setup_worktree_symlinks produces (and nothing else).

Template location strategy
--------------------------
1. If $MNEMOSYNE_VAULT is set, look there first.
2. Fall back to the known sibling checkout path used on Empiria developer machines:
   /home/owen/projects/empiria/mnemosyne/docs/reference/wire-codebase-template.py
3. If neither is found, skip with a clear message (robust in CI without the vault).

The test copies the template to a synthetic project directory so that
VAULT_PROJECT_PATH (Path(__file__).resolve().parent inside the template)
points at the synthetic project — exactly how Phase 50 seeds it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate the template
# ---------------------------------------------------------------------------

_TEMPLATE_RELATIVE_PATH = "docs/reference/wire-codebase-template.py"

_SIBLING_VAULT = Path("/home/owen/projects/empiria/mnemosyne")


def _find_template() -> Path | None:
    """Return the template path, or None if it cannot be found."""
    # Strategy 1: $MNEMOSYNE_VAULT env var
    env_vault = os.environ.get("MNEMOSYNE_VAULT")
    if env_vault:
        candidate = Path(env_vault) / _TEMPLATE_RELATIVE_PATH
        if candidate.is_file():
            return candidate

    # Strategy 2: known sibling checkout
    candidate = _SIBLING_VAULT / _TEMPLATE_RELATIVE_PATH
    if candidate.is_file():
        return candidate

    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def template_file() -> Path:
    """Return the template path, or skip if unreachable."""
    found = _find_template()
    if found is None:
        pytest.skip(
            "wire-codebase-template.py not found. "
            "Set $MNEMOSYNE_VAULT or ensure the sibling vault is checked out at "
            f"{_SIBLING_VAULT}"
        )
    return found


@pytest.fixture
def synthetic_project(tmp_path: Path) -> Path:
    """Build a synthetic operational-home vault project directory.

    Layout:
        tmp_path/vault_project/
            gsd-planning/       (the directory .planning will point to)
            AGENTS.md           (triggers AGENTS.md + CLAUDE.md symlinks)
            wire-codebase.py    (copy of the template — seeded here by the test)

    Returns the vault_project path.
    """
    vault_project = tmp_path / "vault_project"
    (vault_project / "gsd-planning").mkdir(parents=True)
    (vault_project / "AGENTS.md").write_text(
        "# Synthetic AGENTS.md for test\n", encoding="utf-8"
    )
    return vault_project


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    """Build a minimal application repo directory."""
    repo = tmp_path / "app_repo"
    repo.mkdir()
    return repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_template_produces_universal_symlinks(
    template_file: Path,
    synthetic_project: Path,
    app_repo: Path,
) -> None:
    """Running the template creates all three universal symlinks in the app repo.

    Equivalence assertions (D-G2):
      1. (app_repo / '.planning').resolve() == (project / 'gsd-planning').resolve()
      2. (app_repo / 'AGENTS.md').resolve() == (project / 'AGENTS.md').resolve()
      3. os.readlink(app_repo / 'CLAUDE.md') == "AGENTS.md"  (Pitfall 6 — relative)
      4. (app_repo / '.claude') does NOT exist  (D-C2 — no overlay)
    """
    # Copy the template into the synthetic project so VAULT_PROJECT_PATH resolves
    # to the synthetic project directory (exactly how Phase 50 seeds it).
    seeded_script = synthetic_project / "wire-codebase.py"
    shutil.copy2(template_file, seeded_script)

    # Run the template
    result = subprocess.run(
        [sys.executable, str(seeded_script), str(app_repo)],
        check=True,
        capture_output=True,
        text=True,
    )

    # 1. .planning resolves into the synthetic project's gsd-planning/
    planning_link = app_repo / ".planning"
    assert planning_link.is_symlink(), ".planning is not a symlink"
    assert planning_link.resolve() == (synthetic_project / "gsd-planning").resolve(), (
        f".planning resolved to {planning_link.resolve()!r}, "
        f"expected {(synthetic_project / 'gsd-planning').resolve()!r}"
    )

    # 2. AGENTS.md resolves into the synthetic project
    agents_link = app_repo / "AGENTS.md"
    assert agents_link.is_symlink(), "AGENTS.md is not a symlink"
    assert agents_link.resolve() == (synthetic_project / "AGENTS.md").resolve(), (
        f"AGENTS.md resolved to {agents_link.resolve()!r}, "
        f"expected {(synthetic_project / 'AGENTS.md').resolve()!r}"
    )

    # 3. CLAUDE.md is a RELATIVE symlink — os.readlink must return exactly "AGENTS.md"
    claude_link = app_repo / "CLAUDE.md"
    assert claude_link.is_symlink(), "CLAUDE.md is not a symlink"
    readlink_result = os.readlink(claude_link)
    assert readlink_result == "AGENTS.md", (
        f"CLAUDE.md os.readlink returned {readlink_result!r}; "
        "must be exactly 'AGENTS.md' (relative) — Pitfall 6"
    )

    # 4. No .claude/ overlay directory was created (D-C2)
    assert not (app_repo / ".claude").exists(), (
        ".claude/ was created by the template — it must not create the Empiria overlay"
    )


def test_template_omits_agents_when_not_present(
    template_file: Path,
    tmp_path: Path,
) -> None:
    """When the synthetic project has no AGENTS.md, neither AGENTS.md nor CLAUDE.md
    is created in the app repo — only .planning is wired."""
    # Project WITHOUT an AGENTS.md
    project = tmp_path / "project_no_agents"
    (project / "gsd-planning").mkdir(parents=True)
    # Deliberately no AGENTS.md

    seeded_script = project / "wire-codebase.py"
    shutil.copy2(template_file, seeded_script)

    repo = tmp_path / "repo"
    repo.mkdir()

    subprocess.run(
        [sys.executable, str(seeded_script), str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert (repo / ".planning").is_symlink(), ".planning must still be created"
    assert not (repo / "AGENTS.md").exists(), "AGENTS.md must not be created without source"
    assert not (repo / "CLAUDE.md").exists(), "CLAUDE.md must not be created without AGENTS.md"
    assert not (repo / ".claude").exists(), ".claude/ must not be created"


def test_template_wrong_argc_exits_nonzero(template_file: Path, tmp_path: Path) -> None:
    """Invoking the template with wrong number of arguments exits non-zero."""
    seeded_script = tmp_path / "wire-codebase.py"
    shutil.copy2(template_file, seeded_script)

    result = subprocess.run(
        [sys.executable, str(seeded_script)],  # missing repo arg
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "Expected non-zero exit with missing argument"
