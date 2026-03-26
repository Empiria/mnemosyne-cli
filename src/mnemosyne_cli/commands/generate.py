"""mnemosyne generate — generate derived vault artifacts."""

from __future__ import annotations

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault as lib_vault
from mnemosyne_cli.lib.manifests import generate_learning_manifest

console = Console()
error_console = Console(stderr=True, style="bold red")

app = typer.Typer(no_args_is_help=True, help="Generate derived vault artifacts.")


@app.command("manifests")
def manifests() -> None:
    """Generate learning manifests for all technology directories."""
    vault_path = lib_vault.resolve_vault_path()
    tech_root = vault_path / "technologies"

    if not tech_root.is_dir():
        error_console.print(f"Technologies directory not found: {tech_root}")
        raise typer.Exit(1)

    generated = 0
    skipped = 0

    for tech_dir in sorted(tech_root.iterdir()):
        if not tech_dir.is_dir():
            continue

        content = generate_learning_manifest(tech_dir)
        if content is None:
            skipped += 1
            continue

        manifest_path = tech_dir / "learning-manifest.md"
        # Only write if content changed
        if manifest_path.exists() and manifest_path.read_text() == content:
            console.print(f"  [dim]Unchanged[/dim] {tech_dir.name}/learning-manifest.md")
        else:
            manifest_path.write_text(content)
            console.print(f"  [green]Generated[/green] {tech_dir.name}/learning-manifest.md")
        generated += 1

    console.print()
    if generated:
        console.print(f"[green]{generated} manifest(s) generated[/green], {skipped} tech(s) without learning notes.")
    else:
        console.print("[yellow]No technologies with learning notes found.[/yellow]")
