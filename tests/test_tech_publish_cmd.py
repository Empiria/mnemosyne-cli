"""Integration tests for mnemosyne tech-publish — Phase 49 Plan 03.

Tests cover:
  (a) test_review_gate              — D-10: review gate blocks; --skip-review-check bypasses
  (b) test_missing_deploy_key       — D-01: missing secrets.toml entry → actionable error
  (c) test_dirty_worktree_refusal   — D-02: dirty outside subtree blocked; inside allowed
  (d) test_unregistered_target      — D-02: unregistered target vault → PublishError
  (e) test_end_to_end_publish       — full pipeline: staged notes + LICENSE + TPN + PUBLISHED.json + commit + push
  (f) test_no_op_rerun              — D-06: zero-change re-run → nothing to publish, no new commit
  (g) test_diff_only_rerun          — D-05: one changed note → exactly one new commit
  (h) test_detect_client_edits_blocks — D-04: client edit → blocked; --force passes
  (i) test_dry_run_writes_nothing   — dry_run=True → no files, no commit
  (j) test_strip_policy_removes_cross_set_links — CR-01 regression

Phase 50 — LOCAL MODE (in-repo colocation):
  (k) test_local_mode_publish       — --into writes + commits, never pushes, no key
  (l) test_local_mode_no_commit     — --no-commit writes working tree, no commit
  (m) test_local_mode_requires_into — local-mode manifest + no --into → error
  (n) test_local_mode_ignores_dirty_outside — code-repo tree may be dirty; no sweep
  (o) test_local_mode_idempotent_rerun — D-06 no-op holds in local mode

Vault-mode tests use a local file:// bare git remote — no network, no real deploy
key. Local-mode tests use a plain local clone and assert the push is never called.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mnemosyne_cli.share.publish import (
    PublishError,
    PublishResult,
    resolve_deploy_key,
    run_publish,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "publish_vault"


def _make_vault(
    tmp_path: Path,
    client: str = "testclient",
    extra_fixtures: list[str] | None = None,
) -> Path:
    """Create a minimal source vault with a share-manifest and licence template.

    Args:
        tmp_path:       pytest tmp_path fixture.
        client:         Client slug (used for licence template directory).
        extra_fixtures: Additional fixture-relative paths to copy into the vault.
                        Used by tests that need breach targets for strip policy.
    """
    vault = tmp_path / "empiria-vault"
    vault.mkdir(parents=True)

    # Copy in fixture notes
    base_fixtures = [
        "technologies/anvil/reference/testing.md",
        "technologies/anvil/reference/forms.md",
        "technologies/python/reference/vendored-lib.md",
    ]
    for rel in base_fixtures + (extra_fixtures or []):
        src = FIXTURE_ROOT / rel
        dst = vault / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)

    # Copy licence template
    lic_src = FIXTURE_ROOT / "clients" / "friendly-fox" / "license-template.md"
    lic_dst = vault / "clients" / client / "license-template.md"
    lic_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(lic_src, lic_dst)

    # Initialise as a git repo so get_short_sha works
    subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=vault, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=vault, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=vault, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=vault, check=True, capture_output=True,
    )
    return vault


def _make_target_repo(tmp_path: Path, name: str = "target-vault") -> tuple[Path, Path]:
    """Create a target vault working-copy + a bare file:// origin remote.

    Returns (target_working_copy, bare_remote_path).
    The working copy has 'origin' pointing at the bare remote (file:// URL).
    """
    bare = tmp_path / f"{name}.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=bare, check=True, capture_output=True)

    wc = tmp_path / name
    wc.mkdir()
    subprocess.run(["git", "init"], cwd=wc, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=wc, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=wc, check=True, capture_output=True,
    )
    # Create an initial commit so we can push to main
    readme = wc / "README.md"
    readme.write_text("# Target vault\n")
    subprocess.run(["git", "add", "README.md"], cwd=wc, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=wc, check=True, capture_output=True,
    )
    # Rename branch to main
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=wc, check=True, capture_output=True,
    )
    # Point origin at bare remote
    subprocess.run(
        ["git", "remote", "add", "origin", f"file://{bare}"],
        cwd=wc, check=True, capture_output=True,
    )
    # Push initial commit to origin/main
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=wc, check=True, capture_output=True,
    )
    return wc, bare


def _make_manifest_toml(
    client: str = "testclient",
    target_vault: str | None = "client-vault",
    target_subtree: str = "imported/empiria",
    deploy_key_ref: str = "client-deploy",
    policy: str = "refuse",
    reviewed_at: str | None = "2026-06-01",
    extra_license: dict[str, Any] | None = None,
) -> str:
    """Return a share-manifest.toml string.

    Pass ``target_vault=None`` to produce a LOCAL-MODE manifest (omits both
    ``target_vault`` and ``deploy_key_ref``) — Phase 50 in-repo colocation.
    """
    lines = [
        "[client]",
        f'slug = "{client}"',
        f'display = "{client.title()}"',
        'mode = "direct"',
        "",
        "[direct]",
    ]
    if target_vault is not None:
        lines.append(f'target_vault = "{target_vault}"')
    lines.append(f'target_subtree = "{target_subtree}"')
    if target_vault is not None:
        lines.append(f'deploy_key_ref = "{deploy_key_ref}"')
    lines += [
        "",
        "[include]",
        'paths = ["technologies/anvil/reference/**"]',
        "",
        "[on_closure_breach]",
        f'policy = "{policy}"',
        "",
        "[license]",
        f'template = "clients/{client}/license-template.md"',
        'spdx_license_ref = "LicenseRef-Empiria-Test-2026"',
        'copyright_holder = "Empiria Ltd."',
    ]
    if reviewed_at is not None:
        lines.append(f'license_template_reviewed_at = "{reviewed_at}"')
    if extra_license:
        for k, v in extra_license.items():
            lines.append(f'{k} = "{v}"')
    return "\n".join(lines) + "\n"


def _write_manifest(vault: Path, client: str, toml_content: str) -> Path:
    """Write the manifest file and commit it to the vault."""
    manifest_dir = vault / "clients" / client
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "share-manifest.toml"
    manifest_path.write_text(toml_content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add manifest"],
        cwd=vault, check=True, capture_output=True,
    )
    return manifest_path


def _make_config_toml(
    vault_root: Path,
    target_name: str,
    target_path: Path,
) -> dict:
    """Build a config dict that _read_config() would return."""
    return {
        "vault_path": str(vault_root),
        "vaults": {
            "empiria": {
                "path": str(vault_root),
                "description": "Empiria vault",
                "sync": "git",
            },
            target_name: {
                "path": str(target_path),
                "description": "Target vault",
                "sync": "git",
            },
        },
        "vault_rules": [
            {
                "from": "empiria",
                "can_read": [target_name],
            }
        ],
    }


def _make_secrets_toml(key_ref: str, key_path: str | Path) -> str:
    """Return a secrets.toml string with a dummy deploy key entry."""
    return f"[deploy_keys]\n{key_ref} = \"{key_path}\"\n"


def _dummy_key_path(tmp_path: Path) -> Path:
    """Create a dummy SSH key file (content irrelevant for file:// remotes)."""
    key = tmp_path / "dummy_key"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\ndummy\n-----END OPENSSH PRIVATE KEY-----\n")
    key.chmod(0o600)
    return key


def _git_log_count(repo: Path) -> int:
    """Return the number of commits in HEAD's history."""
    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _git_log_messages(repo: Path) -> list[str]:
    """Return commit messages for the current HEAD history."""
    result = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# (a) test_review_gate — D-10
# ---------------------------------------------------------------------------


def test_review_gate(tmp_path: Path) -> None:
    """Manifest WITHOUT license_template_reviewed_at → PublishError.
    WITH --skip-review-check it proceeds past the gate (D-10)."""
    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    # Manifest WITHOUT reviewed_at
    toml_no_review = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
        reviewed_at=None,
    )
    _write_manifest(vault, "testclient", toml_no_review)

    config = _make_config_toml(vault, "client-vault", target_wc)
    secrets_content = _make_secrets_toml("client-deploy", dummy_key)
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(secrets_content)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
    ):
        # Without skip_review_check → must raise PublishError
        with pytest.raises(PublishError) as exc_info:
            run_publish(
                client="testclient",
                force=False,
                skip_review_check=False,
                dry_run=True,
            )
        assert "license_template_reviewed_at" in str(exc_info.value)

        # With skip_review_check=True → proceeds past the gate
        # (dry_run so no git activity required)
        result = run_publish(
            client="testclient",
            force=False,
            skip_review_check=True,
            dry_run=True,
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# (b) test_missing_deploy_key — D-01
# ---------------------------------------------------------------------------


def test_missing_deploy_key(tmp_path: Path) -> None:
    """No secrets.toml entry for the ref → resolve_deploy_key / run_publish raises
    PublishError whose message shows the [deploy_keys] block to add (D-01)."""
    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="missing-key-ref",
    )
    _write_manifest(vault, "testclient", toml)

    # secrets.toml with a DIFFERENT key ref
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text("[deploy_keys]\nother-key = \"/tmp/other\"\n")

    config = _make_config_toml(vault, "client-vault", target_wc)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
    ):
        with pytest.raises(PublishError) as exc_info:
            run_publish(
                client="testclient",
                force=False,
                skip_review_check=False,
                dry_run=False,
            )
        msg = str(exc_info.value)
        assert "missing-key-ref" in msg or "deploy_keys" in msg

    # Also test resolve_deploy_key directly with no secrets file at all
    no_secrets = tmp_path / "no-secrets.toml"
    with patch("mnemosyne_cli.share.publish._SECRETS_PATH", no_secrets):
        with pytest.raises(PublishError) as exc_info2:
            resolve_deploy_key("some-ref")
        assert "deploy_keys" in str(exc_info2.value)


# ---------------------------------------------------------------------------
# (c) test_dirty_worktree_refusal — D-02
# ---------------------------------------------------------------------------


def test_dirty_worktree_refusal(tmp_path: Path) -> None:
    """Dirty file OUTSIDE subtree → PublishError; inside subtree → allowed (D-02)."""
    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
        target_subtree="imported/empiria",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", dummy_key))

    config = _make_config_toml(vault, "client-vault", target_wc)

    # Create a dirty file OUTSIDE the subtree
    dirty_outside = target_wc / "some-other-file.md"
    dirty_outside.write_text("dirty content")

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
    ):
        with pytest.raises(PublishError) as exc_info:
            run_publish(
                client="testclient",
                force=False,
                skip_review_check=False,
                dry_run=False,
            )
        assert "some-other-file.md" in str(exc_info.value)

    # Clean up the dirty file from outside
    dirty_outside.unlink()

    # Create a dirty file INSIDE the subtree — should NOT block
    subtree_dir = target_wc / "imported" / "empiria"
    subtree_dir.mkdir(parents=True, exist_ok=True)
    (subtree_dir / "inside-file.md").write_text("inside dirty")

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
    ):
        # dry_run=True so we don't need push to work — should not raise for
        # dirty-inside-subtree
        result = run_publish(
            client="testclient",
            force=False,
            skip_review_check=False,
            dry_run=True,
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# (d) test_unregistered_target — D-02
# ---------------------------------------------------------------------------


def test_unregistered_target(tmp_path: Path) -> None:
    """Manifest target_vault not in config → PublishError (D-02)."""
    vault = _make_vault(tmp_path, client="testclient")

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="unregistered-vault",  # NOT in config
        deploy_key_ref="client-deploy",
    )
    _write_manifest(vault, "testclient", toml)

    # Config with NO entry for "unregistered-vault"
    config: dict = {
        "vault_path": str(vault),
        "vaults": {
            "empiria": {"path": str(vault), "description": "", "sync": "git"},
        },
        "vault_rules": [],
    }

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
    ):
        with pytest.raises(PublishError) as exc_info:
            run_publish(
                client="testclient",
                force=False,
                skip_review_check=False,
                dry_run=True,
            )
        assert "unregistered-vault" in str(exc_info.value) or "not registered" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (e) test_end_to_end_publish — full pipeline
# ---------------------------------------------------------------------------


def test_end_to_end_publish(tmp_path: Path) -> None:
    """Full run_publish against a throwaway local git repo.

    Asserts:
    - Staged notes exist under <target>/imported/empiria/technologies/... with SPDX frontmatter.
    - LICENSE.md, THIRD-PARTY-NOTICES.md, PUBLISHED.json exist at publish root.
    - A commit landed with the 'Empiria publish:' message.
    - The push reached the bare file:// origin.
    """
    import frontmatter as fm

    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
        target_subtree="imported/empiria",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", dummy_key))

    config = _make_config_toml(vault, "client-vault", target_wc)

    initial_commit_count = _git_log_count(target_wc)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        # For file:// remotes, GIT_SSH_COMMAND is irrelevant — patch push to use
        # regular git push (no SSH needed for file:// protocol)
        patch(
            "mnemosyne_cli.share.publish.git_push_with_deploy_key",
            lambda repo_path, kp: subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=repo_path, check=True, capture_output=True,
            ),
        ),
    ):
        result = run_publish(
            client="testclient",
            force=False,
            skip_review_check=False,
            dry_run=False,
        )

    assert result.success is True
    assert result.published is True

    publish_root = target_wc / "imported" / "empiria"

    # Staged notes exist at FLATTENED paths — the knowledge-type dir (reference)
    # is dropped so the publish doesn't leak Empiria's taxonomy.
    staged_testing = publish_root / "anvil" / "testing.md"
    staged_forms = publish_root / "anvil" / "forms.md"
    assert staged_testing.exists(), "testing.md not staged at flattened path"
    assert staged_forms.exists(), "forms.md not staged at flattened path"
    # Neither the 'technologies' category dir nor the 'reference' knowledge-type
    # dir survives in the published tree.
    assert not (publish_root / "technologies").exists(), (
        "'technologies' category dir leaked into published output"
    )

    # Frontmatter is reduced to ONLY the two SPDX fields — title/tags/created
    # from the source note must NOT leak.
    post = fm.load(str(staged_testing))
    assert post.metadata.get("SPDX-License-Identifier") == "LicenseRef-Empiria-Test-2026"
    assert "SPDX-FileCopyrightText" in post.metadata
    assert set(post.metadata.keys()) == {"SPDX-License-Identifier", "SPDX-FileCopyrightText"}, (
        f"non-SPDX frontmatter leaked: {sorted(post.metadata.keys())}"
    )

    # LICENSE.md, THIRD-PARTY-NOTICES.md, PUBLISHED.json
    assert (publish_root / "LICENSE.md").exists(), "LICENSE.md missing"
    assert (publish_root / "THIRD-PARTY-NOTICES.md").exists(), "THIRD-PARTY-NOTICES.md missing"
    assert (publish_root / "PUBLISHED.json").exists(), "PUBLISHED.json missing"

    # Commit landed with 'Empiria publish:' message
    messages = _git_log_messages(target_wc)
    assert any("Empiria publish:" in m for m in messages), f"No publish commit: {messages}"

    # Push reached origin — bare remote should have the commit
    bare_messages = _git_log_messages(target_bare)
    assert any("Empiria publish:" in m for m in bare_messages), (
        f"Commit not in bare origin: {bare_messages}"
    )


# ---------------------------------------------------------------------------
# (f) test_no_op_rerun — D-06
# ---------------------------------------------------------------------------


def test_no_op_rerun(tmp_path: Path) -> None:
    """Run once (publishes), then run again with zero source changes.
    Second run returns published=False, no new commit (D-06)."""
    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", dummy_key))
    config = _make_config_toml(vault, "client-vault", target_wc)

    patches = dict(
        vault_resolve=patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        config_read=patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        secrets=patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        push=patch(
            "mnemosyne_cli.share.publish.git_push_with_deploy_key",
            lambda rp, kp: subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=rp, check=True, capture_output=True,
            ),
        ),
    )

    with patches["vault_resolve"], patches["config_read"], patches["secrets"], patches["push"]:
        # First run — publishes
        r1 = run_publish(client="testclient", force=False, skip_review_check=False, dry_run=False)
        assert r1.published is True

    commit_count_after_first = _git_log_count(target_wc)

    with patches["vault_resolve"], patches["config_read"], patches["secrets"], patches["push"]:
        # Second run — zero source changes
        r2 = run_publish(client="testclient", force=False, skip_review_check=False, dry_run=False)
        assert r2.published is False
        assert "nothing to publish" in r2.message.lower() or "nothing" in r2.message.lower()

    commit_count_after_second = _git_log_count(target_wc)
    assert commit_count_after_second == commit_count_after_first, (
        f"New commit created on no-op rerun: "
        f"{commit_count_after_first} → {commit_count_after_second}"
    )


# ---------------------------------------------------------------------------
# (g) test_diff_only_rerun — D-05
# ---------------------------------------------------------------------------


def test_diff_only_rerun(tmp_path: Path) -> None:
    """After first publish, modify one source note; re-run publishes only that note."""
    import frontmatter as fm

    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", dummy_key))
    config = _make_config_toml(vault, "client-vault", target_wc)

    push_patch = patch(
        "mnemosyne_cli.share.publish.git_push_with_deploy_key",
        lambda rp, kp: subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=rp, check=True, capture_output=True,
        ),
    )

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        push_patch,
    ):
        r1 = run_publish(client="testclient", force=False, skip_review_check=False, dry_run=False)
        assert r1.published is True

    # Mutate ONE source note
    forms_md = vault / "technologies" / "anvil" / "reference" / "forms.md"
    post = fm.load(str(forms_md))
    post.content += "\n\nAdded in re-run test.\n"
    forms_md.write_text(fm.dumps(post), encoding="utf-8")
    # Commit the source change so vault SHA changes
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "update forms"],
        cwd=vault, check=True, capture_output=True,
    )

    commit_count_before = _git_log_count(target_wc)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        push_patch,
    ):
        r2 = run_publish(client="testclient", force=False, skip_review_check=False, dry_run=False)
        assert r2.published is True

    commit_count_after = _git_log_count(target_wc)
    assert commit_count_after == commit_count_before + 1, (
        f"Expected exactly 1 new commit: {commit_count_before} → {commit_count_after}"
    )

    # The updated forms.md should have updated content in the target
    staged_forms = target_wc / "imported" / "empiria" / "anvil" / "forms.md"
    assert staged_forms.exists()
    assert "Added in re-run test" in staged_forms.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (h) test_detect_client_edits_blocks — D-04
# ---------------------------------------------------------------------------


def test_detect_client_edits_blocks(tmp_path: Path) -> None:
    """After first publish, mutate a staged note; re-run blocks without --force;
    with --force it succeeds (D-04)."""
    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", dummy_key))
    config = _make_config_toml(vault, "client-vault", target_wc)

    push_patch = patch(
        "mnemosyne_cli.share.publish.git_push_with_deploy_key",
        lambda rp, kp: subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=rp, check=True, capture_output=True,
        ),
    )

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        push_patch,
    ):
        r1 = run_publish(client="testclient", force=False, skip_review_check=False, dry_run=False)
        assert r1.published is True

    # Mutate a staged note in the target subtree (simulates client edit)
    staged_testing = (
        target_wc / "imported" / "empiria"
        / "anvil" / "testing.md"
    )
    assert staged_testing.exists(), "staged testing.md not found after first publish"
    staged_testing.write_text("client edited this content!", encoding="utf-8")

    # Re-run without --force → must block
    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        push_patch,
    ):
        with pytest.raises(PublishError) as exc_info:
            run_publish(client="testclient", force=False, skip_review_check=False, dry_run=False)
        assert "modified or deleted" in str(exc_info.value) or "testing.md" in str(exc_info.value)

    # Re-run WITH --force → succeeds
    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        push_patch,
    ):
        r3 = run_publish(client="testclient", force=True, skip_review_check=False, dry_run=False)
        assert r3.published is True


# ---------------------------------------------------------------------------
# (i) test_dry_run_writes_nothing — D-06 dry_run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """run_publish(dry_run=True) against a never-published target.
    No files created under publish root, no commit, git log unchanged."""
    vault = _make_vault(tmp_path, client="testclient")
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")

    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", "/tmp/dummy-key"))
    config = _make_config_toml(vault, "client-vault", target_wc)

    publish_root = target_wc / "imported" / "empiria"
    initial_commit_count = _git_log_count(target_wc)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
    ):
        result = run_publish(
            client="testclient",
            force=False,
            skip_review_check=False,
            dry_run=True,
        )

    assert result.success is True
    assert result.published is False

    # No files created under publish_root
    assert not publish_root.exists() or not any(publish_root.rglob("*")), (
        f"Files were created under {publish_root} during dry_run"
    )

    # No new commit
    assert _git_log_count(target_wc) == initial_commit_count, (
        "A commit was created during dry_run"
    )


# ---------------------------------------------------------------------------
# (j) test_strip_policy_removes_cross_set_links — CR-01 regression
# ---------------------------------------------------------------------------


def test_strip_policy_removes_cross_set_links(tmp_path: Path) -> None:
    """policy='strip' must remove cross-set wikilinks from staged notes (CR-01).

    The fixture testing.md contains [[technologies/secret/internal|the internal note]].
    technologies/secret/internal.md exists in the vault but is NOT covered by the
    include set (only technologies/anvil/reference/** is included), so the walker
    classifies it as a breach.

    The strip policy must:
    - Normalise the .md-suffixed breach paths to extensionless form so they match
      the wikilink base that strip_cross_set_wikilinks compares against (CR-01 fix).
    - Write the staged note WITHOUT the [[technologies/secret/internal...]] wikilink
      (replaced by alias text).

    This test fails against the pre-fix wiring (breach_targets built with .md suffixes)
    and passes after the fix.
    """
    import frontmatter as fm

    # Include the breach-target note so the walker classifies it as 'breach'
    # (if the file is absent it would be 'broken', not 'breach', and breach_targets
    # would be empty — the strip path would be a no-op).
    vault = _make_vault(
        tmp_path,
        client="testclient",
        extra_fixtures=["technologies/secret/internal.md"],
    )
    target_wc, target_bare = _make_target_repo(tmp_path, "client-vault")
    dummy_key = _dummy_key_path(tmp_path)

    # Manifest with policy='strip' so cross-set links are flattened
    toml = _make_manifest_toml(
        client="testclient",
        target_vault="client-vault",
        deploy_key_ref="client-deploy",
        target_subtree="imported/empiria",
        policy="strip",
    )
    _write_manifest(vault, "testclient", toml)

    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(_make_secrets_toml("client-deploy", dummy_key))
    config = _make_config_toml(vault, "client-vault", target_wc)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish._SECRETS_PATH", secrets_path),
        patch(
            "mnemosyne_cli.share.publish.git_push_with_deploy_key",
            lambda repo_path, kp: subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=repo_path, check=True, capture_output=True,
            ),
        ),
    ):
        result = run_publish(
            client="testclient",
            force=False,
            skip_review_check=False,
            dry_run=False,
        )

    assert result.success is True
    assert result.published is True

    publish_root = target_wc / "imported" / "empiria"
    staged_testing = publish_root / "anvil" / "testing.md"
    assert staged_testing.exists(), "testing.md not staged under strip policy"

    staged_content = staged_testing.read_text(encoding="utf-8")

    # The cross-set wikilink target must NOT appear in the staged output.
    # Before CR-01 fix: breach_targets built with .md suffixes would never match
    # the extensionless wikilink base, so the link leaked unchanged.
    assert "technologies/secret/internal" not in staged_content, (
        "Cross-set wikilink target 'technologies/secret/internal' leaked into "
        "staged output — strip policy failed to remove it (CR-01 regression)"
    )

    # The alias text should be present (strip replaces [[target|alias]] with alias)
    assert "the internal note" in staged_content, (
        "Alias text from stripped wikilink not found — expected 'the internal note'"
    )

    # The in-set wikilink must survive the strip pass, retargeted at the path
    # forms.md is actually published to (anvil/forms.md, unique basename → bare
    # link). Its source path spells out the stripped reference/ tier, which does
    # not exist in the client vault.
    assert "[[forms]]" in staged_content, (
        "In-set wikilink was incorrectly stripped"
    )
    assert "[[technologies/anvil/reference/forms]]" not in staged_content, (
        "In-set wikilink kept its source path and would not resolve post-flatten"
    )

    # SPDX frontmatter must be injected (shared code path with normal staging)
    post = fm.load(str(staged_testing))
    assert post.metadata.get("SPDX-License-Identifier") == "LicenseRef-Empiria-Test-2026", (
        "SPDX-License-Identifier not injected in strip-path staged note"
    )
    assert "SPDX-FileCopyrightText" in post.metadata, (
        "SPDX-FileCopyrightText not injected in strip-path staged note"
    )


# ===========================================================================
# Phase 50 — LOCAL MODE (in-repo colocation)
#
# A local-mode manifest omits target_vault/deploy_key_ref; the operator supplies
# the local clone path via `into=`. tech-publish writes <clone>/<subtree>, commits
# locally, and NEVER pushes (transport is the operator's responsibility).
# ===========================================================================


def _make_local_clone(tmp_path: Path, name: str = "iw-clone") -> Path:
    """A plain local git repo (stand-in for the IW code clone) — no origin needed."""
    wc = tmp_path / name
    wc.mkdir()
    subprocess.run(["git", "init"], cwd=wc, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=wc, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=wc, check=True, capture_output=True)
    (wc / "anvil.yaml").write_text("# anvil app\n")
    subprocess.run(["git", "add", "-A"], cwd=wc, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init app"], cwd=wc, check=True, capture_output=True)
    return wc


# ---------------------------------------------------------------------------
# (k) test_local_mode_publish — writes + commits, never pushes, no deploy key
# ---------------------------------------------------------------------------


def test_local_mode_publish(tmp_path: Path) -> None:
    """--into a local clone: writes context/empiria, commits, NO push, no key."""
    import frontmatter as fm

    vault = _make_vault(tmp_path, client="friendly-fox")
    clone = _make_local_clone(tmp_path)

    toml = _make_manifest_toml(
        client="friendly-fox",
        target_vault=None,  # local-mode manifest
        target_subtree="context/empiria",
    )
    _write_manifest(vault, "friendly-fox", toml)

    config = _make_config_toml(vault, "unused", clone)
    push_mock = MagicMock()
    initial_commits = _git_log_count(clone)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish.git_push_with_deploy_key", push_mock),
    ):
        result = run_publish(
            client="friendly-fox",
            force=False,
            skip_review_check=False,
            dry_run=False,
            into=str(clone),
        )

    assert result.success is True
    assert result.published is True
    push_mock.assert_not_called()  # local mode NEVER pushes

    publish_root = clone / "context" / "empiria"
    staged_testing = publish_root / "anvil" / "testing.md"
    assert staged_testing.exists(), "slice not written under context/empiria"
    assert (publish_root / "LICENSE.md").exists()
    assert (publish_root / "THIRD-PARTY-NOTICES.md").exists()
    assert (publish_root / "PUBLISHED.json").exists()

    post = fm.load(str(staged_testing))
    assert post.metadata.get("SPDX-License-Identifier") == "LicenseRef-Empiria-Test-2026"
    assert "SPDX-FileCopyrightText" in post.metadata

    # Exactly one local commit; message carries the publish prefix
    assert _git_log_count(clone) == initial_commits + 1
    assert any("Empiria publish:" in m for m in _git_log_messages(clone))


# ---------------------------------------------------------------------------
# (l) test_local_mode_no_commit — writes the working tree but does not commit
# ---------------------------------------------------------------------------


def test_local_mode_no_commit(tmp_path: Path) -> None:
    """--no-commit (local mode): files written, NO commit, no deploy key."""
    vault = _make_vault(tmp_path, client="friendly-fox")
    clone = _make_local_clone(tmp_path)

    toml = _make_manifest_toml(
        client="friendly-fox", target_vault=None, target_subtree="context/empiria"
    )
    _write_manifest(vault, "friendly-fox", toml)
    config = _make_config_toml(vault, "unused", clone)
    initial_commits = _git_log_count(clone)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
    ):
        result = run_publish(
            client="friendly-fox",
            force=False,
            skip_review_check=False,
            dry_run=False,
            into=str(clone),
            commit=False,
        )

    assert result.success is True
    assert result.published is True
    assert "--no-commit" in result.message

    # Files written to the working tree …
    assert (clone / "context" / "empiria" / "PUBLISHED.json").exists()
    # … but NO commit was created
    assert _git_log_count(clone) == initial_commits


# ---------------------------------------------------------------------------
# (m) test_local_mode_requires_into — local-mode manifest with no --into errors
# ---------------------------------------------------------------------------


def test_local_mode_requires_into(tmp_path: Path) -> None:
    """A manifest with no target_vault and no --into → actionable PublishError."""
    vault = _make_vault(tmp_path, client="friendly-fox")

    toml = _make_manifest_toml(
        client="friendly-fox", target_vault=None, target_subtree="context/empiria"
    )
    _write_manifest(vault, "friendly-fox", toml)
    config = _make_config_toml(vault, "unused", vault)

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
    ):
        with pytest.raises(PublishError) as exc_info:
            run_publish(
                client="friendly-fox",
                force=False,
                skip_review_check=False,
                dry_run=False,
            )
    assert "--into" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (n) test_local_mode_ignores_dirty_outside — code repo tree may be dirty
# ---------------------------------------------------------------------------


def test_local_mode_ignores_dirty_outside(tmp_path: Path) -> None:
    """Local mode must NOT block on a dirty tree outside the subtree, and must
    NOT sweep unrelated changes into the publish commit (explicit pathspec)."""
    vault = _make_vault(tmp_path, client="friendly-fox")
    clone = _make_local_clone(tmp_path)

    toml = _make_manifest_toml(
        client="friendly-fox", target_vault=None, target_subtree="context/empiria"
    )
    _write_manifest(vault, "friendly-fox", toml)
    config = _make_config_toml(vault, "unused", clone)

    # Active code work in progress, outside the publish subtree
    (clone / "client_code.py").write_text("# work in progress\n")

    with (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish.git_push_with_deploy_key", MagicMock()),
    ):
        result = run_publish(
            client="friendly-fox",
            force=False,
            skip_review_check=False,
            dry_run=False,
            into=str(clone),
        )

    assert result.published is True
    # The unrelated file is still uncommitted — the publish commit did not grab it
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=clone, capture_output=True, text=True, check=True,
    ).stdout
    assert "client_code.py" in status, "unrelated dirty file should remain uncommitted"


# ---------------------------------------------------------------------------
# (o) test_local_mode_idempotent_rerun — D-06 holds in local mode
# ---------------------------------------------------------------------------


def test_local_mode_idempotent_rerun(tmp_path: Path) -> None:
    """Second local-mode run with zero source changes is a no-op (no new commit)."""
    vault = _make_vault(tmp_path, client="friendly-fox")
    clone = _make_local_clone(tmp_path)

    toml = _make_manifest_toml(
        client="friendly-fox", target_vault=None, target_subtree="context/empiria"
    )
    _write_manifest(vault, "friendly-fox", toml)
    config = _make_config_toml(vault, "unused", clone)

    patches = (
        patch("mnemosyne_cli.lib.vault.resolve_vault_path", return_value=vault),
        patch("mnemosyne_cli.lib.vault._read_config", return_value=config),
        patch("mnemosyne_cli.share.publish.git_push_with_deploy_key", MagicMock()),
    )

    with patches[0], patches[1], patches[2]:
        r1 = run_publish(
            client="friendly-fox", force=False, skip_review_check=False,
            dry_run=False, into=str(clone),
        )
        assert r1.published is True

    commits_after_first = _git_log_count(clone)

    with patches[0], patches[1], patches[2]:
        r2 = run_publish(
            client="friendly-fox", force=False, skip_review_check=False,
            dry_run=False, into=str(clone),
        )
        assert r2.published is False
        assert "nothing" in r2.message.lower()

    assert _git_log_count(clone) == commits_after_first
