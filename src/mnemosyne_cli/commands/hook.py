"""Git hook handlers."""

from __future__ import annotations

import re
import subprocess

import typer

app = typer.Typer(no_args_is_help=True)


@app.command("post-change")
def post_change() -> None:
    """Detect container and vault content changes and print refresh suggestions."""
    # Get changed files from the most recent commit
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        capture_output=True,
        text=True,
    )
    changed_files = result.stdout.strip()

    # Fall back to ORIG_HEAD diff if no files found
    if not changed_files:
        orig_head_result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
        )
        git_dir = orig_head_result.stdout.strip()
        if git_dir:
            import pathlib
            orig_head_path = pathlib.Path(git_dir) / "ORIG_HEAD"
            if orig_head_path.exists():
                result2 = subprocess.run(
                    ["git", "diff", "--name-only", "ORIG_HEAD", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                changed_files = result2.stdout.strip()

    if not changed_files:
        return

    files = changed_files.splitlines()
    needs_images = any(re.match(r"^containers/", f) for f in files)
    needs_qmd = any(re.match(r"^(technologies/|agents/|docs/|projects/.*\.md)", f) for f in files)

    if needs_images and needs_qmd:
        typer.echo("")
        typer.echo("  \u27f3 Container files and vault content changed \u2014 run: mnemosyne refresh")
        typer.echo("")
    elif needs_images:
        typer.echo("")
        typer.echo("  \u27f3 Container files changed \u2014 run: mnemosyne refresh --skip-qmd")
        typer.echo("")
    elif needs_qmd:
        typer.echo("")
        typer.echo("  \u27f3 Vault content changed \u2014 run: mnemosyne refresh --skip-images")
        typer.echo("")
