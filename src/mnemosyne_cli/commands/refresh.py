"""mnemosyne refresh — pull container images and update qmd index."""

from __future__ import annotations

import subprocess
import shutil

import typer
from rich.console import Console

from pathlib import Path

from mnemosyne_cli.lib import vault
from mnemosyne_cli.lib.manifests import generate_learning_manifest

# CLI repo root — only needed when --build is used
_CLI_ROOT = Path(__file__).resolve().parent.parent.parent.parent

console = Console()
error_console = Console(stderr=True, style="bold red")


def run(
    skip_images: bool = typer.Option(False, "--skip-images", help="Skip container image pull/build."),
    skip_qmd: bool = typer.Option(False, "--skip-qmd", help="Skip qmd index update."),
    build: bool = typer.Option(False, "--build", help="Build images locally from Containerfiles instead of pulling from registry."),
) -> None:
    """Pull container images from registry and refresh the qmd search index."""
    vault_path = vault.resolve_vault_path()
    failed = False

    # --- Container images ---
    if not skip_images:
        if build:
            console.rule("[bold cyan]Rebuilding container images[/bold cyan]")

            containers_dir = _CLI_ROOT / "containers"
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
            console.rule("[bold cyan]Pulling container images[/bold cyan]")

            for name in ["mnemosyne-base", "mnemosyne-claude"]:
                registry_ref = f"ghcr.io/empiria/{name}:latest"
                local_ref = f"localhost/{name}:latest"

                console.print(f"  Pulling [cyan]{registry_ref}[/cyan]...")
                result = subprocess.run(
                    ["podman", "pull", registry_ref],
                    text=True,
                )
                if result.returncode != 0:
                    error_console.print(f"  [red]Failed[/red] to pull {name}")
                    error_console.print("  Hint: if you see 403, run: podman login ghcr.io")
                    failed = True
                    break

                console.print(f"  Tagging [cyan]{registry_ref}[/cyan] → [cyan]{local_ref}[/cyan]...")
                tag_result = subprocess.run(
                    ["podman", "tag", registry_ref, local_ref],
                    text=True,
                )
                if tag_result.returncode != 0:
                    error_console.print(f"  [red]Failed[/red] to tag {name}")
                    failed = True
                    break

                console.print(f"  [green]Pulled[/green] {name}")
    else:
        console.print("[dim]Skipping image pull.[/dim]")

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
