"""mnemosyne broker — manage the SCION broker service file."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

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

    # Phase 33.3 SBR-3.3 D-17: Path-unit watchdog (Task 04.2a). Linux-only —
    # gracefully skipped on macOS / non-linux platforms.
    if platform_name == "linux":
        try:
            units = broker.install_path_unit_watchdog()
            for _name, path in units.items():
                console.print(f"Wrote {path}")
        except FileNotFoundError as e:
            console.print(f"[yellow]Path-unit watchdog skipped — {e}[/yellow]")

    # Phase 33.3 SBR-3.4 D-19: pre-warm empiria-claude (unconditional this wave).
    console.print(
        "Pre-warming empiria-claude:latest (may take up to 10 min on first run)..."
    )
    if broker.prewarm_empiria_claude():
        console.print("Pre-warm completed.")
    else:
        console.print(
            "[yellow]Pre-warm failed or skipped (non-fatal). "
            "First `scion start` may incur ID-mapped chown.[/yellow]"
        )

    # Phase 33.3 SBR-3.7 D-31: apply-empiria-defaults convergence at end of run.
    console.print("Applying Empiria-canonical operator settings...")
    try:
        defaults = broker.apply_empiria_defaults(dry_run=False)
        for p in defaults.written:
            console.print(f"Wrote {p}")
        for p in defaults.unchanged:
            console.print(f"[dim]Already canonical: {p}[/dim]")
    except FileNotFoundError as e:
        console.print(f"[yellow]apply-empiria-defaults skipped — {e}[/yellow]")
        console.print(
            "After running `scion init`, re-run "
            "`mnemosyne broker apply-empiria-defaults`."
        )

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


# ---------------------------------------------------------------------------
# Phase 33.3 — SBR-3.1 / SBR-3.7 verbs
# ---------------------------------------------------------------------------


@app.command("start")
def start() -> NoReturn:
    """systemd ExecStart shim (Phase 33.3 SBR-3.1 D-04).

    Applies the Empiria harness-config overlay (chattr -i -> write canonical ->
    chattr +i) then exec's `scion broker start -p local`, replacing this Python
    process so systemd's Type=forking PID model sees scion as the main PID.

    Overlay failures are logged to stderr but do NOT prevent the broker from
    starting — a drift bug must not become a broker outage.
    """
    try:
        result = broker.apply_harness_config_overlay()
        for p in result.written:
            # journalctl captures stderr — `journalctl --user -u scion-broker`.
            print(f"[mnemosyne broker start] wrote {p}", file=sys.stderr)
    except FileNotFoundError as e:
        print(
            f"[mnemosyne broker start] overlay failed "
            f"(non-fatal — vault seed dir missing): {e}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001 — overlay failure must never block start
        print(
            f"[mnemosyne broker start] overlay failed (non-fatal): {e}",
            file=sys.stderr,
        )

    os.execvp("scion", ["scion", "broker", "start", "-p", "local"])
    # execvp does not return on success; if it returns at all, Python's
    # OSError propagates and systemd's Restart=on-failure will retry.


@app.command("restore-config")
def restore_config(
    seed_dir: Path = typer.Option(
        None,
        "--seed-dir",
        help=(
            "Override the canonical seed dir "
            "(default: $MNEMOSYNE_VAULT/agents/scion-template/claude-harness-config/)."
        ),
    ),
) -> None:
    """Re-apply Empiria harness-config seed to ~/.scion/harness-configs/claude/.

    Phase 33.3 SBR-3.1 tactical fix for "the broker just clobbered my
    harness-config". Idempotent — already-canonical files are not re-written.
    Toggles chattr -i / +i around the writes.
    """
    try:
        result = broker.apply_harness_config_overlay(seed_dir=seed_dir)
    except FileNotFoundError as e:
        error_console.print(str(e))
        raise typer.Exit(1)

    for written in result.written:
        console.print(f"Wrote {written}")
    for unchanged in result.unchanged:
        console.print(f"[dim]Already canonical: {unchanged}[/dim]")
    for skipped in result.skipped:
        console.print(f"[yellow]Skipped (seed file missing): {skipped}[/yellow]")

    if not result.written and not result.unchanged and not result.skipped:
        console.print("Nothing to do.")


@app.command("apply-empiria-defaults")
def apply_empiria_defaults_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without writing."
    ),
) -> None:
    """Write canonical Empiria settings to user, grove, and harness-config surfaces.

    Phase 33.3 SBR-3.7 tier-2. Idempotent and overwrite-on-mismatch (D-32).
    For grove settings.yaml: writes the whole file. For user settings.yaml:
    field-level merge (only fields Empiria manages — auth_selected_type, profile
    env). Comments and unrelated keys in grove files are not preserved (D-32).

    Pre-flight: requires ~/.scion/settings.yaml to exist (run `scion init` first).
    """
    try:
        result = broker.apply_empiria_defaults(dry_run=dry_run)
    except FileNotFoundError as e:
        error_console.print(str(e))
        raise typer.Exit(1)

    # Escape the brackets — Rich treats "[would write]" as a markup tag otherwise.
    prefix = r"\[would write]" if dry_run else "Wrote"
    for written in result.written:
        console.print(f"{prefix} {written}")
    for unchanged in result.unchanged:
        console.print(f"[dim]Already canonical: {unchanged}[/dim]")
    for skipped in result.skipped:
        console.print(f"[yellow]Skipped: {skipped}[/yellow]")

    if dry_run:
        console.print(f"\nWould apply {len(result.written)} canonical updates.")
    else:
        console.print(f"\nApplied {len(result.written)} canonical updates.")


@app.command("check-control-channel")
def check_control_channel_cmd(
    restart_if_stale: bool = typer.Option(
        False,
        "--restart-if-stale",
        help="If broker is stale/disconnected, run `systemctl --user restart scion-broker`.",
    ),
) -> None:
    """Phase 33.3 SBR-3.3 helper (CONTEXT D-38).

    Used by both operators (manual check) and the systemd Path-unit watchdog
    (with --restart-if-stale).

    Exit codes (per D-38 — successful auto-recovery MUST exit 0 to avoid burning
    the Path-unit's StartLimitBurst slots on every recovery):

      | Check | --restart-if-stale | Restart returncode | exit |
      |-------|--------------------|--------------------|------|
      | PASS  | (either)           | (not run)          |  0   |
      | FAIL  | False              | (not run)          |  1   |
      | FAIL  | True               | == 0               |  0   |  <- successful recovery
      | FAIL  | True               | != 0               |  1   |  <- restart actually failed
    """
    result = broker.check_control_channel()
    if result.ok:
        console.print(f"[green]ok[/green] {result.message}")
        return  # exit 0

    # Check returned FAIL.
    console.print(f"[red]fail[/red] {result.message}")

    if not restart_if_stale:
        # Diagnostic mode — surface FAIL to operator with non-zero exit.
        raise typer.Exit(1)

    # --restart-if-stale set: attempt recovery.
    console.print("Restarting scion-broker...")
    proc = subprocess.run(
        ["systemctl", "--user", "restart", "scion-broker"],
        check=False,
    )
    if proc.returncode == 0:
        console.print("Restart issued successfully.")
        return  # exit 0 — successful auto-recovery, do NOT burn StartLimitBurst slot
    console.print(
        f"[red]Restart subprocess failed (returncode={proc.returncode})[/red]"
    )
    raise typer.Exit(1)
