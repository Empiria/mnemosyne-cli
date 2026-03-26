"""mnemosyne refresh — rebuild container images and update qmd index."""

from __future__ import annotations

import subprocess
import shutil

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault
from mnemosyne_cli.lib.manifests import generate_learning_manifest

console = Console()
error_console = Console(stderr=True, style="bold red")


def run(
    skip_images: bool = typer.Option(False, "--skip-images", help="Skip container image rebuild."),
    skip_qmd: bool = typer.Option(False, "--skip-qmd", help="Skip qmd index update."),
) -> None:
    """Rebuild container images and refresh the qmd search index."""
    vault_path = vault.resolve_vault_path()
    containers_dir = vault_path / "containers"
    failed = False

    # --- Container images ---
    if not skip_images:
        console.rule("[bold cyan]Rebuilding container images[/bold cyan]")

        base_dir = containers_dir / "base"
        claude_dir = containers_dir / "claude"

        if not base_dir.exists() or not claude_dir.exists():
            error_console.print(f"Container directories not found in {containers_dir}")
            raise typer.Exit(1)

        # Get git hash of last commit touching containers/ for build label
        build_hash_result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", "containers/"],
            cwd=str(vault_path), capture_output=True, text=True,
        )
        build_hash = build_hash_result.stdout.strip() or "unknown"

        for name, path in [("mnemosyne-base", base_dir), ("mnemosyne-claude", claude_dir)]:
            console.print(f"  Building [cyan]{name}[/cyan]...")
            result = subprocess.run(
                [
                    "podman", "build",
                    "--build-arg", f"MNEMOSYNE_BUILD_HASH={build_hash}",
                    "-t", f"{name}:latest", str(path),
                ],
                text=True,
            )
            if result.returncode != 0:
                error_console.print(f"  [red]Failed[/red] to build {name}")
                failed = True
                break
            else:
                console.print(f"  [green]Built[/green] {name}")
    else:
        console.print("[dim]Skipping image rebuild.[/dim]")

    # --- qmd index ---
    if not skip_qmd:
        console.rule("[bold cyan]Updating qmd index[/bold cyan]")

        if not shutil.which("qmd"):
            error_console.print("qmd not found on PATH — skipping index update.")
        else:
            for step in ["update", "embed"]:
                console.print(f"  Running [cyan]qmd {step}[/cyan]...")
                result = subprocess.run(["qmd", step], text=True)
                if result.returncode != 0:
                    error_console.print(f"  [red]Failed[/red] qmd {step}")
                    failed = True
                    break
                else:
                    console.print(f"  [green]Done[/green] qmd {step}")
    else:
        console.print("[dim]Skipping qmd index update.[/dim]")

    # --- Learning manifests ---
    console.rule("[bold cyan]Regenerating learning manifests[/bold cyan]")
    tech_root = vault_path / "technologies"
    manifest_count = 0
    if tech_root.is_dir():
        for tech_dir in sorted(tech_root.iterdir()):
            if not tech_dir.is_dir():
                continue
            content = generate_learning_manifest(tech_dir)
            if content is None:
                continue
            manifest_path = tech_dir / "learning-manifest.md"
            if manifest_path.exists() and manifest_path.read_text() == content:
                continue
            manifest_path.write_text(content)
            console.print(f"  [green]Generated[/green] {tech_dir.name}/learning-manifest.md")
            manifest_count += 1
    if manifest_count:
        console.print(f"  {manifest_count} manifest(s) updated.")
    else:
        console.print("  [dim]All manifests up to date.[/dim]")

    # --- Summary ---
    console.print()
    if failed:
        error_console.print("Refresh completed with errors.")
        raise typer.Exit(1)
    else:
        console.print("[bold green]Refresh complete.[/bold green]")
