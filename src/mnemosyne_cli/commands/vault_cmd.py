"""Vault registry management commands.

Provides `mnemosyne vault register/list/remove/create/rule` subcommands for
managing multiple vault registrations and directional read rules between vaults.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib.vault import (
    VaultConfig,
    _read_config,
    _write_config,
    can_read,
    get_vault_rules,
    read_vaults_config,
    remove_vault_from_config,
    write_vault_to_config,
)

app = typer.Typer(no_args_is_help=True)
console = Console()
error_console = Console(stderr=True, style="bold red")

# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


@app.command("register")
def register(
    name: Annotated[str, typer.Argument(help="Short identifier for this vault (e.g. 'empiria', 'personal')")],
    path: Annotated[str, typer.Argument(help="Absolute or ~ path to the vault root directory")],
    sync: Annotated[str, typer.Option(help="Sync mechanism: git | nextcloud | obsidian-sync")] = "git",
    description: Annotated[str, typer.Option(help="Human-readable description of this vault")] = "",
    force: Annotated[bool, typer.Option("--force", help="Overwrite if vault name already registered")] = False,
) -> None:
    """Register a vault in the multi-vault registry."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        error_console.print(f"Path does not exist or is not a directory: {resolved}")
        raise typer.Exit(1)

    existing = {v.name: v for v in read_vaults_config()}
    if name in existing and not force:
        error_console.print(
            f"Vault '{name}' is already registered at {existing[name].path}. "
            "Use --force to overwrite."
        )
        raise typer.Exit(1)

    vault = VaultConfig(name=name, path=resolved, description=description, sync=sync)
    write_vault_to_config(vault)
    console.print(f"Registered vault '[bold]{name}[/bold]' at {resolved}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@app.command("list")
def list_vaults() -> None:
    """List all registered vaults and their read rules."""
    vaults = read_vaults_config()
    if not vaults:
        console.print("[dim]No vaults registered. Use `mnemosyne vault register` to add one.[/dim]")
        return

    rules = get_vault_rules()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Sync")
    table.add_column("Description")
    table.add_column("Can Read")

    for v in vaults:
        can_read_list = rules.get(v.name, [])
        can_read_str = ", ".join(can_read_list) if can_read_list else "[dim]—[/dim]"
        table.add_row(v.name, str(v.path), v.sync, v.description or "[dim]—[/dim]", can_read_str)

    console.print(table)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


@app.command("remove")
def remove(
    name: Annotated[str, typer.Argument(help="Name of the vault to remove from the registry")],
) -> None:
    """Remove a vault from the registry (does NOT delete the directory)."""
    existing = {v.name for v in read_vaults_config()}
    if name not in existing:
        error_console.print(f"No vault named '{name}' is registered.")
        raise typer.Exit(1)

    remove_vault_from_config(name)
    console.print(f"Removed vault '[bold]{name}[/bold]' from registry (directory untouched).")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

_MINIMAL_AGENTS_MD = """\
# Agent Instructions

This vault is a Mnemosyne knowledge vault. See the [Mnemosyne documentation](https://github.com/empiria/mnemosyne) for vault conventions.

## Vault Structure

- `projects/` — project notes and GSD planning directories
- `technologies/` — technology knowledge notes
- `agents/` — agent prompts and discovery guides
- `templates/` — note templates
- `bases/` — Obsidian Bases views
- `docs/` — vault documentation

## Searching for prior art

Before planning new work or investigating issues, search the vault for relevant prior experience:

```bash
qmd query "<concept>" -c mnemosyne
qmd search "<keyword>" -c mnemosyne --files
```
"""


@app.command("create")
def create(
    path: Annotated[str, typer.Argument(help="Path where the new vault will be created")],
    name: Annotated[Optional[str], typer.Option(help="Registry name for this vault (defaults to directory name)")] = None,
    register: Annotated[bool, typer.Option("--register/--no-register", help="Register the vault in config.toml after creation")] = True,
) -> None:
    """Scaffold a new vault with standard directory structure.

    Creates the directory structure that agents and the CLI expect. This IS the
    shared schema — it defines folder conventions across all vaults.

    Schema files like technologies/knowledge-standards.md are Empiria-specific
    and are NOT copied. A future enhancement could add a --from-template <vault>
    flag to copy schema files from an existing vault, but this is explicitly
    deferred to keep vault creation generic and not tied to Empiria conventions.
    """
    vault_path = Path(path).expanduser().resolve()
    if vault_path.exists() and any(vault_path.iterdir()):
        error_console.print(f"Directory already exists and is not empty: {vault_path}")
        raise typer.Exit(1)

    vault_path.mkdir(parents=True, exist_ok=True)

    # Standard directory structure
    for subdir in ("projects", "technologies", "agents", "templates", "bases", "docs"):
        (vault_path / subdir).mkdir(exist_ok=True)

    # Minimal AGENTS.md
    (vault_path / "AGENTS.md").write_text(_MINIMAL_AGENTS_MD)

    # git init
    subprocess.run(["git", "init"], cwd=vault_path, check=True, capture_output=True)

    console.print(f"Created vault at [bold]{vault_path}[/bold]")

    vault_name = name or vault_path.name
    if register:
        vault = VaultConfig(name=vault_name, path=vault_path)
        write_vault_to_config(vault)
        console.print(f"Registered vault '[bold]{vault_name}[/bold]' in config.toml")


# ---------------------------------------------------------------------------
# rule
# ---------------------------------------------------------------------------


@app.command("rule")
def rule(
    from_vault: Annotated[str, typer.Argument(help="Vault that gains read access")],
    can_read_target: Annotated[str, typer.Option("--can-read", help="Vault that from_vault is allowed to read")],
    remove: Annotated[bool, typer.Option("--remove", help="Remove the rule instead of adding it")] = False,
) -> None:
    """Add or remove a directional read rule between two vaults.

    Example: mnemosyne vault rule personal --can-read empiria
    grants 'personal' read access to 'empiria'.
    """
    registered = {v.name for v in read_vaults_config()}
    if from_vault not in registered:
        error_console.print(f"Vault '{from_vault}' is not registered. Register it first.")
        raise typer.Exit(1)
    if can_read_target not in registered:
        error_console.print(f"Vault '{can_read_target}' is not registered. Register it first.")
        raise typer.Exit(1)

    data = _read_config()
    rules: list[dict] = data.get("vault_rules", [])

    # Find existing rule for from_vault
    existing_idx = next((i for i, r in enumerate(rules) if r.get("from") == from_vault), None)

    if remove:
        if existing_idx is None:
            error_console.print(f"No rule found for vault '{from_vault}'.")
            raise typer.Exit(1)
        current = list(rules[existing_idx].get("can_read", []))
        if can_read_target not in current:
            error_console.print(f"'{from_vault}' does not have a rule to read '{can_read_target}'.")
            raise typer.Exit(1)
        current.remove(can_read_target)
        if current:
            rules[existing_idx] = {"from": from_vault, "can_read": current}
        else:
            rules.pop(existing_idx)
        action_msg = f"Removed rule: '[bold]{from_vault}[/bold]' can no longer read '[bold]{can_read_target}[/bold]'"
    else:
        if existing_idx is not None:
            current = list(rules[existing_idx].get("can_read", []))
            if can_read_target not in current:
                current.append(can_read_target)
            rules[existing_idx] = {"from": from_vault, "can_read": current}
        else:
            rules.append({"from": from_vault, "can_read": [can_read_target]})
        action_msg = f"Rule: '[bold]{from_vault}[/bold]' can read '[bold]{can_read_target}[/bold]'"

    if rules:
        data["vault_rules"] = rules
    elif "vault_rules" in data:
        del data["vault_rules"]
    _write_config(data)

    console.print(action_msg)
