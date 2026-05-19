"""Path conventions for SCION configuration files under ~/.scion/.

Phase 33.3 SBR-3.7 introduces the need to enumerate per-grove settings.yaml
files. The default skip_prefixes list filters out SCION's own auto-generated
test groves (verified live: ~100+ exist on a typical broker host).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

# Verified live on operator host 2026-05-19 — these prefixes match SCION-
# internal test grove names and should NOT be touched by apply-empiria-defaults.
DEFAULT_SKIP_PREFIXES: tuple[str, ...] = (
    "auto-", "test-", "cleanup-", "fs-delete-test-", "hub-native-grove-",
    "localpath-auto-", "sd-git-", "recreatable-", "e2e-",
)


def grove_configs_root() -> Path:
    return Path.home() / ".scion" / "grove-configs"


def user_settings_path() -> Path:
    return Path.home() / ".scion" / "settings.yaml"


def harness_config_dir() -> Path:
    return Path.home() / ".scion" / "harness-configs" / "claude"


def harness_config_claude_json() -> Path:
    return harness_config_dir() / "home" / ".claude.json"


def harness_config_yaml() -> Path:
    return harness_config_dir() / "config.yaml"


def iter_grove_settings_paths(
    skip_prefixes: Iterable[str] = DEFAULT_SKIP_PREFIXES,
) -> Iterator[Path]:
    """Yield each grove's .scion/settings.yaml that exists and is not a test grove.

    Per RESEARCH Assumption A6 / Open Question 2 — operators have ~100+ auto-
    generated SCION test groves under ~/.scion/grove-configs/. Pass an empty
    tuple to iterate ALL groves including test ones (used by tests).
    """
    base = grove_configs_root()
    if not base.is_dir():
        return
    skips = tuple(skip_prefixes)
    for grove_dir in sorted(base.iterdir()):
        if not grove_dir.is_dir():
            continue
        name = grove_dir.name
        if any(name.startswith(p) for p in skips):
            continue
        candidate = grove_dir / ".scion" / "settings.yaml"
        if candidate.is_file():
            yield candidate
