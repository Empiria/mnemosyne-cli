"""mnemosyne agent — manage autonomous Claude Code agent containers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from mnemosyne_cli.lib import git as lib_git
from mnemosyne_cli.lib import vault

app = typer.Typer(no_args_is_help=True, help="Manage agent containers.")
console = Console()
error_console = Console(stderr=True, style="bold red")

# CLI repo root: this file lives at src/mnemosyne_cli/commands/agent.py
# so four levels up (commands -> mnemosyne_cli -> src -> repo root)
_CLI_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_AGENT_ENV_PATH = Path("~/.config/mnemosyne/agent.env").expanduser()
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_IMAGE_NAME = "mnemosyne-claude:latest"
_HAPI_HUB_IMAGE = "mnemosyne-hapi-hub:latest"
_HAPI_HUB_CONTAINER = "mnemosyne-hapi-hub"
_CONTAINER_PREFIX = "mnemosyne-agent-"
_LABEL_KEY = "mnemosyne.project"


def _load_agent_env() -> dict[str, str]:
    """Load agent env config from ~/.config/mnemosyne/agent.env.

    Falls back to environment variables. Returns a dict of key→value pairs.
    Parses KEY=VALUE lines, skipping blanks and # comments.
    """
    env: dict[str, str] = {}

    if _AGENT_ENV_PATH.exists():
        for line in _AGENT_ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()

    return env


def _resolve_token(env_file: dict[str, str], key: str) -> str | None:
    """Resolve a token from env file first, then OS environment."""
    return env_file.get(key) or os.environ.get(key)


def _resolve_claude_credentials() -> str | None:
    """Extract Claude Code credentials JSON.

    On macOS: reads from the system keychain.
    On Linux: reads from ~/.claude/.credentials.json.

    Returns the full JSON string (including refreshToken, expiresAt, etc.)
    or None if not found.  The full credential set is required for features
    like remote-control that need refresh tokens.
    """
    import platform

    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                creds = json.loads(result.stdout.strip())
                if "claudeAiOauth" in creds:
                    return result.stdout.strip()
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    else:
        creds_path = Path("~/.claude/.credentials.json").expanduser()
        if creds_path.exists():
            try:
                raw = creds_path.read_text().strip()
                creds = json.loads(raw)
                if "claudeAiOauth" in creds:
                    return raw
            except (json.JSONDecodeError, OSError):
                pass
    return None


def _container_status(container_name: str) -> str | None:
    """Return container status string, or None if it doesn't exist."""
    check = subprocess.run(
        ["podman", "inspect", container_name, "--format", "{{.State.Status}}"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return check.stdout.strip()
    return None


def _container_is_running(container_name: str) -> bool:
    """Check if a container is already running."""
    return _container_status(container_name) == "running"


def _image_is_current(container_name: str) -> bool:
    """Check if the container was created from the current image."""
    container_img = subprocess.run(
        ["podman", "inspect", container_name, "--format", "{{.Image}}"],
        capture_output=True, text=True,
    )
    current_img = subprocess.run(
        ["podman", "inspect", _IMAGE_NAME, "--format", "{{.Id}}"],
        capture_output=True, text=True,
    )
    if container_img.returncode != 0 or current_img.returncode != 0:
        return False
    return container_img.stdout.strip() == current_img.stdout.strip()


def _refresh_credentials(
    container_name: str,
    credentials_json: str | None,
    oauth_token: str | None,
) -> None:
    """Update credential files in a restarted container."""
    if credentials_json:
        cred_data = credentials_json
    elif oauth_token:
        cred_data = json.dumps({"claudeAiOauth": {"accessToken": oauth_token}})
    else:
        return
    subprocess.run(
        [
            "podman", "exec", "-i", container_name,
            "bash", "-c", "cat > /home/agent/.claude/.credentials.json",
        ],
        input=cred_data, capture_output=True, text=True,
    )


def _ensure_hub_running(port: int = 3006) -> None:
    """Auto-start the hapi hub if it is not already running."""
    if _container_is_running(_HAPI_HUB_CONTAINER):
        return

    console.print("[dim]hapi hub not running — starting it automatically...[/dim]")

    # Remove stopped container if it exists so we can reuse the name
    subprocess.run(["podman", "rm", "-f", _HAPI_HUB_CONTAINER], capture_output=True)

    cmd = [
        "podman", "run", "-d",
        "--name", _HAPI_HUB_CONTAINER,
        "--restart", "unless-stopped",
        "-p", f"{port}:3006",
        "-v", "hapi-state:/home/hapi/.hapi",
        _HAPI_HUB_IMAGE,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error_console.print(f"Failed to auto-start hapi hub: {result.stderr.strip()}")
        error_console.print("Start it manually with: [bold]mnemosyne agent hub[/bold]")
    else:
        console.print("[green]hapi hub started.[/green]")


def _default_project() -> str:
    """Derive project slug from current directory name."""
    return Path.cwd().name


@app.command("start")
def start(
    branch: str = typer.Argument(None, help="Branch to attach to. If omitted, starts container without attaching."),
    project: str = typer.Option(None, "--project", "-p", help="Vault project slug. Defaults to current directory name."),
    repo: Optional[Path] = typer.Option(
        None,
        "--repo",
        help="Path to project repository. Defaults to current working directory.",
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        "-d",
        help="Start container without attaching.",
    ),
    cli_path: Optional[Path] = typer.Option(
        None,
        "--cli-path",
        help="Path to local mnemosyne-cli repo for editable install. If omitted, CLI is installed from PyPI inside the container.",
    ),
) -> None:
    """Launch an agent container for a project.

    Without a branch argument, starts the container and prints the remote-control
    URL. With a branch, also attaches to a bash shell in that branch's worktree.
    """
    project = project or _default_project()
    vault_path = vault.resolve_vault_path()
    container_name = f"{_CONTAINER_PREFIX}{project}"

    # Load env config and resolve credentials (needed for all paths)
    env_file = _load_agent_env()
    credentials_json = _resolve_claude_credentials()
    oauth_token = _resolve_token(env_file, "CLAUDE_CODE_OAUTH_TOKEN")

    # If already running, refresh credentials and attach
    if _container_is_running(container_name):
        console.print(f"[bold]Container already running for[/bold] [cyan]{project}[/cyan]")
        _refresh_credentials(container_name, credentials_json, oauth_token)
        if not branch or detach:
            _print_remote_url(container_name)
            return
        repo_path = (repo or Path.cwd()).resolve()
        existing_wt = vault_path / "worktrees" / project
        _do_attach(
            container_name, branch, repo_path,
            project=project if existing_wt.exists() else None,
        )
        return
    if not credentials_json and not oauth_token:
        error_console.print(
            "No Claude Code credentials found.\n"
            "On macOS, run [bold]claude[/bold] and complete login — credentials are read from the keychain.\n"
            "On Linux, set CLAUDE_CODE_OAUTH_TOKEN in ~/.config/mnemosyne/agent.env."
        )
        raise typer.Exit(1)

    # Try to restart a stopped container if the image hasn't changed.
    # This preserves the rootfs (installed deps, system packages) and skips the
    # entrypoint entirely — the biggest startup-time win.
    status = _container_status(container_name)
    if status is not None:
        if status in ("exited", "stopped", "created") and _image_is_current(container_name):
            console.print(f"[bold]Restarting container for[/bold] [cyan]{project}[/cyan]...")
            result = subprocess.run(
                ["podman", "start", container_name],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                _refresh_credentials(container_name, credentials_json, oauth_token)
                console.print("[green]Container restarted.[/green]")
                if not branch or detach:
                    _print_remote_url(container_name)
                else:
                    existing_wt = vault_path / "worktrees" / project
                    _do_attach(
                        container_name, branch,
                        project=project if existing_wt.exists() else None,
                    )
                return
            # Restart failed — fall through to fresh start
            error_console.print(f"[dim]Restart failed, recreating: {result.stderr.strip()}[/dim]")
        subprocess.run(["podman", "rm", "-f", container_name], capture_output=True)

    # Resolve repo path
    repo_path = (repo or Path.cwd()).resolve()
    if not repo_path.is_dir():
        error_console.print(f"Repo path does not exist: {repo_path}")
        raise typer.Exit(1)

    # Create vault worktree for this project (one per container, not per branch)
    vault_worktree_path: Path | None = None
    vault_worktree_path = vault_path / "worktrees" / project
    if not vault_worktree_path.exists():
        try:
            lib_git.worktree_add(vault_path, vault_worktree_path, project, new_branch=True)
            console.print(f"  Vault worktree: worktrees/{project}/")
        except subprocess.CalledProcessError:
            try:
                lib_git.worktree_add(vault_path, vault_worktree_path, project, new_branch=False)
                console.print(f"  Vault worktree: worktrees/{project}/ (existing branch)")
            except subprocess.CalledProcessError:
                vault_worktree_path = None
                console.print("[yellow]Could not create vault worktree — using main vault[/yellow]")
    else:
        console.print(f"  Vault worktree: worktrees/{project}/ (existing)")

    # Build podman run command — always detached
    cmd: list[str] = [
        "podman",
        "run",
        "-d",
        "--name",
        container_name,
        "--hostname",
        project,
        "--label",
        f"{_LABEL_KEY}={project}",
        "--userns=keep-id",
        "-v",
        f"{repo_path}:{repo_path}",
        "--workdir",
        str(repo_path),
        "-v",
        f"{vault_path}:/vault",
        "-v",
        f"{vault_path}:{vault_path}",
    ]

    # Mount CLI repo for editable install when --cli-path is given;
    # otherwise the entrypoint installs mnemosyne-cli from PyPI.
    cli_root = cli_path.resolve() if cli_path else None
    if cli_root:
        containers_dir = cli_root / "containers"
        cmd.extend(["-v", f"{containers_dir}/config:/config:ro"])
        cmd.extend(["-v", f"{cli_root}:/mnemosyne-cli:ro"])

    # Pass credentials: full JSON from keychain preferred, access token as fallback
    if credentials_json:
        cmd.extend(["-e", f"CLAUDE_CODE_CREDENTIALS={credentials_json}"])
    else:
        cmd.extend(["-e", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}"])

    # SSH agent socket forwarding (only if set)
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_auth_sock:
        cmd.extend([
            "-v",
            f"{ssh_auth_sock}:/run/ssh-agent:Z",
            "-e",
            "SSH_AUTH_SOCK=/run/ssh-agent",
        ])

    # GPU passthrough via CDI (if nvidia-container-toolkit is configured)
    try:
        gpu_check = subprocess.run(
            ["nvidia-ctk", "cdi", "list"],
            capture_output=True, text=True, timeout=2,
        )
        if gpu_check.returncode == 0 and "nvidia.com/gpu=all" in gpu_check.stdout:
            cmd.extend(["--device", "nvidia.com/gpu=all"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # No toolkit, no GPU — CPU fallback

    # qmd: named volume for index persistence, host models bind-mounted read-only
    cmd.extend(["-v", f"qmd-cache-{project}:/home/agent/.cache/qmd"])
    qmd_models = Path("~/.cache/qmd/models").expanduser()
    if qmd_models.is_dir():
        cmd.extend(["-v", f"{qmd_models}:/home/agent/.cache/qmd/models:ro"])

    # Per-project dependency cache (unconditional — empty volume is fine)
    dep_cache_vol = f"dep-cache-{project}"
    cmd.extend(["-v", f"{dep_cache_vol}:/home/agent/.dep-cache"])
    cmd.extend(["-e", "UV_CACHE_DIR=/home/agent/.dep-cache/uv"])
    cmd.extend(["-e", "PLAYWRIGHT_BROWSERS_PATH=/home/agent/.dep-cache/ms-playwright"])
    cmd.extend(["-e", "CARGO_HOME=/home/agent/.dep-cache/cargo"])

    # Per-project container.toml (bind-mount if present)
    if toml_path := _find_container_toml(vault_path, project):
        cmd.extend(["-v", f"{toml_path}:/config/container.toml:ro"])

    # GitHub CLI auth (gh respects GH_TOKEN env var)
    gh_token = _resolve_token(env_file, "GH_TOKEN")
    if gh_token:
        cmd.extend(["-e", f"GH_TOKEN={gh_token}"])

    cmd.extend(["-e", f"MNEMOSYNE_PROJECT={project}"])
    cmd.extend(["-e", f"WORKSPACE_PATH={repo_path}"])

    # Multi-vault mounts: iterate registered vaults and mount readable extras as :ro
    primary_vault = vault.resolve_primary_vault()
    all_vaults = vault.resolve_vaults()
    extra_vault_entries: list[str] = []
    for v in all_vaults:
        # Skip the primary vault (already mounted at /vault)
        if v.path.expanduser().resolve() == vault_path:
            continue
        # Only mount vaults the primary vault is allowed to read (closed by default)
        if not vault.can_read(primary_vault.name, v.name):
            continue
        mount_point = f"/vault-{v.name}"
        cmd.extend(["-v", f"{v.path}:{mount_point}:ro"])
        extra_vault_entries.append(f"{v.name}:{mount_point}")

    # Pass primary vault name so entrypoint can name the qmd collection correctly
    cmd.extend(["-e", f"MNEMOSYNE_PRIMARY_VAULT_NAME={primary_vault.name}"])

    if extra_vault_entries:
        cmd.extend(["-e", f"MNEMOSYNE_EXTRA_VAULTS={' '.join(extra_vault_entries)}"])

    # Pass vault worktree path to container (entrypoint overrides MNEMOSYNE_VAULT with this)
    if vault_worktree_path:
        container_vault_worktree = f"/vault/worktrees/{project}"
        cmd.extend(["-e", f"MNEMOSYNE_VAULT_WORKTREE={container_vault_worktree}"])

    cmd.append(_IMAGE_NAME)

    console.print(f"[bold]Starting agent container for[/bold] [cyan]{project}[/cyan]...")
    console.print(f"  Repo:  {repo_path}")
    console.print(f"  Vault: {vault_path}")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        console.print()
        error_console.print(result.stderr.strip())
        console.print(
            "[yellow]Hints:[/yellow]\n"
            f"  If container already exists: [bold]mnemosyne agent stop {project}[/bold] then retry\n"
            f"  If image not found: [bold]podman build -t mnemosyne-base containers/base[/bold]\n"
            f"    then: [bold]podman build -t mnemosyne-claude containers/claude[/bold]"
        )
        raise typer.Exit(result.returncode)

    console.print(f"[green]Container started.[/green]")

    # Wait for entrypoint to finish (installs mnemosyne CLI, project deps, etc.)
    _wait_for_ready(container_name)

    if not branch or detach:
        _print_remote_url(container_name)
    else:
        _do_attach(
            container_name, branch, repo_path,
            project=project if vault_worktree_path is not None else None,
        )


@app.command("list")
def list_agents() -> None:
    """List running agent containers."""
    cmd = [
        "podman",
        "ps",
        "--filter",
        f"label={_LABEL_KEY}",
        "--format",
        "json",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error_console.print("Failed to query Podman:")
        error_console.print(result.stderr)
        raise typer.Exit(1)

    try:
        containers = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        error_console.print(f"Failed to parse Podman output: {exc}")
        raise typer.Exit(1)

    if not containers:
        console.print("[dim]No running agent containers.[/dim]")
        return

    table = Table(title="Running Agent Containers", show_lines=False)
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Container ID", style="dim")
    table.add_column("Status", style="green")
    table.add_column("Created", style="dim")

    for c in containers:
        # Podman JSON fields
        container_id = (c.get("Id") or "")[:12]
        labels = c.get("Labels") or {}
        project_name = labels.get(_LABEL_KEY, "unknown")
        status = c.get("Status") or c.get("State") or "unknown"
        created = c.get("Created") or c.get("CreatedAt") or ""

        table.add_row(project_name, container_id, status, str(created))

    console.print(table)


@app.command("stop")
def stop(
    project: str = typer.Argument(None, help="Vault project slug. Defaults to current directory name."),
) -> None:
    """Stop and remove an agent container."""
    project = project or _default_project()
    container_name = f"{_CONTAINER_PREFIX}{project}"
    result = subprocess.run(
        ["podman", "rm", "-f", container_name],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        console.print(f"[green]Agent container for {project} stopped.[/green]")
    else:
        error_console.print(f"Failed to stop container: {result.stderr.strip()}")
        raise typer.Exit(1)

    # Report vault worktree status (project-scoped, user decides cleanup)
    try:
        vault_path = vault.resolve_vault_path()
        vault_wt = vault_path / "worktrees" / project
        if vault_wt.exists():
            # Get the actual branch name in the worktree
            wt_list = lib_git.list_worktrees(vault_path)
            wt_branch = next(
                (wt.get("branch", project) for wt in wt_list
                 if Path(wt["worktree"]) == vault_wt),
                project,
            )
            if lib_git.is_branch_merged_to_main(vault_path, wt_branch):
                lib_git.worktree_remove(vault_path, vault_wt)
                console.print(f"Vault worktree worktrees/{project}/ removed (merged).")
            else:
                console.print(
                    f"[yellow]Vault worktree worktrees/{project}/ has unmerged work — kept.[/yellow]\n"
                    f"  Remove manually when ready: "
                    f"git -C {vault_path} worktree remove worktrees/{project}/"
                )
    except (subprocess.CalledProcessError, SystemExit):
        pass  # Vault not configured or error — skip


@app.command("rebuild")
def rebuild(
    branch: str = typer.Argument(None, help="Branch to attach to after rebuild. If omitted, starts without attaching."),
    project: str = typer.Option(None, "--project", "-p", help="Vault project slug. Defaults to current directory name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove dependency cache and restart container for a project."""
    project = project or _default_project()
    dep_cache_vol = f"dep-cache-{project}"
    container_name = f"{_CONTAINER_PREFIX}{project}"

    if not yes:
        confirmed = typer.confirm(
            f"Remove dependency cache volume '{dep_cache_vol}' and restart {project}?"
        )
        if not confirmed:
            raise typer.Exit(0)

    # Stop container if running
    subprocess.run(["podman", "rm", "-f", container_name], capture_output=True)

    # Remove cache volume (tolerate "no such volume")
    result = subprocess.run(
        ["podman", "volume", "rm", dep_cache_vol],
        capture_output=True, text=True,
    )
    if result.returncode != 0 and "no such volume" not in result.stderr.lower():
        error_console.print(f"Failed to remove volume: {result.stderr.strip()}")
        raise typer.Exit(1)

    console.print(f"[green]Cache removed. Restarting {project}...[/green]")
    # Delegate to start (pass through project slug and branch)
    start(project=project, branch=branch, repo=None, detach=False)


@app.command("hub")
def hub(
    stop: bool = typer.Option(False, "--stop", help="Stop the running hapi hub."),
    port: int = typer.Option(3006, "--port", "-p", help="Host port to expose."),
    qr: bool = typer.Option(False, "--qr", help="Show QR code for mobile access."),
) -> None:
    """Start (or stop) the hapi hub for mobile access."""
    if stop:
        result = subprocess.run(
            ["podman", "stop", _HAPI_HUB_CONTAINER],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            console.print("[green]hapi hub stopped.[/green]")
        else:
            error_console.print(f"Failed to stop hapi hub: {result.stderr.strip()}")
            raise typer.Exit(1)
        return

    if qr:
        _print_hub_qr(port)
        return

    # Check if already running
    check = subprocess.run(
        ["podman", "inspect", _HAPI_HUB_CONTAINER, "--format", "{{.State.Status}}"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0 and check.stdout.strip() == "running":
        console.print(f"[green]hapi hub is already running on port {port}.[/green]")
        _print_hub_token()
        return

    # Remove stopped container if it exists (so we can reuse the name)
    subprocess.run(
        ["podman", "rm", "-f", _HAPI_HUB_CONTAINER],
        capture_output=True,
    )

    cmd = [
        "podman",
        "run",
        "-d",
        "--name",
        _HAPI_HUB_CONTAINER,
        "--restart",
        "unless-stopped",
        "-p",
        f"{port}:3006",
        "-v",
        "hapi-state:/home/hapi/.hapi",
        _HAPI_HUB_IMAGE,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error_console.print(f"Failed to start hapi hub: {result.stderr.strip()}")
        raise typer.Exit(1)

    console.print(f"[green]hapi hub started on port {port}.[/green]")
    console.print(f"  PWA: http://localhost:{port}")
    _print_hub_token(wait=True)


def _get_hub_token() -> str | None:
    """Read the CLI_API_TOKEN from the running hub."""
    import time

    for i in range(10):
        result = subprocess.run(
            ["podman", "exec", _HAPI_HUB_CONTAINER, "cat", "/home/hapi/.hapi/settings.json"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                settings = json.loads(result.stdout)
                token = settings.get("cliApiToken") or settings.get("CLI_API_TOKEN")
                if token:
                    return token
            except json.JSONDecodeError:
                pass
        if i < 9:
            time.sleep(1)
    return None


def _print_hub_token(*, wait: bool = False) -> None:
    """Print the CLI_API_TOKEN from the running hub."""
    import time

    attempts = 10 if wait else 1
    for i in range(attempts):
        result = subprocess.run(
            ["podman", "exec", _HAPI_HUB_CONTAINER, "cat", "/home/hapi/.hapi/settings.json"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                settings = json.loads(result.stdout)
                token = settings.get("cliApiToken") or settings.get("CLI_API_TOKEN")
                if token:
                    console.print(f"\n  Add to ~/.config/mnemosyne/agent.env:")
                    console.print(f"  [bold]CLI_API_TOKEN={token}[/bold]")
                    return
            except json.JSONDecodeError:
                pass
        if i < attempts - 1:
            time.sleep(1)


def _get_tunnel_url() -> str | None:
    """Extract the relay tunnel URL from hub container logs."""
    import re

    result = subprocess.run(
        ["podman", "logs", _HAPI_HUB_CONTAINER],
        capture_output=True,
        text=True,
    )
    # Look for: http://localhost:3006 <= https://xxx.relay.hapi.run
    for line in result.stdout.splitlines() + result.stderr.splitlines():
        m = re.search(r"(https://[a-z0-9]+\.relay\.hapi\.run)", line)
        if m:
            return m.group(1)
    return None


def _print_remote_url(container_name: str) -> None:
    """Show the remote-control URL, polling briefly for it to appear."""
    import time

    # remote-control starts after entrypoint, then needs seconds to connect
    # to Anthropic and print its URL — poll for up to 20s
    for _ in range(20):
        url = _get_remote_url(container_name)
        if url:
            console.print(f"\n[bold]Remote control URL:[/bold] {url}")
            try:
                subprocess.run(["qrencode", "-t", "UTF8", url])
            except FileNotFoundError:
                pass
            console.print("Open in your browser or on claude.ai/code to connect.\n")
            return
        time.sleep(1)

    console.print(
        "\n[dim]Remote URL not yet available. Run [bold]mnemosyne agent remote[/bold] to check later.[/dim]\n"
    )


def _get_remote_url(container_name: str) -> str | None:
    """Extract the remote-control URL from container logs."""
    import re

    result = subprocess.run(
        ["podman", "logs", container_name],
        capture_output=True, text=True,
    )
    # Look for claude.ai URL in stdout or stderr
    for line in result.stdout.splitlines() + result.stderr.splitlines():
        m = re.search(r"(https://claude\.ai/[^\s]+)", line)
        if m:
            return m.group(1)
    return None


def _print_hub_qr(port: int) -> None:
    """Print a QR code for mobile hub access via relay tunnel."""
    from urllib.parse import urlencode

    token = _get_hub_token()
    if not token:
        error_console.print("Could not read token from hub. Is it running?")
        raise typer.Exit(1)

    tunnel_url = _get_tunnel_url()
    if not tunnel_url:
        error_console.print(
            "No relay tunnel found in hub logs.\n"
            "Ensure the hub is running with relay mode (default)."
        )
        raise typer.Exit(1)

    params = urlencode({"hub": tunnel_url, "token": token})
    url = f"https://app.hapi.run/?{params}"

    console.print(f"\n[bold]Scan this QR code with your phone:[/bold]\n")
    subprocess.run(["qrencode", "-t", "UTF8", url])
    console.print(f"\n  Tunnel: {tunnel_url}")
    console.print(f"\n  URL: {url}")


@app.command("remote")
def remote(
    project: str = typer.Argument(None, help="Vault project slug. Defaults to current directory name."),
) -> None:
    """Show the remote-control URL for connecting from browser or phone."""
    project = project or _default_project()
    container_name = f"{_CONTAINER_PREFIX}{project}"

    if not _container_is_running(container_name):
        error_console.print(f"No running container for {project}.")
        error_console.print(f"Start one with: [bold]mnemosyne agent start <branch>[/bold]")
        raise typer.Exit(1)

    url = _get_remote_url(container_name)
    if not url:
        error_console.print(
            "Could not find remote-control URL in container logs.\n"
            "The container may still be starting up, or remote-control may not be running."
        )
        raise typer.Exit(1)

    console.print(f"\n[bold]Remote control URL:[/bold] {url}\n")
    try:
        subprocess.run(["qrencode", "-t", "UTF8", url])
    except FileNotFoundError:
        pass
    console.print("Open this URL in your browser or on claude.ai/code to connect.")


@app.command("attach")
def attach(
    branch: str = typer.Argument(..., help="Branch name for the work session."),
    project: str = typer.Option(None, "--project", "-p", help="Vault project slug. Defaults to current directory name."),
    repo: Optional[Path] = typer.Option(
        None,
        "--repo",
        help="Path to project repository. Defaults to current working directory.",
    ),
) -> None:
    """Attach to a running agent container and resume a work session on a branch."""
    project = project or _default_project()
    vault_path = vault.resolve_vault_path()
    container_name = f"{_CONTAINER_PREFIX}{project}"
    repo_path = (repo or Path.cwd()).resolve()
    existing_wt = vault_path / "worktrees" / project
    _do_attach(
        container_name, branch, repo_path,
        project=project if existing_wt.exists() else None,
    )


def _wait_for_ready(container_name: str, timeout: int = 120, tail: int = 4) -> None:
    """Block until the entrypoint writes /tmp/.entrypoint-ready.

    Shows a rolling window of the last *tail* container log lines so the
    user can see that progress is being made.
    """
    import time
    from collections import deque
    from rich.live import Live
    from rich.text import Text

    seen_lines = 0
    recent: deque[str] = deque(maxlen=tail)
    deadline = time.monotonic() + timeout

    def _refresh_logs() -> None:
        nonlocal seen_lines
        result = subprocess.run(
            ["podman", "logs", container_name],
            capture_output=True, text=True,
        )
        lines = (result.stdout + result.stderr).splitlines()
        for line in lines[seen_lines:]:
            recent.append(line)
        seen_lines = len(lines)

    with Live(Text("Waiting for entrypoint…", style="dim"), console=console, refresh_per_second=2) as live:
        while time.monotonic() < deadline:
            check = subprocess.run(
                ["podman", "exec", container_name, "test", "-f", "/tmp/.entrypoint-ready"],
                capture_output=True,
            )
            if check.returncode == 0:
                _refresh_logs()
                return
            if not _container_is_running(container_name):
                live.stop()
                logs = subprocess.run(
                    ["podman", "logs", "--tail", "20", container_name],
                    capture_output=True, text=True,
                )
                error_console.print("Container exited during startup:")
                error_console.print(logs.stdout + logs.stderr)
                raise typer.Exit(1)
            _refresh_logs()
            display = Text()
            for line in recent:
                display.append(f"  {line}\n", style="dim")
            live.update(display)
            time.sleep(0.5)

    error_console.print(f"Entrypoint did not complete within {timeout}s")
    raise typer.Exit(1)


def _do_attach(
    container_name: str,
    branch: str,
    repo_path: Path | None = None,
    project: str | None = None,
) -> None:
    """Set up the worktree, then exec into a bash shell.

    Step 1: Create worktree on the host (avoids macOS/Podman bind-mount
            permission issues with .git lock files).
    Step 2: Interactive exec into bash shell. The user can then run
            `claude --remote-control` for an interactive session that is
            also remotely accessible.

    project: when set, MNEMOSYNE_VAULT is scoped to the project vault worktree
    inside the container (e.g. /vault/worktrees/<project>).
    """
    # Determine MNEMOSYNE_VAULT: prefer project-scoped worktree when available
    vault_env = f"/vault/worktrees/{project}" if project else "/vault"

    # Forward host terminal type so the container's TUI gets correct key sequences
    # (e.g. Kitty's xterm-kitty enables enhanced keyboard protocol for Shift+Enter)
    host_term = os.environ.get("TERM", "xterm-256color")
    term_env = ["-e", f"TERM={host_term}"]
    if os.environ.get("COLORTERM"):
        term_env += ["-e", f"COLORTERM={os.environ['COLORTERM']}"]

    # Copy host terminfo into the container so $TERM is recognised
    if host_term != "xterm-256color":
        infocmp = subprocess.run(
            ["infocmp", "-x", host_term],
            capture_output=True, text=True,
        )
        if infocmp.returncode == 0 and infocmp.stdout:
            subprocess.run(
                ["podman", "exec", "-i", container_name, "tic", "-x", "-o",
                 "/home/agent/.terminfo", "/dev/stdin"],
                input=infocmp.stdout, capture_output=True, text=True,
            )
            term_env += ["-e", "TERMINFO=/home/agent/.terminfo"]

    # Use a tmux session so the user can detach and reattach later
    tmux_session = f"terminal-{branch}" if repo_path else "terminal"

    if repo_path is None:
        # Restart path — skip worktree setup, exec into repo root
        # Read WORKSPACE_PATH from the container's environment
        wp_result = subprocess.run(
            ["podman", "exec", container_name, "printenv", "WORKSPACE_PATH"],
            capture_output=True, text=True,
        )
        workdir = wp_result.stdout.strip() if wp_result.returncode == 0 else "/workspace"
        os.execvp(
            "podman",
            [
                "podman", "exec", "-it",
                "-w", workdir,
                "-e", "MNEMOSYNE_CONTAINER=1",
                "-e", f"MNEMOSYNE_VAULT={vault_env}",
                *term_env,
                container_name,
                "tmux", "new-session", "-A", "-s", tmux_session,
            ],
        )
        return  # unreachable after execvp, but keeps type checker happy

    # Step 1: Ensure worktree exists — run on the host where we have real
    # filesystem permissions.  Inside the Podman VM on macOS, bind-mounted
    # .git directories are often read-only for the container user.
    setup = subprocess.run(
        ["mnemosyne", "work", "setup", branch],
        capture_output=True, text=True,
        cwd=repo_path,
    )
    if setup.returncode != 0:
        error_console.print(f"Worktree setup failed: {setup.stderr.strip()}")
        raise typer.Exit(1)

    # Parse worktree path — host paths match container paths (same mount point)
    worktree_path: str | None = None
    for line in (setup.stdout or "").splitlines():
        if line.startswith("WORKTREE_PATH="):
            worktree_path = line.split("=", 1)[1]
            break

    if not worktree_path:
        worktree_path = str(repo_path / "worktrees" / branch)  # fallback

    # Show non-machine-readable lines to the user
    user_lines = [
        ln for ln in (setup.stdout or "").splitlines()
        if not ln.startswith("WORKTREE_PATH=")
    ]
    if user_lines:
        console.print("\n".join(user_lines))

    # Step 2: exec into tmux session (detach/reattach supported)
    os.execvp(
        "podman",
        [
            "podman", "exec", "-it",
            "-w", worktree_path,
            "-e", "MNEMOSYNE_CONTAINER=1",
            "-e", f"MNEMOSYNE_VAULT={vault_env}",
            *term_env,
            container_name,
            "tmux", "new-session", "-A", "-s", tmux_session,
        ],
    )


def _find_container_toml(vault_path: Path, project: str) -> Path | None:
    """Locate container.toml for a project slug via flat glob (unique slug assumption)."""
    matches = list(vault_path.glob(f"projects/*/{project}/container.toml"))
    return matches[0] if matches else None
