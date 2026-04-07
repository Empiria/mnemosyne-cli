"""Statusline command — merged vault staleness prefix + model/task/context bar + bridge file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import typer

# Auto-compact buffer reserve (Claude Code reserves ~16.5% of the window for
# auto-compact. We normalise context display to treat 83.5% as "100% used"
# so the bar tracks usable context, not raw tokens.)
_AUTO_COMPACT_BUFFER_PCT = 16.5


def _read_stdin_json() -> dict | None:
    """Read and parse JSON from stdin. Returns None if stdin is empty or invalid."""
    try:
        data = sys.stdin.read()
        if not data or not data.strip():
            return None
        return json.loads(data)
    except (json.JSONDecodeError, OSError):
        return None


def _build_vault_prefix(cache: dict) -> str:
    """Build the vault staleness prefix string from cache data."""
    behind = cache.get("behind", 0)
    ahead = cache.get("ahead", 0)
    if behind > 0:
        return f"\033[33m\u26a0 mnemosyne: {behind} behind\033[0m \u2502 "
    elif ahead > 0:
        return f"\033[33m\u26a0 mnemosyne: {ahead} ahead\033[0m \u2502 "
    else:
        return "\033[32m\u2713 mnemosyne\033[0m \u2502 "


def _build_context_bar(remaining: float) -> tuple[str, int]:
    """Build ANSI colour context bar string and used percentage.

    Returns (bar_string, used_pct).
    """
    usable_remaining = max(
        0.0,
        ((remaining - _AUTO_COMPACT_BUFFER_PCT) / (100 - _AUTO_COMPACT_BUFFER_PCT)) * 100,
    )
    used = max(0, min(100, round(100 - usable_remaining)))

    filled = used // 10
    bar = "\u2588" * filled + "\u2591" * (10 - filled)

    if used < 50:
        ctx = f" \033[32m{bar} {used}%\033[0m"
    elif used < 65:
        ctx = f" \033[33m{bar} {used}%\033[0m"
    elif used < 80:
        ctx = f" \033[38;5;208m{bar} {used}%\033[0m"
    else:
        ctx = f" \033[5;31m\U0001f480 {bar} {used}%\033[0m"

    return ctx, used


def _write_bridge_files(
    cwd: str,
    session_id: str,
    remaining: float,
    used_pct: int,
    model: str,
) -> None:
    """Write context metrics to bridge files consumed by context-monitor and other tools."""
    # Reject session IDs with path separators or traversal sequences
    safe_session = session_id
    if session_id and ("/" in session_id or "\\" in session_id or ".." in session_id):
        safe_session = ""

    # .planning/.bridge-statusline.json — for mnemosyne workflow tools
    planning_dir = Path(cwd) / ".planning"
    if planning_dir.is_dir():
        bridge_data = {
            "session_id": safe_session,
            "remaining_percentage": remaining,
            "used_pct": used_pct,
            "model": model,
            "cwd": cwd,
            "timestamp": int(time.time()),
        }
        try:
            (planning_dir / ".bridge-statusline.json").write_text(json.dumps(bridge_data))
        except OSError:
            pass

    # /tmp/claude-ctx-{session_id}.json — read by context-monitor hook subcommand
    if safe_session:
        tmp_bridge = Path(tempfile.gettempdir()) / f"claude-ctx-{safe_session}.json"
        try:
            tmp_bridge.write_text(
                json.dumps(
                    {
                        "session_id": safe_session,
                        "remaining_percentage": remaining,
                        "used_pct": used_pct,
                        "timestamp": int(time.time()),
                    }
                )
            )
        except OSError:
            pass


def _current_task(session_id: str | None) -> str:
    """Read the in-progress todo task label for the current session."""
    if not session_id:
        return ""
    home = Path.home()
    claude_dir_env = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_dir = Path(claude_dir_env) if claude_dir_env else home / ".claude"
    todos_dir = claude_dir / "todos"
    if not todos_dir.exists():
        return ""
    try:
        files = sorted(
            [
                f
                for f in todos_dir.iterdir()
                if f.name.startswith(session_id)
                and "-agent-" in f.name
                and f.name.endswith(".json")
            ],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return ""
        todos = json.loads(files[0].read_text())
        for t in todos:
            if t.get("status") == "in_progress":
                return t.get("activeForm") or ""
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def run() -> None:
    """Merged statusline: vault staleness prefix + model/task/dir/context bar + bridge file.

    Reads Claude Code context JSON from stdin and produces a combined status line.
    Writes bridge files as a side effect for the context-monitor hook.
    """
    home = Path.home()
    cache_file = home / ".claude" / "cache" / "mnemosyne-status.json"

    # --- 1. Read vault status cache ---
    cache: dict | None = None
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            cache = None

    if cache is None:
        # Synchronously populate cache
        try:
            subprocess.run(
                ["mnemosyne", "status", "--json"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if cache_file.exists():
                cache = json.loads(cache_file.read_text())
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    if cache is None:
        raise typer.Exit(0)

    # Refresh in background if stale (>60 seconds)
    checked = cache.get("checked", 0)
    if time.time() - checked > 60:
        try:
            proc = subprocess.Popen(
                ["mnemosyne", "status", "--json"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _ = proc.pid
        except OSError:
            pass

    # --- 2. Read stdin (Claude Code context JSON) ---
    data = _read_stdin_json()

    # --- 3. Build vault staleness prefix ---
    prefix = _build_vault_prefix(cache)

    # --- 4. Build GSD-style model/task/dir/context bar ---
    bar = ""
    if data:
        model: str = ((data.get("model") or {}).get("display_name")) or "Claude"
        cwd: str = ((data.get("workspace") or {}).get("current_dir")) or os.getcwd()
        session_id: str = data.get("session_id") or ""
        remaining = (data.get("context_window") or {}).get("remaining_percentage")

        ctx_str = ""
        used_pct = 0
        if remaining is not None:
            ctx_str, used_pct = _build_context_bar(float(remaining))
            _write_bridge_files(cwd, session_id, float(remaining), used_pct, model)

        task = _current_task(session_id)
        dirname = Path(cwd).name

        if task:
            bar = (
                f"\033[2m{model}\033[0m \u2502 \033[1m{task}\033[0m \u2502 "
                f"\033[2m{dirname}\033[0m{ctx_str}"
            )
        else:
            bar = f"\033[2m{model}\033[0m \u2502 \033[2m{dirname}\033[0m{ctx_str}"

    # --- 5. Write combined output ---
    sys.stdout.write(prefix + bar)
