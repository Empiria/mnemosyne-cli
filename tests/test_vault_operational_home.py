"""Unit tests for operational_home parsing helpers (lib/vault.py + lib/git.py).

Wave 0 host-independent tests:
- read_operational_home: absent/missing-note/malformed/valid
- vault_by_name: registered/unregistered
- validate_vault_rules: consistent/orphan-from/unregistered-can_read/empty-registry
- is_within: containment guard (../escape rejected, sub-path accepted)
- add_gitignore_entry / check_gitignore_entry: idempotent create/check

All tests use tmp_path + monkeypatched _CONFIG_PATH. No host state is read.
No doctor.run() calls — these are pure lib units.
"""

from __future__ import annotations

import tomli_w
import pytest
from pathlib import Path

import mnemosyne_cli.lib.vault as lib_vault
import mnemosyne_cli.lib.git as lib_git


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write a config.toml to tmp_path and return its path."""
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps(data).encode())
    return cfg


def _make_project_note(vault: Path, project_rel: str, frontmatter: str) -> Path:
    """Create a minimal vault project note with the given frontmatter."""
    project_dir = vault / project_rel
    project_dir.mkdir(parents=True, exist_ok=True)
    slug = project_dir.name
    note = project_dir / f"{slug}.md"
    note.write_text(f"---\n{frontmatter}---\n# Project\n", encoding="utf-8")
    return note


# ---------------------------------------------------------------------------
# read_operational_home
# ---------------------------------------------------------------------------


class TestReadOperationalHome:
    def test_absent_field_returns_none(self, tmp_path):
        """Field absent from frontmatter -> None (D-D1 unset branch)."""
        vault = tmp_path / "vault"
        _make_project_note(vault, "projects/friendly-fox/infinite-worlds", "tags: [project]\n")
        result = lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")
        assert result is None

    def test_missing_note_returns_none(self, tmp_path):
        """Note file does not exist -> None."""
        vault = tmp_path / "vault"
        (vault / "projects" / "friendly-fox" / "infinite-worlds").mkdir(parents=True)
        result = lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")
        assert result is None

    def test_malformed_field_not_a_dict_raises(self, tmp_path):
        """operational_home is a string (not a dict) -> ValueError (Open Q2 lock)."""
        vault = tmp_path / "vault"
        _make_project_note(
            vault,
            "projects/friendly-fox/infinite-worlds",
            "tags: [project]\noperational_home: just-a-string\n",
        )
        with pytest.raises(ValueError, match="operational_home"):
            lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")

    def test_malformed_field_missing_path_key_raises(self, tmp_path):
        """operational_home dict missing 'path' key -> ValueError."""
        vault = tmp_path / "vault"
        _make_project_note(
            vault,
            "projects/friendly-fox/infinite-worlds",
            "tags: [project]\noperational_home:\n  vault: friendly-fox-vault\n",
        )
        with pytest.raises(ValueError, match="operational_home"):
            lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")

    def test_malformed_field_missing_vault_key_raises(self, tmp_path):
        """operational_home dict missing 'vault' key -> ValueError."""
        vault = tmp_path / "vault"
        _make_project_note(
            vault,
            "projects/friendly-fox/infinite-worlds",
            "tags: [project]\noperational_home:\n  path: projects/infinite-worlds\n",
        )
        with pytest.raises(ValueError, match="operational_home"):
            lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")

    def test_valid_returns_operational_home(self, tmp_path):
        """Valid {vault, path} table -> OperationalHome with correct fields."""
        vault = tmp_path / "vault"
        _make_project_note(
            vault,
            "projects/friendly-fox/infinite-worlds",
            (
                "tags: [project]\n"
                "operational_home:\n"
                "  vault: friendly-fox-vault\n"
                "  path: projects/infinite-worlds\n"
            ),
        )
        result = lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")
        assert result is not None
        assert result.vault == "friendly-fox-vault"
        assert result.path == "projects/infinite-worlds"

    def test_structurally_broken_yaml_raises_value_error(self, tmp_path):
        """Structurally broken YAML frontmatter raises ValueError, not yaml.YAMLError (CR-01)."""
        vault = tmp_path / "vault"
        project_dir = vault / "projects" / "friendly-fox" / "infinite-worlds"
        project_dir.mkdir(parents=True, exist_ok=True)
        # Write a note with unclosed bracket — structurally invalid YAML
        note = project_dir / "infinite-worlds.md"
        note.write_text("---\npath: [unclosed\n---\n# Project\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid YAML"):
            lib_vault.read_operational_home(vault, "projects/friendly-fox/infinite-worlds")

    def test_slug_note_filename_convention(self, tmp_path):
        """Note path uses {dir}/{slug}.md convention (slug = directory basename)."""
        vault = tmp_path / "vault"
        # Create a note with a slug that matches the dir name
        _make_project_note(
            vault,
            "projects/my-org/my-project",
            (
                "tags: [project]\n"
                "operational_home:\n"
                "  vault: my-vault\n"
                "  path: projects/my-project\n"
            ),
        )
        result = lib_vault.read_operational_home(vault, "projects/my-org/my-project")
        assert result is not None
        assert result.vault == "my-vault"


# ---------------------------------------------------------------------------
# vault_by_name
# ---------------------------------------------------------------------------


class TestVaultByName:
    def test_registered_vault_returned(self, tmp_path, monkeypatch):
        """vault_by_name returns the registered VaultConfig for a known name."""
        cfg = _write_config(tmp_path, {
            "vaults": {
                "friendly-fox-vault": {"path": str(tmp_path / "ff"), "sync": "git"},
                "empiria": {"path": str(tmp_path / "emp"), "sync": "git"},
            }
        })
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        result = lib_vault.vault_by_name("friendly-fox-vault")
        assert result is not None
        assert result.name == "friendly-fox-vault"

    def test_unregistered_vault_returns_none(self, tmp_path, monkeypatch):
        """vault_by_name returns None for an unknown vault name."""
        cfg = _write_config(tmp_path, {
            "vaults": {
                "empiria": {"path": str(tmp_path / "emp"), "sync": "git"},
            }
        })
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        result = lib_vault.vault_by_name("nonexistent-vault")
        assert result is None

    def test_empty_config_returns_none(self, tmp_path, monkeypatch):
        """vault_by_name returns None when no vaults are registered."""
        cfg = _write_config(tmp_path, {})
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        result = lib_vault.vault_by_name("anything")
        assert result is None


# ---------------------------------------------------------------------------
# validate_vault_rules
# ---------------------------------------------------------------------------


class TestValidateVaultRules:
    def test_consistent_rules_returns_empty_list(self, tmp_path, monkeypatch):
        """All vault_rules reference registered vaults -> empty problem list."""
        cfg = _write_config(tmp_path, {
            "vaults": {
                "empiria": {"path": str(tmp_path / "emp"), "sync": "git"},
                "friendly-fox-vault": {"path": str(tmp_path / "ff"), "sync": "git"},
            },
            "vault_rules": [
                {"from": "empiria", "can_read": ["friendly-fox-vault"]},
            ],
        })
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        problems = lib_vault.validate_vault_rules()
        assert problems == []

    def test_orphan_from_is_problem(self, tmp_path, monkeypatch):
        """A vault_rules 'from' that names an unregistered vault -> problem listed."""
        cfg = _write_config(tmp_path, {
            "vaults": {
                "empiria": {"path": str(tmp_path / "emp"), "sync": "git"},
            },
            "vault_rules": [
                {"from": "orphan-vault", "can_read": ["empiria"]},
            ],
        })
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        problems = lib_vault.validate_vault_rules()
        assert any("orphan" in p.lower() and "orphan-vault" in p for p in problems)

    def test_unregistered_can_read_is_problem(self, tmp_path, monkeypatch):
        """A can_read entry naming an unregistered vault -> problem listed."""
        cfg = _write_config(tmp_path, {
            "vaults": {
                "empiria": {"path": str(tmp_path / "emp"), "sync": "git"},
            },
            "vault_rules": [
                {"from": "empiria", "can_read": ["unregistered-target"]},
            ],
        })
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        problems = lib_vault.validate_vault_rules()
        assert any("unregistered-target" in p for p in problems)

    def test_empty_registry_returns_empty_list(self, tmp_path, monkeypatch):
        """No [vaults.*] registered -> [] (skip; D-F1 empty-registry case)."""
        cfg = _write_config(tmp_path, {
            "vault_rules": [
                {"from": "some-vault", "can_read": ["other-vault"]},
            ],
        })
        monkeypatch.setattr(lib_vault, "_CONFIG_PATH", cfg)

        problems = lib_vault.validate_vault_rules()
        assert problems == []


# ---------------------------------------------------------------------------
# is_within (path-traversal containment guard)
# ---------------------------------------------------------------------------


class TestIsWithin:
    def test_clean_sub_path_returns_true(self, tmp_path):
        """A path inside the root -> True."""
        root = tmp_path / "vault"
        root.mkdir()
        candidate = root / "projects" / "infinite-worlds"
        candidate.mkdir(parents=True)
        assert lib_vault.is_within(root, candidate) is True

    def test_escape_via_dotdot_returns_false(self, tmp_path):
        """A path that resolves outside the root via ../  -> False."""
        root = tmp_path / "vault"
        root.mkdir()
        escape = tmp_path / "escape"
        escape.mkdir()
        # Build a candidate that goes outside via symlink-resolving path
        assert lib_vault.is_within(root, escape) is False

    def test_path_traversal_in_component_returns_false(self, tmp_path):
        """oh.path of '../escape' resolves outside root -> False (Security V5/V12)."""
        root = tmp_path / "vault"
        root.mkdir()
        # Simulate what join(root, "../escape").resolve() produces
        candidate = (root / ".." / "escaped").resolve()
        assert lib_vault.is_within(root, candidate) is False

    def test_root_itself_returns_true(self, tmp_path):
        """The root itself is within itself -> True."""
        root = tmp_path / "vault"
        root.mkdir()
        assert lib_vault.is_within(root, root) is True


# ---------------------------------------------------------------------------
# add_gitignore_entry / check_gitignore_entry
# ---------------------------------------------------------------------------


class TestGitignoreHelpers:
    def test_check_returns_false_when_no_file(self, tmp_path):
        """check_gitignore_entry returns False when .gitignore does not exist."""
        assert lib_git.check_gitignore_entry(".planning", tmp_path) is False

    def test_add_creates_file_and_entry(self, tmp_path):
        """add_gitignore_entry creates .gitignore and adds the entry."""
        lib_git.add_gitignore_entry(".planning", tmp_path)
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".planning" in gitignore.read_text().splitlines()

    def test_check_returns_true_after_add(self, tmp_path):
        """check_gitignore_entry returns True after the entry has been added."""
        lib_git.add_gitignore_entry("AGENTS.md", tmp_path)
        assert lib_git.check_gitignore_entry("AGENTS.md", tmp_path) is True

    def test_add_is_idempotent(self, tmp_path):
        """add_gitignore_entry does not duplicate an existing entry."""
        lib_git.add_gitignore_entry(".planning", tmp_path)
        lib_git.add_gitignore_entry(".planning", tmp_path)
        gitignore = tmp_path / ".gitignore"
        lines = [l for l in gitignore.read_text().splitlines() if l == ".planning"]
        assert len(lines) == 1

    def test_add_multiple_entries(self, tmp_path):
        """add_gitignore_entry can add multiple distinct entries."""
        lib_git.add_gitignore_entry(".planning", tmp_path)
        lib_git.add_gitignore_entry("AGENTS.md", tmp_path)
        gitignore = tmp_path / ".gitignore"
        lines = gitignore.read_text().splitlines()
        assert ".planning" in lines
        assert "AGENTS.md" in lines

    def test_check_returns_false_for_missing_entry(self, tmp_path):
        """check_gitignore_entry returns False for an entry not in the file."""
        lib_git.add_gitignore_entry(".planning", tmp_path)
        assert lib_git.check_gitignore_entry("AGENTS.md", tmp_path) is False

    def test_original_exclude_pair_unchanged(self, tmp_path):
        """add_git_exclusion / check_git_exclusion still target info/exclude."""
        git_dir = tmp_path / ".git"
        (git_dir / "info").mkdir(parents=True)
        lib_git.add_git_exclusion(".envrc", git_dir)
        assert lib_git.check_git_exclusion(".envrc", git_dir) is True
        # .gitignore must NOT be touched
        assert not (tmp_path / ".gitignore").exists()
