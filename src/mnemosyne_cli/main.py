"""Mnemosyne CLI entry point."""

from __future__ import annotations

import typer

from mnemosyne_cli.commands import add, agent, component, config, doctor, generate, hook, init, merge_driver, model, refresh, status, statusline_cmd, vault_cmd, work

app = typer.Typer(
    name="mnemosyne",
    no_args_is_help=True,
    help="Vault operational tools for Mnemosyne.",
)

app.command("init")(init.run)
app.command("doctor")(doctor.run)
app.command("status")(status.run)
app.command("add")(add.run)
app.command("refresh")(refresh.run)
app.add_typer(generate.app, name="generate", help="Generate derived vault artifacts.")
app.add_typer(agent.app, name="agent", help="Manage agent containers.")
app.add_typer(config.app, name="config", help="Read and write CLI configuration.")
app.add_typer(model.app, name="model", help="Manage subagent model selection.")
app.add_typer(work.app, name="work", help="Manage worktree-based work sessions.")
app.add_typer(vault_cmd.app, name="vault", help="Manage vault registry.")
app.add_typer(merge_driver.app, name="merge-driver", help="Git merge drivers for GSD files.")
app.add_typer(hook.app, name="hook", help="Git hook handlers.")
app.add_typer(component.app, name="component", help="Manage multi-repo project component paths.")
app.command("statusline")(statusline_cmd.run)

if __name__ == "__main__":
    app()
