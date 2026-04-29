"""mnemosyne shellenv — emit shell exports derived from config.toml.

Lets users keep `~/.config/mnemosyne/config.toml` as the only place a vault
path lives. Add to your shell init file:

    eval "$(mnemosyne shellenv)"             # bash, zsh
    mnemosyne shellenv --shell fish | source # fish
    execx($(mnemosyne shellenv --shell xonsh)) # xonsh
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console

from mnemosyne_cli.lib import vault

error_console = Console(stderr=True, style="bold red")

_SHELLS = {"bash", "zsh", "sh", "fish", "xonsh"}


def _detect_shell() -> str:
    return os.path.basename(os.environ.get("SHELL", "")) or "bash"


def _format(shell: str, vault_path: Path) -> str:
    p = str(vault_path)
    if shell == "fish":
        return (
            f'set -gx MNEMOSYNE_VAULT "{p}"\n'
            f'set -gx MNEMOSYNE_VAULT_HOST "{p}"\n'
        )
    if shell == "xonsh":
        return (
            f'$MNEMOSYNE_VAULT = "{p}"\n'
            f'$MNEMOSYNE_VAULT_HOST = "{p}"\n'
        )
    # bash, zsh, sh, and unknown POSIX-ish shells
    return (
        f'export MNEMOSYNE_VAULT="{p}"\n'
        f'export MNEMOSYNE_VAULT_HOST="{p}"\n'
    )


def run(
    shell: str = typer.Option(
        None,
        "--shell",
        help="Shell flavour (bash, zsh, fish, xonsh). Auto-detected from $SHELL when omitted.",
    ),
) -> None:
    """Emit shell exports for MNEMOSYNE_VAULT and MNEMOSYNE_VAULT_HOST."""
    chosen = shell or _detect_shell()
    if chosen not in _SHELLS:
        # Fall through to POSIX shape rather than error — the values are still useful.
        pass

    vault_path = vault.resolve_vault_path()
    typer.echo(_format(chosen, vault_path), nl=False)
