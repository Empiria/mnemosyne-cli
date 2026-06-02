"""Integration tests for mnemosyne init — operational_home branch (D-D2).

These tests exercise the _run_host OH branch added in Plan 47-04:
  - Happy path: wire-codebase.py is invoked, overlay is layered, exclusions split (D-C5).
  - Unregistered OH vault: typer.Exit(1) with message containing "mnemosyne vault add".
  - Missing wire-codebase.py: typer.Exit(1) naming the expected path.
  - Path-traversal oh.path: typer.Exit(1) before any subprocess.
  - Unset operational_home (D-D1): falls through to today's path (exclusions in .git/info/exclude,
    NOT in .gitignore).

Uses synthetic tmp-dir fixtures — no real vault, no host state, no doctor.run() (Pitfall 1).

Fixture approach:
  - empiria vault:   tmp_path/empiria/   with projects/friendly-fox/infinite-worlds/infinite-worlds.md
  - OH vault:        tmp_path/oh-vault/  with projects/infinite-worlds/wire-codebase.py
  - app repo:        tmp_path/app/       with .git/info/ for exclusions
  - config.toml:     tmp_path/config.toml  (patched via monkeypatch on lib_vault._CONFIG_PATH)
"""

from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import click
import pytest
import typer

from mnemosyne_cli.commands import init
from mnemosyne_cli.lib import git as lib_git
from mnemosyne_cli.lib import vault as lib_vault

# typer.Exit raises click.exceptions.Exit (RuntimeError subclass, not SystemExit).
# Both are possible depending on how typer/click dispatch the exit.
_EXIT_EXCEPTIONS = (SystemExit, click.exceptions.Exit)

# ---------------------------------------------------------------------------
# Location of the canonical wire-codebase-template.py
# ---------------------------------------------------------------------------

_VAULT_SIBLING = Path("/home/owen/projects/empiria/mnemosyne")
_TEMPLATE_REL = "docs/reference/wire-codebase-template.py"


def _find_template() -> Path | None:
    """Return the wire-codebase-template.py path, or None if absent."""
    import os

    env_vault = os.environ.get("MNEMOSYNE_VAULT")
    if env_vault:
        candidate = Path(env_vault) / _TEMPLATE_REL
        if candidate.is_file():
            return candidate
    candidate = _VAULT_SIBLING / _TEMPLATE_REL
    if candidate.is_file():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Shared environment builder
# ---------------------------------------------------------------------------


def _build_env(tmp_path: Path, *, oh_vault_name: str = "oh-vault"):
    """Build the synthetic multi-vault environment.

    Returns:
        (empiria_vault, oh_vault, app_repo, config_toml_path,
         vault_project_path, oh_project_path)

    Layout:
        empiria/projects/friendly-fox/infinite-worlds/
            gsd-planning/
            claude-config/settings.json  (so setup_claude_overlay has something to link)
            infinite-worlds.md           (frontmatter: operational_home: {vault, path})
        oh-vault/projects/infinite-worlds/
            gsd-planning/
            AGENTS.md
            (wire-codebase.py added per-test)
        app/
            .git/info/
    """
    empiria = tmp_path / "empiria"
    oh_vault = tmp_path / oh_vault_name
    app = tmp_path / "app"

    # empiria vault project
    vault_proj = empiria / "projects" / "friendly-fox" / "infinite-worlds"
    (vault_proj / "gsd-planning").mkdir(parents=True)
    claude_config = vault_proj / "claude-config"
    claude_config.mkdir(parents=True)
    # Minimal settings.json so setup_claude_overlay creates the symlink
    (claude_config / "settings.json").write_text("{}", encoding="utf-8")

    # empiria engagement record with operational_home
    (vault_proj / "infinite-worlds.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            tags: [project]
            operational_home:
              vault: {oh_vault_name}
              path: projects/infinite-worlds
            ---
            # Infinite Worlds
            """
        ),
        encoding="utf-8",
    )

    # OH vault project
    oh_proj = oh_vault / "projects" / "infinite-worlds"
    (oh_proj / "gsd-planning").mkdir(parents=True)
    (oh_proj / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    # App repo with .git/info/
    app.mkdir(parents=True)
    (app / ".git" / "info").mkdir(parents=True)

    # config.toml registering both vaults
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(
        textwrap.dedent(
            f"""\
            vault_path = "{empiria}"

            [vaults.empiria]
            path = "{empiria}"
            description = "Empiria vault"
            sync = "git"

            [vaults.{oh_vault_name}]
            path = "{oh_vault}"
            description = "OH vault"
            sync = "git"
            """
        ),
        encoding="utf-8",
    )

    return (
        empiria,
        oh_vault,
        app,
        config_toml,
        vault_proj,  # empiria vault project
        oh_proj,     # oh vault project
    )


def _get_wire_codebase_template() -> Path:
    """Return the template path, or skip if unavailable."""
    found = _find_template()
    if found is None:
        pytest.skip(
            "wire-codebase-template.py not found. "
            "Set $MNEMOSYNE_VAULT or ensure vault at "
            f"{_VAULT_SIBLING}"
        )
    return found


# ---------------------------------------------------------------------------
# Test 1 — Happy path: D-D2 all steps, D-C5 exclusion split
# ---------------------------------------------------------------------------


def test_init_oh_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: wire-codebase.py runs, overlay created, exclusions split (D-C5).

    After init:
    - app/.planning, app/AGENTS.md, app/CLAUDE.md exist (created by wire-codebase.py)
    - app/.claude/settings.json exists (created by setup_claude_overlay)
    - app/.gitignore contains ".planning" and "AGENTS.md" (D-C5 tracked)
    - app/.git/info/exclude contains ".claude/settings.json" (D-C5 per-clone)
    - app/.git/info/exclude does NOT contain ".planning" or "AGENTS.md" (D-C5 split)
    - app/.gitignore does NOT contain ".claude/settings.json"
    """
    template = _get_wire_codebase_template()

    empiria, oh_vault, app, config_toml, vault_proj, oh_proj = _build_env(tmp_path)
    # Seed the wire-codebase.py into the oh vault project
    shutil.copy2(template, oh_proj / "wire-codebase.py")

    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_toml)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=empiria),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=app / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=app),
    ):
        init.run(project="projects/friendly-fox/infinite-worlds")

    # Universal symlinks created by wire-codebase.py
    assert (app / ".planning").is_symlink(), ".planning should be a symlink"
    assert (app / "AGENTS.md").is_symlink(), "AGENTS.md should be a symlink"
    assert (app / "CLAUDE.md").is_symlink(), "CLAUDE.md should be a symlink"

    # Empiria overlay created by setup_claude_overlay
    assert (app / ".claude" / "settings.json").is_symlink() or (
        app / ".claude" / "settings.json"
    ).exists(), ".claude/settings.json should exist (overlay)"

    # D-C5: .planning and AGENTS.md in tracked .gitignore
    assert lib_git.check_gitignore_entry(".planning", app), (
        ".planning must be in tracked .gitignore (D-C5)"
    )
    assert lib_git.check_gitignore_entry("AGENTS.md", app), (
        "AGENTS.md must be in tracked .gitignore (D-C5)"
    )

    # D-C5: .claude/* in .git/info/exclude (per-clone)
    git_dir = app / ".git"
    assert lib_git.check_git_exclusion(".claude/settings.json", git_dir), (
        ".claude/settings.json must be in .git/info/exclude (D-C5)"
    )

    # D-C5: .planning and AGENTS.md NOT in .git/info/exclude
    assert not lib_git.check_git_exclusion(".planning", git_dir), (
        ".planning must NOT be in .git/info/exclude when operational_home is set (D-C5)"
    )
    assert not lib_git.check_git_exclusion("AGENTS.md", git_dir), (
        "AGENTS.md must NOT be in .git/info/exclude when operational_home is set (D-C5)"
    )

    # D-C5: .claude/settings.json NOT in tracked .gitignore
    assert not lib_git.check_gitignore_entry(".claude/settings.json", app), (
        ".claude/settings.json must NOT be in tracked .gitignore (D-C5)"
    )


# ---------------------------------------------------------------------------
# Test 2 — Unregistered OH vault → FAIL with "mnemosyne vault add" message
# ---------------------------------------------------------------------------


def test_init_oh_unregistered_vault_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the OH vault is not registered in config.toml, init FAILs (D-E1).

    The error message must contain "mnemosyne vault add" so the operator knows
    the fix command.
    """
    empiria, _oh_vault, app, config_toml, vault_proj, _oh_proj = _build_env(
        tmp_path, oh_vault_name="oh-vault"
    )
    # Write a config that ONLY has the empiria vault — oh-vault is absent
    config_toml.write_text(
        textwrap.dedent(
            f"""\
            vault_path = "{empiria}"

            [vaults.empiria]
            path = "{empiria}"
            description = "Empiria vault"
            sync = "git"
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_toml)

    captured_messages: list[str] = []

    def _capturing_print(*args, **kwargs):
        captured_messages.append(str(args))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=empiria),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=app / ".git"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=app),
        patch("mnemosyne_cli.commands.init.error_console.print", side_effect=_capturing_print),
    ):
        with pytest.raises(_EXIT_EXCEPTIONS) as exc_info:
            init.run(project="projects/friendly-fox/infinite-worlds")

    exit_code = exc_info.value.code if hasattr(exc_info.value, "code") else None
    assert exit_code != 0, f"Expected non-zero exit, got code={exit_code!r}"
    all_messages = " ".join(captured_messages)
    assert "mnemosyne vault add" in all_messages, (
        f"Expected 'mnemosyne vault add' in error output. Got: {all_messages!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Missing wire-codebase.py → FAIL naming the expected path
# ---------------------------------------------------------------------------


def test_init_oh_missing_wire_script_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When wire-codebase.py is absent, init FAILs and names the expected path."""
    empiria, oh_vault, app, config_toml, vault_proj, oh_proj = _build_env(tmp_path)
    # Deliberately do NOT seed the wire-codebase.py

    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_toml)

    captured_messages: list[str] = []

    def _capturing_print(*args, **kwargs):
        captured_messages.append(str(args))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=empiria),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=app / ".git"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=app),
        patch("mnemosyne_cli.commands.init.error_console.print", side_effect=_capturing_print),
    ):
        with pytest.raises(_EXIT_EXCEPTIONS) as exc_info:
            init.run(project="projects/friendly-fox/infinite-worlds")

    exit_code = exc_info.value.code if hasattr(exc_info.value, "code") else None
    assert exit_code != 0, f"Expected non-zero exit, got code={exit_code!r}"
    all_messages = " ".join(captured_messages)
    assert "wire-codebase.py" in all_messages, (
        f"Expected 'wire-codebase.py' to be named in error output. Got: {all_messages!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Path-traversal oh.path → FAIL before any subprocess
# ---------------------------------------------------------------------------


def test_init_oh_path_traversal_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oh.path that escapes the OH vault root is rejected before subprocess runs (V5/V12)."""
    empiria, oh_vault, app, config_toml, vault_proj, oh_proj = _build_env(tmp_path)

    # Override the engagement record with a path-traversal oh.path
    (vault_proj / "infinite-worlds.md").write_text(
        textwrap.dedent(
            """\
            ---
            tags: [project]
            operational_home:
              vault: oh-vault
              path: ../../etc
            ---
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_toml)

    subprocess_calls: list = []

    original_subprocess_run = __import__("subprocess").run

    def _spy_subprocess_run(cmd, **kwargs):
        subprocess_calls.append(cmd)
        return original_subprocess_run(cmd, **kwargs)

    captured_messages: list[str] = []

    def _capturing_print(*args, **kwargs):
        captured_messages.append(str(args))

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=empiria),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=app / ".git"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=app),
        patch("mnemosyne_cli.commands.init.error_console.print", side_effect=_capturing_print),
        patch("subprocess.run", side_effect=_spy_subprocess_run),
    ):
        with pytest.raises(_EXIT_EXCEPTIONS) as exc_info:
            init.run(project="projects/friendly-fox/infinite-worlds")

    exit_code = exc_info.value.code if hasattr(exc_info.value, "code") else None
    assert exit_code != 0, f"Expected non-zero exit on path-traversal, got code={exit_code!r}"
    # The subprocess must NOT have been called with the wire-codebase script
    wire_calls = [
        c for c in subprocess_calls
        if isinstance(c, list) and any("wire-codebase" in str(x) for x in c)
    ]
    assert not wire_calls, (
        f"Subprocess should not be called with wire-codebase.py on path-traversal. "
        f"Got calls: {subprocess_calls!r}"
    )
    # Also verify that no symlinks were created in the app repo
    assert not (app / ".planning").exists(), ".planning must not be created on path-traversal"


# ---------------------------------------------------------------------------
# Test 5 — Unset operational_home (D-D1): today's path unchanged
# ---------------------------------------------------------------------------


def test_init_unset_oh_uses_today_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When operational_home is absent, init falls through to today's path (D-D1).

    Specifically:
    - .planning, AGENTS.md, CLAUDE.md are created (by setup_worktree_symlinks)
    - .planning, AGENTS.md, CLAUDE.md are in .git/info/exclude (today's behaviour)
    - .planning and AGENTS.md are NOT in tracked .gitignore
    """
    empiria = tmp_path / "empiria"
    app = tmp_path / "app"

    # Vault project WITHOUT operational_home
    vault_proj = empiria / "projects" / "testorg" / "testproj"
    (vault_proj / "gsd-planning").mkdir(parents=True)
    (vault_proj / "claude-config").mkdir(parents=True)
    (vault_proj / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    # engagement record with NO operational_home field
    (vault_proj / "testproj.md").write_text(
        "---\ntags: [project]\n---\n# Test Project\n", encoding="utf-8"
    )

    app.mkdir(parents=True)
    (app / ".git" / "info").mkdir(parents=True)

    # Config without OH vault
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(
        textwrap.dedent(
            f"""\
            vault_path = "{empiria}"

            [vaults.empiria]
            path = "{empiria}"
            description = "Empiria vault"
            sync = "git"
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(lib_vault, "_CONFIG_PATH", config_toml)

    with (
        patch("mnemosyne_cli.commands.init.lib_vault.resolve_vault_path", return_value=empiria),
        patch("mnemosyne_cli.commands.init.lib_git.get_git_dir", return_value=app / ".git"),
        patch("mnemosyne_cli.commands.init.lib_git.register_merge_drivers"),
        patch("mnemosyne_cli.commands.init.lib_envrc.set_envrc_vault", return_value=True),
        patch("mnemosyne_cli.commands.init.Path.cwd", return_value=app),
    ):
        init.run(project="projects/testorg/testproj")

    git_dir = app / ".git"

    # Today's path: all entries go to .git/info/exclude
    assert lib_git.check_git_exclusion(".planning", git_dir), (
        ".planning must be in .git/info/exclude for unset operational_home (D-D1)"
    )
    assert lib_git.check_git_exclusion("AGENTS.md", git_dir), (
        "AGENTS.md must be in .git/info/exclude for unset operational_home (D-D1)"
    )
    assert lib_git.check_git_exclusion("CLAUDE.md", git_dir), (
        "CLAUDE.md must be in .git/info/exclude for unset operational_home (D-D1)"
    )

    # Today's path: .planning and AGENTS.md are NOT in tracked .gitignore
    assert not lib_git.check_gitignore_entry(".planning", app), (
        ".planning must NOT be in tracked .gitignore for unset operational_home (D-D1)"
    )
    assert not lib_git.check_gitignore_entry("AGENTS.md", app), (
        "AGENTS.md must NOT be in tracked .gitignore for unset operational_home (D-D1)"
    )
