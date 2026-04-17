"""mnemosyne component — manage multi-repo project component paths.

Sub-commands:

    mnemosyne component list   — show configured components and on-disk status
    mnemosyne component env    — emit MNEMOSYNE_COMPONENT_<NAME>_HOST=... lines
                                 for the broker service unit
    mnemosyne component check  — exit 0 if all declared components configured
                                 AND on disk, else exit 1 with remediation
"""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib import vault
from mnemosyne_cli.lib.components import (
    ComponentNotCloned,
    ComponentNotConfigured,
    read_components_config,
    resolve_component_path,
)

app = typer.Typer(no_args_is_help=True, help="Manage multi-repo project component paths.")
console = Console()
error_console = Console(stderr=True, style="bold red")


_COMPONENT_FRONTMATTER_RE = re.compile(
    r"^components:\s*\n((?:\s*-\s*name:.*(?:\n(?!\S).*)*)+)", re.MULTILINE
)
_NAME_LINE_RE = re.compile(r"^\s*-\s*name:\s*(\S+)\s*$", re.MULTILINE)


def _read_declared_components(vault_path: Path, project_rel: str) -> list[str]:
    """Read components[*].name from a project note's frontmatter.

    Returns [] if the project note has no components: field.
    """
    project_dir = vault_path / project_rel
    project_slug = project_dir.name
    note_path = project_dir / f"{project_slug}.md"
    if not note_path.is_file():
        return []
    text = note_path.read_text()
    match = _COMPONENT_FRONTMATTER_RE.search(text)
    if not match:
        return []
    return _NAME_LINE_RE.findall(match.group(1))


def _envvar(name: str) -> str:
    """Convert a component name to its broker-side env var name.

    e.g. 'mnemosyne-cli' -> 'MNEMOSYNE_COMPONENT_MNEMOSYNE_CLI_HOST'
    """
    return f"MNEMOSYNE_COMPONENT_{name.upper().replace('-', '_')}_HOST"


@app.command("list")
def list_cmd() -> None:
    """List configured components and their on-disk status."""
    components = read_components_config()
    if not components:
        console.print("[yellow]No components configured in ~/.config/mnemosyne/config.toml[/yellow]")
        console.print("Add a [components.<name>] section per docs/how-to/scion-grove-setup.md.")
        raise typer.Exit(0)

    table = Table(title="Configured components")
    table.add_column("Name")
    table.add_column("local_path")
    table.add_column("Status")
    for name, cfg in sorted(components.items()):
        status = "[green]on disk[/green]" if cfg.exists_on_disk() else "[red]MISSING[/red]"
        table.add_row(name, str(cfg.local_path), status)
    console.print(table)


@app.command("env")
def env_cmd(
    systemd: bool = typer.Option(False, "--systemd", help="Emit Environment= lines for a systemd unit"),
) -> None:
    """Emit MNEMOSYNE_COMPONENT_<NAME>_HOST=... lines.

    Default: KEY=VALUE per line — suitable for sourcing into a shell or
    EnvironmentFile=. With --systemd: emit Environment=KEY=VALUE per line for
    pasting under [Service] in a systemd unit.
    """
    components = read_components_config()
    prefix = "Environment=" if systemd else ""
    for name, cfg in sorted(components.items()):
        print(f"{prefix}{_envvar(name)}={cfg.local_path}")


@app.command("check")
def check_cmd(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Vault project path (default: derive from cwd .planning symlink, fall back to projects/empiria/mnemosyne)",
    ),
) -> None:
    """Exit 0 if all declared components are configured AND on disk.

    Reads the project note's components: list and verifies each one against
    the per-machine config. Prints a remediation report on failure and exits
    non-zero, suitable for use as a SCION pre-start hook or systemd
    ExecStartPre.
    """
    vault_path = vault.resolve_vault_path()

    if project:
        project_rel = project
    else:
        cwd = Path.cwd()
        project_rel = vault.resolve_vault_project(cwd, vault_path) or "projects/empiria/mnemosyne"

    declared = _read_declared_components(vault_path, project_rel)
    if not declared:
        console.print(f"[yellow]No components: declared in {project_rel}/[/yellow]")
        console.print("This project is not multi-repo — no checks to run.")
        raise typer.Exit(0)

    declared_to_check = [c for c in declared if c != "mnemosyne"]

    failures: list[str] = []
    for name in declared_to_check:
        try:
            resolve_component_path(name)
        except ComponentNotConfigured as exc:
            failures.append(exc.remediation())
        except ComponentNotCloned as exc:
            failures.append(exc.remediation())

    if failures:
        error_console.print(
            f"Component pre-flight FAILED for project {project_rel} "
            f"({len(failures)}/{len(declared_to_check)} component(s) unavailable)\n"
        )
        for msg in failures:
            error_console.print(msg, markup=False)
            error_console.print("")
        raise typer.Exit(1)

    console.print(
        f"[green]✓[/green] All {len(declared_to_check)} component(s) "
        f"declared by {project_rel} are configured and present on disk."
    )
