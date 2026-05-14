"""Wikilink-format tests for written ``phase.md`` frontmatter (ACC-37-08).

Every wikilink field uses the display-name short form per D-17:

    project:      ``[[mnemosyne]]``           — required, never null
    plan:         ``[[37-01-PLAN]]`` or null
    summary_doc:  ``[[37-01-SUMMARY]]`` or null
    validation:   ``[[37-VALIDATION]]`` or null

Regex (no slashes; no nested brackets): ``^\\[\\[[^\\]/]+\\]\\]$``
"""

from __future__ import annotations

import re

import frontmatter
from typer.testing import CliRunner

from mnemosyne_cli.main import app as cli_app

_WIKILINK_RE = re.compile(r"^\[\[[^\]/]+\]\]$")


def _run(args):
    return CliRunner().invoke(cli_app, ["phase", *args])


def _all_phase_mds(vault_root):
    return list(
        (vault_root / "projects").rglob("gsd-planning/phases/*/phase.md")
    )


def test_project_wikilink_short_form(synthetic_vault, monkeypatch):
    """ACC-37-08 — every phase.md ``project:`` is a short-form wikilink."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    _run(["backfill"])
    found = _all_phase_mds(synthetic_vault)
    assert found, "Backfill produced no phase.md files to check"
    for phase_md in found:
        post = frontmatter.load(phase_md)
        project = post.get("project")
        assert isinstance(project, str), (
            f"{phase_md}: project should be str, got {type(project).__name__}"
        )
        assert _WIKILINK_RE.match(project), (
            f"{phase_md}: project wikilink not short form — got {project!r}"
        )


def test_plan_wikilink_or_null(synthetic_vault, monkeypatch):
    """plan / summary_doc / validation are either null or short-form wikilinks."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    _run(["backfill"])
    for phase_md in _all_phase_mds(synthetic_vault):
        post = frontmatter.load(phase_md)
        for field in ("plan", "summary_doc", "validation"):
            v = post.get(field)
            assert v is None or (
                isinstance(v, str) and _WIKILINK_RE.match(v)
            ), (
                f"{phase_md}: {field} value {v!r} is neither null nor a "
                "short-form wikilink"
            )


def test_project_wikilink_has_no_slashes(synthetic_vault, monkeypatch):
    """D-17 — wikilinks MUST be display-name short form, no path segments."""
    monkeypatch.setenv("MNEMOSYNE_VAULT", str(synthetic_vault))
    _run(["backfill"])
    for phase_md in _all_phase_mds(synthetic_vault):
        post = frontmatter.load(phase_md)
        for field in ("project", "plan", "summary_doc", "validation"):
            v = post.get(field)
            if v is None:
                continue
            assert "/" not in v, (
                f"{phase_md}: {field}={v!r} contains '/' — must be display-name "
                "short form per D-17"
            )
