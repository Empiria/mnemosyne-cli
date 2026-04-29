"""SCION broker service file generation and patching.

The mnemosyne CLI installs a platform-native service file that runs the SCION
broker as a user-level service:

- Linux: systemd user unit at ~/.config/systemd/user/scion-broker.service
- macOS: launchd LaunchAgent plist at ~/Library/LaunchAgents/uk.co.empiria.scion-broker.plist

`MNEMOSYNE_VAULT_HOST` (the env var the SCION agent template substitutes into
volume mounts) is derived from ~/.config/mnemosyne/config.toml so that
config.toml is the only place a vault path lives on a given machine.
"""

from __future__ import annotations

import platform
import plistlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SYSTEMD_UNIT_PATH = Path("~/.config/systemd/user/scion-broker.service").expanduser()
LAUNCHD_PLIST_PATH = Path(
    "~/Library/LaunchAgents/uk.co.empiria.scion-broker.plist"
).expanduser()
LAUNCHD_LABEL = "uk.co.empiria.scion-broker"

Platform = Literal["linux", "macos"]


def detect_platform() -> Platform:
    s = platform.system()
    if s == "Linux":
        return "linux"
    if s == "Darwin":
        return "macos"
    raise RuntimeError(f"Unsupported platform for SCION broker: {s}")


def service_file_path(p: Platform | None = None) -> Path:
    p = p or detect_platform()
    return SYSTEMD_UNIT_PATH if p == "linux" else LAUNCHD_PLIST_PATH


def reload_command(p: Platform | None = None) -> str:
    p = p or detect_platform()
    if p == "linux":
        return (
            "systemctl --user daemon-reload && "
            "systemctl --user restart scion-broker"
        )
    return f"launchctl kickstart -k gui/$UID/{LAUNCHD_LABEL}"


def find_scion_bin() -> Path | None:
    for candidate in (
        Path.home() / ".local/bin/scion",
        Path.home() / "go/bin/scion",
        Path("/usr/local/bin/scion"),
    ):
        if candidate.is_file():
            return candidate
    found = shutil.which("scion")
    return Path(found) if found else None


def render_systemd_unit(
    *,
    vault_host: Path,
    scion_bin: Path,
    ssh_auth_sock: str | None = None,
    extra_path: str | None = None,
) -> str:
    env_lines = [f"Environment=MNEMOSYNE_VAULT_HOST={vault_host}"]
    if ssh_auth_sock:
        env_lines.append(f"Environment=SSH_AUTH_SOCK={ssh_auth_sock}")
    if extra_path:
        env_lines.append(f"Environment=PATH={extra_path}")
    env_block = "\n".join(env_lines)

    return f"""[Unit]
Description=SCION Broker
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
{env_block}
ExecStart={scion_bin} broker start -p local
ExecStop={scion_bin} broker stop
PIDFile=%h/.scion/broker.pid
Restart=on-failure

[Install]
WantedBy=default.target
"""


def render_launchd_plist(
    *,
    vault_host: Path,
    scion_bin: Path,
    home: Path,
    path_env: str = "/usr/local/bin:/usr/bin:/bin",
) -> bytes:
    pl = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            str(scion_bin),
            "broker",
            "start",
            "--foreground",
            "--global",
            "-p",
            "local",
        ],
        "EnvironmentVariables": {
            "MNEMOSYNE_VAULT_HOST": str(vault_host),
            "PATH": path_env,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(home / ".scion" / "broker-stdout.log"),
        "StandardErrorPath": str(home / ".scion" / "broker-stderr.log"),
    }
    return plistlib.dumps(pl)


@dataclass
class InstallResult:
    path: Path
    created: bool
    changed: bool


def install_service(
    vault_host: Path,
    *,
    force: bool = False,
    scion_bin: Path | None = None,
) -> InstallResult:
    """Install or update the broker service file.

    - If file does not exist: render fresh and write.
    - If file exists and force=False: only patch MNEMOSYNE_VAULT_HOST so user
      customisations (SSH_AUTH_SOCK, PATH, log paths) are preserved.
    - If force=True: rewrite from scratch (destroys customisations).
    """
    p = detect_platform()
    path = service_file_path(p)

    if path.exists() and not force:
        changed = sync_vault_host(vault_host)
        return InstallResult(path=path, created=False, changed=changed)

    scion = scion_bin or find_scion_bin()
    if scion is None:
        raise FileNotFoundError(
            "Could not find the scion binary. Install scion first, or pass scion_bin explicitly."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if p == "linux":
        path.write_text(
            render_systemd_unit(vault_host=vault_host, scion_bin=scion)
        )
    else:
        path.write_bytes(
            render_launchd_plist(
                vault_host=vault_host, scion_bin=scion, home=Path.home()
            )
        )
    return InstallResult(path=path, created=True, changed=True)


def sync_vault_host(vault_host: Path) -> bool:
    """Patch MNEMOSYNE_VAULT_HOST in the existing service file.

    Returns True if the file changed, False if it didn't exist or already matched.
    """
    p = detect_platform()
    path = service_file_path(p)
    if not path.exists():
        return False

    if p == "linux":
        return _sync_systemd_unit(path, vault_host)
    return _sync_launchd_plist(path, vault_host)


_VAULT_HOST_RE = re.compile(
    r"^Environment=MNEMOSYNE_VAULT_HOST=.*$", re.MULTILINE
)


def _sync_systemd_unit(path: Path, vault_host: Path) -> bool:
    text = path.read_text()
    new_line = f"Environment=MNEMOSYNE_VAULT_HOST={vault_host}"
    if _VAULT_HOST_RE.search(text):
        new_text = _VAULT_HOST_RE.sub(new_line, text)
        if new_text == text:
            return False
        path.write_text(new_text)
        return True
    if "[Service]" not in text:
        raise ValueError(f"Could not find [Service] section in {path}")
    new_text = text.replace("[Service]", f"[Service]\n{new_line}", 1)
    path.write_text(new_text)
    return True


def _sync_launchd_plist(path: Path, vault_host: Path) -> bool:
    with path.open("rb") as f:
        data = plistlib.load(f)
    env = data.setdefault("EnvironmentVariables", {})
    if env.get("MNEMOSYNE_VAULT_HOST") == str(vault_host):
        return False
    env["MNEMOSYNE_VAULT_HOST"] = str(vault_host)
    with path.open("wb") as f:
        plistlib.dump(data, f)
    return True
