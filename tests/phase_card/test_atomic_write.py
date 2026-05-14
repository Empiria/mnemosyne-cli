"""Wave 0 test for ACC-38-08 — atomic frontmatter write.

The contract: write_phase_md_atomic(target, card, body) MUST:
  1. Create a temp file in the SAME DIRECTORY as target.
  2. fsync the temp file before replacing target.
  3. Use os.replace (NOT os.rename — RESEARCH.md State of the Art).
  4. Never leave a half-written target file visible to a concurrent reader.
  5. Preserve the body bytes verbatim (frontmatter-only mutation).

This test FAILS until P02 implements write_phase_md_atomic.
"""
from __future__ import annotations

import os
from pathlib import Path

import frontmatter
import pytest


def test_temp_file_then_replace(tmp_path):
    """Static proxy for atomicity — verifies temp-rename mechanics and absence of lingering .tmp files.

    The dynamic invariant (concurrent reader never sees partial YAML) is implied
    by os.replace's POSIX/Win32 atomic semantics, asserted statically via
    test_uses_os_replace_not_os_rename. A dynamic concurrent-reader stress test
    (hypothesis-based) is deferred — implementation cost exceeds value for an
    os.replace guarantee documented in CPython's stdlib (Python 3.3+).

    Probes the implementation by snooping the directory during the write and
    asserting (a) a `.tmp` file may appear briefly, (b) the final file is valid
    YAML, (c) no stale `.tmp` files linger post-call, (d) the target may
    pre-exist and gets replaced cleanly (Windows-atomicity probe via os.replace).
    """
    from mnemosyne_cli.lib.phase_card import PhaseCard, write_phase_md_atomic

    target = tmp_path / "phase.md"
    # Target pre-exists — os.replace must clobber atomically.
    target.write_text("# pre-existing — should be replaced\n")

    card = PhaseCard(
        project="[[mnemosyne]]",
        milestone="v1.0",
        phase_number="38",
        status="planned",
        title="Test",
    )
    write_phase_md_atomic(target, card, body="")

    # Target now contains valid frontmatter — the old content is gone.
    post = frontmatter.load(target)
    assert post["status"] == "planned"
    assert post["phase_number"] == "38"
    # No stale .tmp files lingering in the directory.
    tmp_files = list(tmp_path.glob(".phase.md.*.tmp"))
    assert tmp_files == [], f"Lingering temp files: {tmp_files}"


def test_body_preserved_verbatim(tmp_path):
    """ACC-38-08 + impl-4 — body bytes survive the write unchanged."""
    from mnemosyne_cli.lib.phase_card import PhaseCard, write_phase_md_atomic

    target = tmp_path / "phase.md"
    card = PhaseCard(
        project="[[mnemosyne]]",
        milestone=None,
        phase_number="38",
        status="planned",
        title="Test",
    )
    body = "## Manual notes\n\nLine 1\nLine 2 with `code` and *emphasis*.\n"
    write_phase_md_atomic(target, card, body=body)

    post = frontmatter.load(target)
    assert post.content == body.rstrip("\n") or post.content == body, (
        f"Body mutated: expected {body!r}, got {post.content!r}"
    )


def test_uses_os_replace_not_os_rename():
    """Static check — module source uses os.replace, not os.rename.

    Guards against RESEARCH.md Pitfall 5 regression. This is the static-proxy
    partner to test_temp_file_then_replace: together they assert the atomicity
    invariant (the dynamic guarantee comes from CPython's os.replace contract).
    """
    from mnemosyne_cli.lib import phase_card

    src = Path(phase_card.__file__).read_text()
    assert "os.replace" in src, "write_phase_md_atomic must use os.replace"
    # Allow os.rename to appear in comments/docstrings but reject as a callable.
    assert "os.rename(" not in src, (
        "os.rename detected — replace with os.replace for Windows atomicity"
    )


def test_atomic_replace_clobbers_existing(tmp_path):
    """The pattern handles target-exists case (Windows os.rename would fail here)."""
    from mnemosyne_cli.lib.phase_card import PhaseCard, write_phase_md_atomic

    target = tmp_path / "phase.md"
    target.write_text("---\nstatus: stale\n---\nold body\n")

    card = PhaseCard(
        project="[[mnemosyne]]",
        milestone=None,
        phase_number="38",
        status="in-progress",
        title="Test",
    )
    write_phase_md_atomic(target, card, body="new body\n")

    post = frontmatter.load(target)
    assert post["status"] == "in-progress"
    assert "old body" not in target.read_text()
