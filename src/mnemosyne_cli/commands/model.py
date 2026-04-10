"""mnemosyne model — manage subagent model selection."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib.models import (
    DEFAULT_PROFILE,
    MODEL_PROFILES,
    VALID_MODELS,
    VALID_PROFILES,
    clear_override,
    get_global_profile,
    get_overrides,
    get_profile,
    resolve_all,
    resolve_model,
    set_global_profile,
    set_override,
    set_profile,
)

app = typer.Typer(no_args_is_help=True, help="Manage subagent model selection.")
console = Console()
error_console = Console(stderr=True, style="bold red")


def _find_planning_dir() -> Path:
    """Locate .planning/ from the current directory."""
    planning = Path.cwd() / ".planning"
    if planning.exists():
        # Resolve symlinks so we read the actual config
        return planning.resolve() if planning.is_symlink() else planning
    error_console.print(
        "No .planning/ directory found in current directory.\n"
        "Run this command from a project root with an initialised .planning/ directory."
    )
    raise typer.Exit(1)


@app.command("resolve")
def resolve(
    agent_type: str = typer.Argument(help="Agent type to resolve (e.g. mnemosyne-codebase-mapper)."),
) -> None:
    """Resolve the model for an agent type.  Prints the model alias to stdout.

    Designed for scripting — skills call this to get the model before spawning agents:

        MODEL=$(mnemosyne model resolve mnemosyne-codebase-mapper)
    """
    planning = _find_planning_dir()
    result = resolve_model(agent_type, planning)
    # Print raw value only — no formatting, no Rich markup
    print(result)


@app.command("profile")
def profile_cmd(
    name: str = typer.Argument(None, help="Profile to activate (quality/balanced/budget/inherit)."),
    global_: bool = typer.Option(False, "--global", help="Set/show the machine-wide default instead of per-project."),
) -> None:
    """Show or set the active model profile.

    Without arguments, shows the current profile (and its source).
    With a profile name, switches to it.
    Use --global to set the machine-wide default in ~/.config/mnemosyne/config.toml.
    """
    if global_:
        if name is None:
            current = get_global_profile()
            if current:
                console.print(f"Global default profile: [bold]{current}[/bold]")
            else:
                console.print(f"No global default set (falls back to [bold]{DEFAULT_PROFILE}[/bold])")
            return

        if name not in VALID_PROFILES:
            error_console.print(f"Unknown profile: {name}")
            error_console.print(f"Valid profiles: {', '.join(VALID_PROFILES)}")
            raise typer.Exit(1)

        set_global_profile(name)
        console.print(f"Global default profile set to [bold]{name}[/bold]")
        return

    planning = _find_planning_dir()

    if name is None:
        from mnemosyne_cli.lib.models import _read_planning_config
        cfg = _read_planning_config(planning)
        project_profile = cfg.get("model_profile")
        global_profile = get_global_profile()
        effective = get_profile(planning)

        if project_profile:
            console.print(f"Active profile: [bold]{effective}[/bold] (project)")
        elif global_profile:
            console.print(f"Active profile: [bold]{effective}[/bold] (global default)")
        else:
            console.print(f"Active profile: [bold]{effective}[/bold] (hardcoded default)")
        return

    if name not in VALID_PROFILES:
        error_console.print(f"Unknown profile: {name}")
        error_console.print(f"Valid profiles: {', '.join(VALID_PROFILES)}")
        raise typer.Exit(1)

    set_profile(planning, name)
    console.print(f"Profile set to [bold]{name}[/bold] (project)")

    # Show the resulting mappings
    _print_resolution_table(planning)


@app.command("list")
def list_cmd() -> None:
    """Show resolved models for all agent types under the active profile."""
    planning = _find_planning_dir()
    _print_resolution_table(planning)


@app.command("override")
def override_cmd(
    agent_type: str = typer.Argument(help="Agent type (e.g. mnemosyne-executor)."),
    model: str = typer.Argument(None, help="Model alias (opus/sonnet/haiku), or omit to clear."),
) -> None:
    """Set or clear a per-agent model override.

    Overrides take precedence over the active profile.
    Omit the model argument to clear an existing override.
    """
    planning = _find_planning_dir()

    if agent_type not in MODEL_PROFILES:
        error_console.print(f"Unknown agent type: {agent_type}")
        error_console.print(f"Known types: {', '.join(sorted(MODEL_PROFILES))}")
        raise typer.Exit(1)

    if model is None:
        clear_override(planning, agent_type)
        console.print(f"Cleared override for [bold]{agent_type}[/bold]")
    else:
        if model not in VALID_MODELS:
            error_console.print(f"Unknown model: {model}")
            error_console.print(f"Valid models: {', '.join(VALID_MODELS)}")
            raise typer.Exit(1)
        set_override(planning, agent_type, model)
        console.print(f"Override set: [bold]{agent_type}[/bold] → [bold]{model}[/bold]")

    _print_resolution_table(planning)


def _print_resolution_table(planning: Path) -> None:
    """Print a table of agent types and their resolved models."""
    current_profile = get_profile(planning)
    overrides = get_overrides(planning)
    resolved = resolve_all(planning)

    table = Table(title=f"Model Resolution (profile: {current_profile})")
    table.add_column("Agent Type", style="cyan")
    table.add_column("Profile Default", style="dim")
    table.add_column("Override", style="yellow")
    table.add_column("Resolved", style="bold green")

    for agent_type in sorted(MODEL_PROFILES):
        profile_default = MODEL_PROFILES[agent_type].get(current_profile, "—")
        override = overrides.get(agent_type, "")
        final = resolved[agent_type]
        table.add_row(agent_type, profile_default, override or "—", final)

    console.print(table)
