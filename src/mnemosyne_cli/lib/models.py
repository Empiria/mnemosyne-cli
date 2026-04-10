"""Model profile resolution for mnemosyne subagents.

Each subagent type maps to a model (opus/sonnet/haiku) based on the active
profile.  Profiles are stored per-project in .planning/config.json under
``model_profile``.  Per-agent overrides live under ``model_overrides``.
A machine-wide default profile can be set in ~/.config/mnemosyne/config.toml.

Resolution priority:
1. model_overrides[agent_type] in .planning/config.json
2. model_profile in .planning/config.json
3. model_profile in ~/.config/mnemosyne/config.toml
4. "balanced" (hardcoded fallback)
"""

from __future__ import annotations

import json
from pathlib import Path

from mnemosyne_cli.lib import vault

# ---------------------------------------------------------------------------
# Profile table
# ---------------------------------------------------------------------------
# Each agent type maps to a model alias per profile.  Rationale comments
# explain *why* a particular tier was chosen.

MODEL_PROFILES: dict[str, dict[str, str]] = {
    # Planning needs strong reasoning
    "mnemosyne-planner": {
        "quality": "opus",
        "balanced": "opus",
        "budget": "sonnet",
    },
    # Research benefits from breadth and depth
    "mnemosyne-researcher": {
        "quality": "opus",
        "balanced": "sonnet",
        "budget": "haiku",
    },
    # Execution is mostly code generation — sonnet is strong enough
    "mnemosyne-executor": {
        "quality": "opus",
        "balanced": "sonnet",
        "budget": "sonnet",
    },
    # Verification needs careful checking but not creativity
    "mnemosyne-verifier": {
        "quality": "sonnet",
        "balanced": "sonnet",
        "budget": "haiku",
    },
    # Codebase mapping is read-only structured extraction
    "mnemosyne-codebase-mapper": {
        "quality": "sonnet",
        "balanced": "haiku",
        "budget": "haiku",
    },
}

VALID_PROFILES = ("quality", "balanced", "budget", "inherit")

VALID_MODELS = ("opus", "sonnet", "haiku")

DEFAULT_PROFILE = "balanced"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _read_planning_config(planning_dir: Path) -> dict:
    """Read .planning/config.json.  Returns empty dict if missing."""
    config_path = planning_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_planning_config(planning_dir: Path, data: dict) -> None:
    """Write .planning/config.json, preserving existing keys."""
    config_path = planning_dir / "config.json"
    config_path.write_text(json.dumps(data, indent=2) + "\n")


def get_global_profile() -> str | None:
    """Return the machine-wide model profile from config.toml, or None."""
    cfg = vault._read_config()
    return cfg.get("model_profile")


def set_global_profile(profile: str) -> None:
    """Set the machine-wide default model profile in config.toml."""
    cfg = vault._read_config()
    cfg["model_profile"] = profile
    vault._write_config(cfg)


def get_profile(planning_dir: Path) -> str:
    """Return the active model profile name.

    Resolution: project config → global config → "balanced".
    """
    cfg = _read_planning_config(planning_dir)
    project_profile = cfg.get("model_profile")
    if project_profile:
        return project_profile
    return get_global_profile() or DEFAULT_PROFILE


def set_profile(planning_dir: Path, profile: str) -> None:
    """Set the active model profile in config.json."""
    cfg = _read_planning_config(planning_dir)
    cfg["model_profile"] = profile
    _write_planning_config(planning_dir, cfg)


def get_overrides(planning_dir: Path) -> dict[str, str]:
    """Return per-agent model overrides from config.json."""
    cfg = _read_planning_config(planning_dir)
    return cfg.get("model_overrides", {})


def set_override(planning_dir: Path, agent_type: str, model: str) -> None:
    """Set a per-agent model override in config.json."""
    cfg = _read_planning_config(planning_dir)
    overrides = cfg.get("model_overrides", {})
    overrides[agent_type] = model
    cfg["model_overrides"] = overrides
    _write_planning_config(planning_dir, cfg)


def clear_override(planning_dir: Path, agent_type: str) -> None:
    """Remove a per-agent model override from config.json."""
    cfg = _read_planning_config(planning_dir)
    overrides = cfg.get("model_overrides", {})
    overrides.pop(agent_type, None)
    if overrides:
        cfg["model_overrides"] = overrides
    else:
        cfg.pop("model_overrides", None)
    _write_planning_config(planning_dir, cfg)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_model(agent_type: str, planning_dir: Path) -> str:
    """Resolve the model for an agent type given project config.

    Priority:
    1. model_overrides[agent_type] in .planning/config.json
    2. model_profile in .planning/config.json
    3. model_profile in ~/.config/mnemosyne/config.toml
    4. "balanced" profile (hardcoded fallback)
    """
    cfg = _read_planning_config(planning_dir)

    # 1. Per-agent override
    override = cfg.get("model_overrides", {}).get(agent_type)
    if override:
        return override

    # 2-3. Profile lookup (project → global → default)
    profile = get_profile(planning_dir)
    if profile == "inherit":
        return "inherit"

    agent_models = MODEL_PROFILES.get(agent_type)
    if not agent_models:
        return "sonnet"

    return agent_models.get(profile, agent_models.get(DEFAULT_PROFILE, "sonnet"))


def resolve_all(planning_dir: Path) -> dict[str, str]:
    """Resolve models for all known agent types.  Returns {agent: model}."""
    return {agent: resolve_model(agent, planning_dir) for agent in MODEL_PROFILES}
