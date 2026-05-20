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

import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

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


# ---------------------------------------------------------------------------
# YAML / settings.yaml helpers (Phase 33.3 SBR-3.7)
# ---------------------------------------------------------------------------


def yaml_safe_load_or_none(path: Path) -> dict | None:
    """Read YAML file; return {} for empty, dict for parsed, raise YAMLError on bad input.

    Returns None if the file does not exist (caller decides whether that is WARN or PASS).
    """
    import yaml

    if not path.exists():
        return None
    text = path.read_text()
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# SBR-3.1 + SBR-3.7: Harness-config overlay + operator-state convergence
# ---------------------------------------------------------------------------

EXPECTED_AUTH_SELECTED_TYPE = "oauth-token"
EXPECTED_GROVE_TEMPLATE = "empiria-agent"
EXPECTED_GROVE_HARNESS = "claude"


def get_protected_paths() -> list[Path]:
    """Return the two harness-config paths the chattr lock guards.

    LAZY — re-evaluates at every call so tests that monkeypatch Path.home()
    AFTER import time see the patched home. Per Plan 03 Task 03.2 fix
    (planning revision): a module-level ``PROTECTED_PATHS = [...]`` would
    resolve against the real host at import time and break monkeypatched
    tests. The two scion_paths helpers themselves call Path.home() at call
    time, so this function is idempotent and safe to call repeatedly.
    """
    from mnemosyne_cli.lib.scion_paths import (
        harness_config_claude_json,
        harness_config_yaml,
    )

    return [
        harness_config_claude_json(),
        harness_config_yaml(),
    ]


@dataclass
class OverlayResult:
    """Summary of an overlay / convergence run.

    Falsy when nothing was written — `apply_empiria_defaults` returns this and
    Wave 0 tests rely on `assert not result` for idempotency / `assert result`
    for dry-run-found-changes. `__bool__` keys off `written` only.
    """

    written: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.written)


@dataclass
class CanonicalChange:
    path: Path
    current: dict | None
    target: dict


@contextmanager
def writable(paths: list[Path]) -> Iterator[None]:
    """Temporarily strip chattr +i from existing paths; restore on exit.

    Idempotent: chattr -i on a non-immutable file is a no-op (exit 0).
    check=False so the helper survives EPERM / EROFS / missing-binary failures
    on hosts where chattr is unavailable (e.g., macOS, tmpfs without xattr).

    Implementation note: the paths may not exist when the broker is first
    installed; we only chattr files that currently exist on disk.
    """
    existing = [p for p in paths if p.exists()]
    for p in existing:
        subprocess.run(["chattr", "-i", str(p)], check=False)
    try:
        yield
    finally:
        for p in existing:
            if p.exists():
                subprocess.run(["chattr", "+i", str(p)], check=False)


def _atomic_write(path: Path, content: bytes | str) -> None:
    """Write content to path atomically (tempfile + os.replace)."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, delete=False, prefix=".mnemosyne-tmp-"
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _default_seed_dir() -> Path:
    """Locate the canonical harness-config seed dir in the vault."""
    from mnemosyne_cli.lib import vault

    vault_path = vault.resolve_vault_path()
    return vault_path / "agents" / "scion-template" / "claude-harness-config"


def apply_harness_config_overlay(seed_dir: Path | None = None) -> OverlayResult:
    """Write canonical .claude.json + config.yaml to ~/.scion/harness-configs/claude/.

    Idempotent. Toggles chattr -i / +i around writes via get_protected_paths()
    (LAZY — resolved at this call, not at module import). Uses atomic
    temp+replace for crash safety.

    Raises FileNotFoundError if seed_dir does not exist.
    """
    from mnemosyne_cli.lib.scion_paths import (
        harness_config_claude_json,
        harness_config_yaml,
    )

    if seed_dir is None:
        seed_dir = _default_seed_dir()
    if not seed_dir.is_dir():
        raise FileNotFoundError(f"Seed dir does not exist: {seed_dir}")

    result = OverlayResult()
    # Resolve target paths at call time too (so monkeypatched home is honoured).
    pairs = [
        (seed_dir / ".claude.json", harness_config_claude_json()),
        (seed_dir / "config.yaml", harness_config_yaml()),
    ]

    with writable(get_protected_paths()):
        for seed_path, target_path in pairs:
            if not seed_path.is_file():
                result.skipped.append(target_path)
                continue
            seed_bytes = seed_path.read_bytes()
            if target_path.exists() and target_path.read_bytes() == seed_bytes:
                result.unchanged.append(target_path)
                continue
            _atomic_write(target_path, seed_bytes)
            result.written.append(target_path)

    return result


def _build_grove_settings_target() -> dict:
    return {
        "default_template": EXPECTED_GROVE_TEMPLATE,
        "default_harness_config": EXPECTED_GROVE_HARNESS,
    }


def _strip_profile_vault_overrides(profiles: dict) -> tuple[dict, bool]:
    """Return (new_profiles, changed) with profiles.*.env.MNEMOSYNE_VAULT stripped."""
    new_profiles = dict(profiles)
    changed = False
    for pname, pval in list(profiles.items()):
        pval_t = dict(pval or {})
        env = dict(pval_t.get("env") or {})
        if "MNEMOSYNE_VAULT" in env:
            env.pop("MNEMOSYNE_VAULT")
            pval_t["env"] = env
            new_profiles[pname] = pval_t
            changed = True
    return new_profiles, changed


def compute_canonical_changes() -> list[CanonicalChange]:
    """Enumerate what apply_empiria_defaults() WOULD write (no side effects).

    Used by `apply-empiria-defaults --dry-run`. One entry per drifted path:
    user settings.yaml (auth type and/or profile env override), every
    non-test grove settings.yaml.
    """
    import yaml

    from mnemosyne_cli.lib.scion_paths import (
        iter_grove_settings_paths,
        user_settings_path,
    )

    changes: list[CanonicalChange] = []

    # (a)+(c) User settings.yaml — field-level merge (other harness types coexist).
    user_path = user_settings_path()
    if user_path.exists():
        try:
            user_data = yaml_safe_load_or_none(user_path) or {}
        except yaml.YAMLError:
            user_data = {}

        current_auth = (
            (user_data.get("harness_configs") or {})
            .get("claude", {})
            .get("auth_selected_type")
        )
        auth_drift = current_auth != EXPECTED_AUTH_SELECTED_TYPE

        new_profiles, profile_drift = _strip_profile_vault_overrides(
            dict(user_data.get("profiles") or {})
        )

        if auth_drift or profile_drift:
            target_data = dict(user_data)
            if auth_drift:
                hc = dict(target_data.get("harness_configs") or {})
                claude_hc = dict(hc.get("claude") or {})
                claude_hc["auth_selected_type"] = EXPECTED_AUTH_SELECTED_TYPE
                hc["claude"] = claude_hc
                target_data["harness_configs"] = hc
            if profile_drift:
                target_data["profiles"] = new_profiles
            changes.append(
                CanonicalChange(
                    path=user_path, current=user_data, target=target_data
                )
            )

    # (b) Per-grove settings.yaml — whole-file overwrite (D-32).
    grove_target = _build_grove_settings_target()
    for grove_path in iter_grove_settings_paths():
        try:
            grove_data = yaml_safe_load_or_none(grove_path) or {}
        except yaml.YAMLError:
            grove_data = {}
        if (
            grove_data.get("default_template") != EXPECTED_GROVE_TEMPLATE
            or grove_data.get("default_harness_config") != EXPECTED_GROVE_HARNESS
        ):
            changes.append(
                CanonicalChange(
                    path=grove_path, current=grove_data, target=grove_target
                )
            )

    return changes


def apply_empiria_defaults(dry_run: bool = False) -> OverlayResult:
    """Write canonical Empiria settings to user, grove, and harness-config surfaces.

    Pre-flight: if ~/.scion/settings.yaml does not exist, raises FileNotFoundError
    (caller maps to typer.Exit). RESEARCH Pitfall 5.

    Field-level merge for user settings.yaml (multiple harness types may coexist).
    Whole-file overwrite for grove settings.yaml (single-purpose, Empiria-owned).
    Idempotent — re-run on already-canonical state returns an empty (falsy) result.
    """
    import yaml

    from mnemosyne_cli.lib.scion_paths import user_settings_path

    if not user_settings_path().exists():
        raise FileNotFoundError(
            "Run `scion init` first; then re-run `mnemosyne broker apply-empiria-defaults`."
        )

    result = OverlayResult()
    changes = compute_canonical_changes()

    for change in changes:
        if not dry_run:
            yaml_text = yaml.safe_dump(
                change.target, default_flow_style=False, sort_keys=False
            )
            _atomic_write(change.path, yaml_text)
        result.written.append(change.path)

    # Always run the harness-config overlay (idempotent — reuses writable() lock).
    try:
        overlay_result = apply_harness_config_overlay()
        if not dry_run:
            result.written.extend(overlay_result.written)
        else:
            # In dry-run, surface harness-config drift as "would write" too.
            result.written.extend(overlay_result.written)
        result.unchanged.extend(overlay_result.unchanged)
        result.skipped.extend(overlay_result.skipped)
    except FileNotFoundError:
        # Vault seed dir not found — warn but don't fail the convergence.
        pass

    return result


# ---------------------------------------------------------------------------
# SBR-3.3: Broker control-channel health (Phase 33.3)
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 — keep SBR-3.3 block self-contained
import socket  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

BROKER_HEARTBEAT_STALE_THRESHOLD = timedelta(seconds=120)


def _resolve_broker_name() -> str:
    """Resolve this host's broker name as registered with the hub.

    Per RESEARCH Assumption A7: SCION returns brokers with `name` field set
    to hostname for this host. If operators registered with a custom name,
    socket.gethostname() may not match — surface this as "broker not
    registered" rather than masquerade as healthy.
    """
    return socket.gethostname()


def check_control_channel() -> "CheckResult":
    """Query `scion hub brokers --json` and assess this broker's health.

    Returns CheckResult(ok=True) when healthy, ok=False when stale/disconnected
    /not-registered/hub-unreachable.

    Returns ok=True with message "scion CLI not present" when scion is not
    installed (greenfield host — let install proceed). Per RESEARCH SBR-3.3
    refinement: this uses `scion hub brokers --json` (NOT log-tail grep) so the
    same helper backs both the doctor check (tier-1) and the Path-unit watchdog
    (tier-2), avoiding RESEARCH Pitfall 1 (restart-loop on already-healed
    broken-pipe log events).
    """
    from mnemosyne_cli.lib.symlinks import CheckResult  # avoid cycle

    try:
        proc = subprocess.run(
            ["scion", "hub", "brokers", "--json"],
            check=True,
            capture_output=True,
            timeout=10,
            text=True,
        )
    except FileNotFoundError:
        return CheckResult(
            ok=True, message="scion CLI not present — broker check skipped"
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            ok=False, message="Hub unreachable (scion hub brokers timed out)"
        )
    except subprocess.CalledProcessError as e:
        return CheckResult(ok=False, message=f"Could not query hub: {e}")

    try:
        brokers = _json.loads(proc.stdout)
    except _json.JSONDecodeError as e:
        return CheckResult(ok=False, message=f"Hub returned unparseable JSON: {e}")

    this_host = _resolve_broker_name()
    me = next(
        (b for b in brokers if b.get("name") == this_host), None
    ) if isinstance(brokers, list) else None
    if me is None:
        return CheckResult(
            ok=False,
            message=f"This host ({this_host}) not registered as a broker on the hub",
        )

    state = me.get("connectionState")
    status = me.get("status")
    last_hb_iso = me.get("lastHeartbeat")
    if not last_hb_iso:
        return CheckResult(
            ok=False, message=f"Broker {this_host}: no lastHeartbeat field"
        )
    try:
        last_hb = datetime.fromisoformat(str(last_hb_iso).replace("Z", "+00:00"))
    except ValueError:
        return CheckResult(
            ok=False,
            message=f"Broker {this_host}: malformed lastHeartbeat={last_hb_iso}",
        )

    age = datetime.now(timezone.utc) - last_hb
    if (
        state != "connected"
        or status != "online"
        or age > BROKER_HEARTBEAT_STALE_THRESHOLD
    ):
        return CheckResult(
            ok=False,
            message=(
                f"Broker {this_host}: state={state}, status={status}, "
                f"lastHeartbeat={int(age.total_seconds())}s ago"
            ),
            fix_cmd="systemctl --user restart scion-broker",
        )
    return CheckResult(
        ok=True,
        message=f"Broker {this_host} healthy (lastHeartbeat {int(age.total_seconds())}s ago)",
    )


# ---------------------------------------------------------------------------
# Phase 33.3 SBR-3.3 tier-2: Path-unit watchdog renderers
# ---------------------------------------------------------------------------

PATH_UNIT_NAME = "scion-broker-control-channel-watchdog.path"
RESTART_SERVICE_NAME = "scion-broker-restart-on-broken-pipe.service"


def render_path_unit() -> str:
    """SBR-3.3 D-16 watchdog .path unit — triggers on broker.log writes."""
    return """[Unit]
Description=Watch SCION broker log for control-channel breaks

[Path]
PathModified=%h/.scion/broker.log
Unit=scion-broker-restart-on-broken-pipe.service

[Install]
WantedBy=default.target
"""


def render_restart_service(mnemosyne_bin: Path) -> str:
    """SBR-3.3 D-16 triggered .service unit. ExecStart calls the D-38 verb.

    ExecStart calls `mnemosyne broker check-control-channel --restart-if-stale`
    which queries `scion hub brokers --json` and restarts the broker only if
    actually stale (avoids RESEARCH Pitfall 1 — restart loop on already-healed
    broken-pipe log entries). Per the D-38 exit-code matrix, successful
    recovery exits 0 so the StartLimitBurst slot is preserved for genuine
    restart failures.
    """
    return f"""[Unit]
Description=Restart scion-broker when control-channel goes stale

[Service]
Type=oneshot
ExecStart={mnemosyne_bin} broker check-control-channel --restart-if-stale
# Rate limit: at most 5 failed triggers in 10 minutes (RESEARCH Pitfall 1).
# Successful recoveries exit 0 and do NOT count toward the burst budget
# (per CONTEXT D-38 + Task 04.1 exit-code matrix).
StartLimitIntervalSec=600
StartLimitBurst=5

[Install]
WantedBy=default.target
"""


def _find_mnemosyne_bin() -> Path:
    """Locate the mnemosyne binary for systemd unit ExecStart (Assumption A8)."""
    found = shutil.which("mnemosyne")
    if found:
        return Path(found)
    for candidate in (
        Path.home() / ".local/bin/mnemosyne",
        Path("/opt/empiria/.venv/bin/mnemosyne"),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not find `mnemosyne` binary for Path-unit ExecStart"
    )


def install_path_unit_watchdog() -> dict[str, Path]:
    """SBR-3.3 D-17: emit Path-unit + triggered .service, daemon-reload, enable.

    Returns dict mapping unit-name -> Path written.
    Idempotent — re-running install rewrites the same files (Empiria values).
    """
    mnemosyne_bin = _find_mnemosyne_bin()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    path_unit_path = unit_dir / PATH_UNIT_NAME
    restart_service_path = unit_dir / RESTART_SERVICE_NAME

    path_unit_path.write_text(render_path_unit())
    restart_service_path.write_text(render_restart_service(mnemosyne_bin))

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", PATH_UNIT_NAME], check=False
    )

    return {
        PATH_UNIT_NAME: path_unit_path,
        RESTART_SERVICE_NAME: restart_service_path,
    }
