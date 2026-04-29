"""mnemosyne broker — manage the SCION broker service file."""

from __future__ import annotations

import typer
from rich.console import Console

from mnemosyne_cli.lib import broker, vault

app = typer.Typer(no_args_is_help=True, help="Manage the SCION broker service file.")
console = Console()
error_console = Console(stderr=True, style="bold red")


@app.command("install")
def install(
    force: bool = typer.Option(
        False, "--force", help="Rewrite the service file from scratch (destroys customisations)."
    ),
) -> None:
    """Install or update the broker service file from config.toml.

    First run writes a fresh service file (systemd unit on Linux, launchd plist on macOS)
    with `MNEMOSYNE_VAULT_HOST` derived from `vault_path` in config.toml.

    Subsequent runs only patch `MNEMOSYNE_VAULT_HOST` in the existing file — user
    customisations (SSH_AUTH_SOCK, PATH, log paths) are preserved. Use --force to
    regenerate from defaults.
    """
    try:
        platform_name = broker.detect_platform()
    except RuntimeError as e:
        error_console.print(str(e))
        raise typer.Exit(1)

    vault_path = vault.resolve_vault_path()

    try:
        result = broker.install_service(vault_path, force=force)
    except FileNotFoundError as e:
        error_console.print(str(e))
        raise typer.Exit(1)

    if result.created:
        console.print(f"Wrote {result.path}")
    elif result.changed:
        console.print(f"Patched MNEMOSYNE_VAULT_HOST in {result.path}")
    else:
        console.print(f"{result.path} already up to date")
        return

    console.print(f"\nReload the broker:\n  {broker.reload_command(platform_name)}")


@app.command("show")
def show() -> None:
    """Show the broker service file path for this platform."""
    try:
        path = broker.service_file_path()
    except RuntimeError as e:
        error_console.print(str(e))
        raise typer.Exit(1)
    console.print(str(path))
    if not path.exists():
        console.print("[dim](does not exist — run `mnemosyne broker install`)[/dim]")
