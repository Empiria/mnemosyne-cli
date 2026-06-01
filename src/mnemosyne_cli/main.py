"""Mnemosyne CLI entry point."""

from __future__ import annotations

import typer

from mnemosyne_cli.commands import add, broker, component, config, doctor, generate, hook, init, merge_driver, phase, refresh, shellenv, status, statusline_cmd, vault_cmd

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
app.add_typer(config.app, name="config", help="Read and write CLI configuration.")
app.add_typer(vault_cmd.app, name="vault", help="Manage vault registry.")
app.add_typer(merge_driver.app, name="merge-driver", help="Git merge drivers for GSD files.")
app.add_typer(hook.app, name="hook", help="Git hook handlers.")
app.add_typer(component.app, name="component", help="Manage multi-repo project component paths.")
app.add_typer(broker.app, name="broker", help="Manage the SCION broker service file.")
app.add_typer(phase.app, name="phase", help="Inspect and maintain phase.md cards.")
app.command("shellenv")(shellenv.run)
app.command("statusline")(statusline_cmd.run)

if __name__ == "__main__":
    app()
