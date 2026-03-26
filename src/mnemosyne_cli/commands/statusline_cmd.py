"""Statusline command combining mnemosyne status prefix with GSD statusline output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer


def run() -> None:
    """Output mnemosyne status prefix then pass stdin through to GSD statusline."""
    home = Path.home()
    cache_file = home / ".claude" / "cache" / "mnemosyne-status.json"

    # 1. Read cache; bootstrap if missing
    cache = None
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            cache = None

    if cache is None:
        # Synchronously run mnemosyne status --json to populate cache
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
        # Still no cache — exit cleanly
        raise typer.Exit(0)

    # 2. Refresh cache in background if stale (>60 seconds)
    checked = cache.get("checked", 0)
    if time.time() - checked > 60:
        try:
            proc = subprocess.Popen(
                ["mnemosyne", "status", "--json"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Access .pid to ensure process is detached (no need to wait)
            _ = proc.pid
        except OSError:
            pass

    # 3. Build mnemosyne status prefix
    behind = cache.get("behind", 0)
    ahead = cache.get("ahead", 0)
    if behind > 0:
        prefix = f"\033[33m! mnemosyne: {behind} behind\033[0m | "
    elif ahead > 0:
        prefix = f"\033[33m! mnemosyne: {ahead} ahead\033[0m | "
    else:
        prefix = "\033[32m+ mnemosyne\033[0m | "

    # 4. Read stdin
    stdin_data = sys.stdin.read()

    # 5. Pass through GSD statusline if present
    gsd_output = ""
    gsd_statusline = home / ".claude" / "hooks" / "gsd-statusline.js"
    if gsd_statusline.exists():
        try:
            result = subprocess.run(
                ["node", str(gsd_statusline)],
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=2,
            )
            gsd_output = result.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass

    # 6. Write combined output
    sys.stdout.write(prefix + gsd_output)
