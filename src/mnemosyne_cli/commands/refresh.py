"""mnemosyne refresh — pull agent images, update the qmd index, regenerate learning manifests, sync vendored copies."""

from __future__ import annotations

import subprocess
import shutil
from typing import Optional

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault
from mnemosyne_cli.lib import vendoring
from mnemosyne_cli.lib.manifests import generate_learning_manifest

# SCION agent images every Empiria broker host runs. podman never re-pulls
# :latest on its own, so refresh is what keeps these current — a stale pull
# surfaces as agent bootstrap failures at dispatch time.
SCION_IMAGES = [
    "ghcr.io/empiria/empiria-claude:latest",
    "ghcr.io/empiria/empiria-claude-anvil:latest",
]

# Fixed (non-vendored) component names understood by the named-selector.
_FIXED_COMPONENTS = {"images", "qmd", "manifests"}

console = Console()
error_console = Console(stderr=True, style="bold red")


def run(
    components: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Optional component names to refresh. "
            "Fixed names: images, qmd, manifests. "
            "Vendored names match entries in agents/vendored.toml (e.g. anvil-agent-references). "
            "No arguments refreshes everything."
        ),
    ),
    skip_images: bool = typer.Option(False, "--skip-images", help="Skip agent image pull."),
    skip_qmd: bool = typer.Option(False, "--skip-qmd", help="Skip qmd index update."),
) -> None:
    """Pull SCION agent images, refresh the qmd search index, regenerate learning manifests, sync vendored copies."""
    vault_path = vault.resolve_vault_path()
    failed = False

    # Build the set of known component names (fixed + vendored manifest entries)
    vendored_entries = vendoring.load_manifest(vault_path)
    vendored_names = {e["name"] for e in vendored_entries}
    all_known = _FIXED_COMPONENTS | vendored_names

    # Validate any explicitly-supplied component names
    if components:
        unknown = [c for c in components if c not in all_known]
        if unknown:
            for name in unknown:
                error_console.print(
                    f"Unknown component: '{name}'. "
                    f"Known components: {', '.join(sorted(all_known))}"
                )
            raise typer.Exit(1)

    def _wants(name: str) -> bool:
        """Return True if *name* should be processed given the user's selector."""
        if not components:
            return True  # no-args → refresh all
        return name in components

    def _wants_any_vendored() -> bool:
        """True when no selector is given, or any vendored entry name is selected."""
        if not components:
            return True
        return any(c in vendored_names for c in components)

    # --- SCION agent images ---
    if _wants("images") and not skip_images:
        console.rule("[bold cyan]Pulling agent images[/bold cyan]")

        if not shutil.which("podman"):
            console.print("  [dim]podman not found on PATH — skipping image pull.[/dim]")
        else:
            for registry_ref in SCION_IMAGES:
                console.print(f"  Pulling [cyan]{registry_ref}[/cyan]...")
                result = subprocess.run(["podman", "pull", registry_ref], text=True)
                if result.returncode != 0:
                    error_console.print(f"  [red]Failed[/red] to pull {registry_ref}")
                    error_console.print("  Hint: if you see 403, run: podman login ghcr.io")
                    failed = True
                else:
                    console.print(f"  [green]Pulled[/green] {registry_ref}")
    elif not _wants("images") or skip_images:
        if skip_images:
            console.print("[dim]Skipping image pull.[/dim]")

    # --- qmd index ---
    if _wants("qmd") and not skip_qmd:
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
    elif skip_qmd:
        console.print("[dim]Skipping qmd index update.[/dim]")

    # --- Learning manifests ---
    if _wants("manifests"):
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

    # --- Vendored copies ---
    if _wants_any_vendored() and vendored_entries:
        console.rule("[bold cyan]Refreshing vendored copies[/bold cyan]")
        for entry in vendored_entries:
            if not _wants(entry["name"]):
                continue
            try:
                head = vendoring.refresh_entry(entry, vault_path)
                console.print(f"  [green]Staged[/green] {entry['name']} @ {head[:7]}")
            except subprocess.CalledProcessError as exc:
                error_console.print(f"  [red]Failed[/red] to refresh {entry['name']}: {exc}")
                failed = True

    # --- Summary ---
    console.print()
    if failed:
        error_console.print("Refresh completed with errors.")
        raise typer.Exit(1)
    else:
        console.print("[bold green]Refresh complete.[/bold green]")
