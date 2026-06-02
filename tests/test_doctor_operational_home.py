"""Tests for the 'Operational Home' doctor check category (D-E1/E2/E3).

All assertions go through doctor._build_checks() filtered by category —
never doctor.run() (Pitfall 1: host-state leakage).
"""

from __future__ import annotations

import tomli_w
from pathlib import Path

from mnemosyne_cli.commands import doctor
from mnemosyne_cli.lib import vault as lib_vault


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_toml(tmp_path: Path, vaults: dict[str, str]) -> Path:
    """Write a synthetic config.toml with the given vault name->path entries."""
    config_path = tmp_path / "config.toml"
    data: dict = {}
    if vaults:
        data["vaults"] = {
            name: {"path": str(path), "description": "", "sync": "git"}
            for name, path in vaults.items()
        }
    config_path.write_bytes(tomli_w.dumps(data).encode())
    return config_path


def _setup_env(tmp_path: Path):
    """Build a minimal vault + project under tmp_path.

    Returns (vault_path, project_path, vault_project_path).
    The vault is named 'empiria'; has a projects/testorg/testproj directory.
    """
    vault_path = tmp_path / "empiria-vault"
    vault_path.mkdir(parents=True)
    (vault_path / ".git" / "info").mkdir(parents=True)

    project_path = tmp_path / "project"
    project_path.mkdir(parents=True)
    git_dir = project_path / ".git"
    (git_dir / "info").mkdir(parents=True)

    vault_project_path = vault_path / "projects" / "testorg" / "testproj"
    (vault_project_path / "gsd-planning").mkdir(parents=True)
    (vault_project_path / "claude-config").mkdir(parents=True)
    (vault_project_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    return vault_path, project_path, vault_project_path


def _make_oh_vault(tmp_path: Path, name: str) -> Path:
    """Create a synthetic operational-home vault directory."""
    oh_vault_path = tmp_path / name
    oh_vault_path.mkdir(parents=True, exist_ok=True)
    return oh_vault_path


def _write_engagement_note(vault_project_path: Path, oh: dict | None = None) -> None:
    """Write the empiria-side engagement record .md file with optional operational_home."""
    slug = vault_project_path.name  # 'testproj'
    lines = ["---\n"]
    if oh is not None:
        lines.append("operational_home:\n")
        lines.append(f"  vault: {oh['vault']}\n")
        lines.append(f"  path: {oh['path']}\n")
    lines.append("---\n\n# Test project\n")
    (vault_project_path / f"{slug}.md").write_text("".join(lines), encoding="utf-8")


def _get_oh_checks(
    project_path: Path,
    vault_path: Path,
    *,
    plan_wired: bool = True,
):
    """Return only the 'Operational Home' checks from _build_checks."""
    if plan_wired:
        planning_link = project_path / ".planning"
        if not planning_link.exists() and not planning_link.is_symlink():
            vault_project_path = vault_path / "projects" / "testorg" / "testproj"
            planning_link.symlink_to(vault_project_path / "gsd-planning")

    git_dir = project_path / ".git"
    checks = doctor._build_checks(project_path, vault_path, git_dir)
    return [c for c in checks if c.category == "Operational Home"]


# ---------------------------------------------------------------------------
# Test 1: Unregistered OH vault → not ok, fix_cmd contains 'mnemosyne vault add'
# ---------------------------------------------------------------------------


def test_oh_unregistered_vault_fails(tmp_path, monkeypatch):
    """D-E1: when operational_home.vault is not registered, doctor FAILs.

    The check must not be ok and fix_cmd must contain 'mnemosyne vault add'.
    """
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Write engagement note referencing an unregistered OH vault
    _write_engagement_note(
        vault_project_path,
        oh={"vault": "friendly-fox-vault", "path": "projects/infinite-worlds"},
    )

    # Config has empiria but NOT friendly-fox-vault
    config_path = _make_config_toml(tmp_path, {"empiria": str(vault_path)})
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    oh_checks = _get_oh_checks(project_path, vault_path)
    assert oh_checks, "Expected Operational Home checks to be present"

    results = [(c.name, c.check()) for c in oh_checks]
    # At least one check must fail
    assert any(not r.ok for _, r in results), (
        f"Expected a failing OH check for unregistered vault; got {results}"
    )
    # The failing check must have a fix_cmd mentioning 'mnemosyne vault add'
    fail_results = [(n, r) for n, r in results if not r.ok]
    assert any(
        r.fix_cmd is not None and "mnemosyne vault add" in r.fix_cmd
        for _, r in fail_results
    ), f"Expected fix_cmd with 'mnemosyne vault add' in failures: {fail_results}"

    # The failing message must mention "not registered"
    assert any(
        "not registered" in r.message
        for _, r in fail_results
    ), f"Expected 'not registered' in failure message: {fail_results}"


# ---------------------------------------------------------------------------
# Test 2: Registered OH vault + present dir + wire-codebase.py + symlinks +
#         .gitignore listing → all OH checks ok
# ---------------------------------------------------------------------------


def test_oh_all_green(tmp_path, monkeypatch):
    """D-E2: fully wired OH project passes all Operational Home checks."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)
    oh_vault_path = _make_oh_vault(tmp_path, "friendly-fox-vault")

    # Create the OH project directory + wire-codebase.py
    oh_project_path = oh_vault_path / "projects" / "infinite-worlds"
    oh_project_path.mkdir(parents=True)
    (oh_project_path / "wire-codebase.py").write_text(
        "# wire-codebase.py stub\n", encoding="utf-8"
    )

    # Write engagement note
    _write_engagement_note(
        vault_project_path,
        oh={"vault": "friendly-fox-vault", "path": "projects/infinite-worlds"},
    )

    # Register both vaults
    config_path = _make_config_toml(
        tmp_path,
        {
            "empiria": str(vault_path),
            "friendly-fox-vault": str(oh_vault_path),
        },
    )
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    # Wire .planning symlink → OH vault project (D-E2 symlink resolution)
    planning_target = oh_project_path / "gsd-planning"
    planning_target.mkdir(parents=True)
    planning_link = project_path / ".planning"
    planning_link.symlink_to(planning_target)

    # Wire AGENTS.md → OH vault project
    (oh_project_path / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    (project_path / "AGENTS.md").symlink_to(oh_project_path / "AGENTS.md")

    # .gitignore lists .planning and AGENTS.md (D-C5)
    (project_path / ".gitignore").write_text(".planning\nAGENTS.md\n", encoding="utf-8")

    git_dir = project_path / ".git"
    checks = doctor._build_checks(project_path, vault_path, git_dir)
    oh_checks = [c for c in checks if c.category == "Operational Home"]

    assert oh_checks, "Expected Operational Home checks to be present"
    results = [(c.name, c.check()) for c in oh_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert not failures, f"Expected all OH checks to pass; failures: {failures}"


# ---------------------------------------------------------------------------
# Test 3: operational_home UNSET → OH checks return ok "empiria-resident"
# ---------------------------------------------------------------------------


def test_oh_unset_skips_gracefully(tmp_path, monkeypatch):
    """When operational_home is not set on the project, all OH checks skip (ok=True)."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Write engagement note WITHOUT operational_home
    _write_engagement_note(vault_project_path, oh=None)

    config_path = _make_config_toml(tmp_path, {"empiria": str(vault_path)})
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    oh_checks = _get_oh_checks(project_path, vault_path)
    assert oh_checks, "Expected Operational Home checks even when operational_home is unset"

    results = [(c.name, c.check()) for c in oh_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert not failures, (
        f"Expected all OH checks to pass (skip) when operational_home is unset; failures: {failures}"
    )


# ---------------------------------------------------------------------------
# Test 4: empiria unregistered → empiria-specific checks skip (D-E3)
# ---------------------------------------------------------------------------


def test_oh_empiria_unregistered_skips(tmp_path, monkeypatch):
    """D-E3: when empiria vault is not registered, OH checks skip gracefully (ok=True)."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)

    # Write engagement note WITH operational_home
    _write_engagement_note(
        vault_project_path,
        oh={"vault": "friendly-fox-vault", "path": "projects/infinite-worlds"},
    )

    # Config has NO registered vaults at all (empiria absent)
    config_path = _make_config_toml(tmp_path, {})
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    # Note: vault_path is our empiria vault; when no vaults registered,
    # read_operational_home may not be called (no project_rel derived).
    # We create the .planning symlink so doctor can attempt project resolution.
    planning_link = project_path / ".planning"
    if not planning_link.exists():
        planning_link.symlink_to(vault_project_path / "gsd-planning")

    git_dir = project_path / ".git"
    checks = doctor._build_checks(project_path, vault_path, git_dir)
    oh_checks = [c for c in checks if c.category == "Operational Home"]

    assert oh_checks, "Expected Operational Home checks even when empiria unregistered"
    results = [(c.name, c.check()) for c in oh_checks]
    failures = [(n, r) for n, r in results if not r.ok]
    assert not failures, (
        f"Expected graceful skip (ok=True) when empiria unregistered; failures: {failures}"
    )


# ---------------------------------------------------------------------------
# Test 5: path-traversal oh.path → failing check
# ---------------------------------------------------------------------------


def test_oh_path_traversal_fails(tmp_path, monkeypatch):
    """Security V5/V12: an oh.path that escapes the OH vault root is flagged."""
    vault_path, project_path, vault_project_path = _setup_env(tmp_path)
    oh_vault_path = _make_oh_vault(tmp_path, "friendly-fox-vault")

    # Use a path-traversal oh.path that escapes the OH vault
    traversal_path = "../../outside"

    _write_engagement_note(
        vault_project_path,
        oh={"vault": "friendly-fox-vault", "path": traversal_path},
    )

    # Register both vaults so the OH vault lookup succeeds
    config_path = _make_config_toml(
        tmp_path,
        {
            "empiria": str(vault_path),
            "friendly-fox-vault": str(oh_vault_path),
        },
    )
    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_path)

    oh_checks = _get_oh_checks(project_path, vault_path)
    assert oh_checks, "Expected Operational Home checks"

    results = [(c.name, c.check()) for c in oh_checks]
    # At least one check must fail (path traversal / dir not found)
    assert any(not r.ok for _, r in results), (
        f"Expected a failing check for path-traversal oh.path; got {results}"
    )
