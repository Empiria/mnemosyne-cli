"""mnemosyne config — read and write CLI configuration."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault

app = typer.Typer(no_args_is_help=True, help="Read and write CLI configuration.")
console = Console()
error_console = Console(stderr=True, style="bold red")

_KNOWN_KEYS = {"vault_path"}


@app.command("set")
def set_key(
    key: str = typer.Argument(help="Config key to set."),
    value: str = typer.Argument(help="Value to set."),
) -> None:
    """Set a configuration value."""
    if key not in _KNOWN_KEYS:
        error_console.print(f"Unknown config key: {key}")
        error_console.print(f"Known keys: {', '.join(sorted(_KNOWN_KEYS))}")
        raise typer.Exit(1)

    if key == "vault_path":
        p = Path(value).expanduser().resolve()
        if not p.is_dir():
            error_console.print(f"Path does not exist: {p}")
            raise typer.Exit(1)
        vault.save_vault_path(p)
        console.print(f"vault_path = {p}")


@app.command("get")
def get_key(
    key: str = typer.Argument(help="Config key to read."),
) -> None:
    """Read a configuration value."""
    if key not in _KNOWN_KEYS:
        error_console.print(f"Unknown config key: {key}")
        error_console.print(f"Known keys: {', '.join(sorted(_KNOWN_KEYS))}")
        raise typer.Exit(1)

    if key == "vault_path":
        p = vault._read_config_vault_path()
        if p:
            console.print(str(p))
        else:
            console.print(f"[dim]vault_path not set in {vault._CONFIG_PATH}[/dim]")


@app.command("path")
def show_path() -> None:
    """Show the config file path."""
    console.print(str(vault._CONFIG_PATH))
