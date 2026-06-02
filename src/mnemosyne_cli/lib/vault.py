"""Vault path resolution and project discovery."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import tomli_w
import typer
from rich.console import Console

console = Console()
error_console = Console(stderr=True, style="bold red")

_CONFIG_PATH = Path("~/.config/mnemosyne/config.toml").expanduser()


@dataclass(frozen=True)
class OperationalHome:
    """Parsed operational_home frontmatter field from an empiria engagement record.

    vault: name of the registered vault that holds the operational workspace.
    path: vault-relative path to the project directory (e.g. 'projects/infinite-worlds').
    """

    vault: str
    path: str


@dataclass
class VaultConfig:
    """Configuration for a registered vault."""

    name: str
    path: Path
    description: str = ""
    sync: str = "git"  # "git" | "nextcloud" | "obsidian-sync"


def _read_config() -> dict:
    """Read the full config.toml as a dict. Returns empty dict if missing."""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    """Write data dict to config.toml, creating parent dirs as needed."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_bytes(tomli_w.dumps(data).encode())


def read_vaults_config() -> list[VaultConfig]:
    """Read all registered vaults from [vaults.*] tables in config.toml.

    Returns an empty list if no [vaults] section exists.
    """
    data = _read_config()
    vaults_section = data.get("vaults", {})
    result = []
    for name, entry in vaults_section.items():
        result.append(
            VaultConfig(
                name=name,
                path=Path(entry.get("path", "")).expanduser(),
                description=entry.get("description", ""),
                sync=entry.get("sync", "git"),
            )
        )
    return result


def write_vault_to_config(vault: VaultConfig) -> None:
    """Add or update a single vault entry in config.toml.

    Preserves all other config sections (vault_path, vault_rules, etc.).
    """
    data = _read_config()
    if "vaults" not in data:
        data["vaults"] = {}
    data["vaults"][vault.name] = {
        "path": str(vault.path),
        "description": vault.description,
        "sync": vault.sync,
    }
    _write_config(data)


def remove_vault_from_config(name: str) -> None:
    """Remove a single vault entry from config.toml by name.

    Also removes any [[vault_rules]] entries referencing this vault.
    Preserves everything else.
    """
    data = _read_config()
    vaults = data.get("vaults", {})
    vaults.pop(name, None)
    if vaults:
        data["vaults"] = vaults
    elif "vaults" in data:
        del data["vaults"]

    # Remove rules referencing this vault
    rules = data.get("vault_rules", [])
    if rules:
        updated_rules = []
        for rule in rules:
            if rule.get("from") == name:
                continue  # drop entire rule for the removed vault
            # Remove this vault from can_read lists
            can_read = [v for v in rule.get("can_read", []) if v != name]
            if can_read:
                updated_rules.append({**rule, "can_read": can_read})
            # If can_read is now empty, drop the rule entirely
        if updated_rules:
            data["vault_rules"] = updated_rules
        elif "vault_rules" in data:
            del data["vault_rules"]

    _write_config(data)


def get_vault_rules() -> dict[str, list[str]]:
    """Read [[vault_rules]] array of tables from config.toml.

    Returns {vault_name: [can_read_names]}. Empty dict if no rules defined.
    """
    data = _read_config()
    rules = data.get("vault_rules", [])
    result: dict[str, list[str]] = {}
    for rule in rules:
        from_vault = rule.get("from", "")
        can_read = rule.get("can_read", [])
        if from_vault:
            result[from_vault] = can_read
    return result


def resolve_vaults() -> list[VaultConfig]:
    """Return all registered vaults.

    If [vaults.*] tables exist in config.toml, reads from there.
    If not, falls back to creating a single-entry list from resolve_vault_path()
    result (backward compatibility with single-vault setups).
    """
    vaults = read_vaults_config()
    if vaults:
        return vaults
    # Backward compatibility: wrap the single-vault path in a VaultConfig
    try:
        vault_path = resolve_vault_path()
        return [VaultConfig(name="default", path=vault_path)]
    except SystemExit:
        return []


def resolve_primary_vault() -> VaultConfig:
    """Return the primary vault as a VaultConfig.

    Returns the vault matching MNEMOSYNE_VAULT env var or config.toml vault_path,
    or the first vault in the registry.
    """
    vault_path = resolve_vault_path()
    vaults = read_vaults_config()
    # Check if any registered vault matches the resolved path
    for v in vaults:
        if v.path.expanduser().resolve() == vault_path:
            return v
    # Fall back to wrapping the resolved path
    return VaultConfig(name="default", path=vault_path)


def can_read(from_vault: str, target_vault: str) -> bool:
    """Check whether from_vault is allowed to read target_vault.

    Consults [[vault_rules]] in config.toml. Returns False if no rule grants
    access (closed by default).
    """
    rules = get_vault_rules()
    allowed = rules.get(from_vault, [])
    return target_vault in allowed


def _read_config_vault_path() -> Path | None:
    """Read vault_path from ~/.config/mnemosyne/config.toml, if present."""
    if not _CONFIG_PATH.exists():
        return None
    try:
        with open(_CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        raw = data.get("vault_path")
        if raw:
            return Path(raw).expanduser().resolve()
    except Exception:
        pass
    return None


def save_vault_path(vault_path: Path) -> None:
    """Persist vault_path to ~/.config/mnemosyne/config.toml."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _CONFIG_PATH.exists():
        content = _CONFIG_PATH.read_text()
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("vault_path"):
                lines[i] = f'vault_path = "{vault_path}"'
                _CONFIG_PATH.write_text("\n".join(lines) + "\n")
                return
        # Key not found — append
        with open(_CONFIG_PATH, "a") as f:
            f.write(f'vault_path = "{vault_path}"\n')
    else:
        _CONFIG_PATH.write_text(f'vault_path = "{vault_path}"\n')


def resolve_vault_path() -> Path:
    """Resolve the Mnemosyne vault path.

    Resolution order:
    1. MNEMOSYNE_VAULT environment variable (container/explicit override)
    2. ~/.config/mnemosyne/config.toml vault_path

    Returns a Path to the vault root, or raises typer.Exit on error.
    """
    # 1. Environment variable (highest priority — used by containers and .envrc)
    vault_env = os.environ.get("MNEMOSYNE_VAULT")
    if vault_env:
        vault = Path(vault_env).expanduser().resolve()
        if not vault.is_dir():
            error_console.print(
                f"MNEMOSYNE_VAULT is set but does not exist: {vault}"
            )
            raise typer.Exit(1)
        return vault

    # 2. Config file
    config_path = _read_config_vault_path()
    if config_path:
        if not config_path.is_dir():
            error_console.print(
                f"vault_path in {_CONFIG_PATH} does not exist: {config_path}"
            )
            raise typer.Exit(1)
        return config_path

    error_console.print(
        "Cannot locate Mnemosyne vault.\n"
        f"Set vault_path in {_CONFIG_PATH} or set the MNEMOSYNE_VAULT environment variable."
    )
    raise typer.Exit(1)


def resolve_vault_project(client_path: Path, vault_path: Path) -> str | None:
    """Derive vault-relative project path from the .planning symlink.

    Given the client codebase root and the vault root, returns something like
    'projects/friendly-fox/infinite-worlds', or None if the symlink is absent
    or does not point inside the vault.
    """
    planning = client_path / ".planning"
    if not planning.is_symlink():
        return None
    target = planning.resolve()
    # target is e.g. /path/to/vault/projects/friendly-fox/infinite-worlds/gsd-planning
    project_dir = target.parent  # strip /gsd-planning
    try:
        return str(project_dir.relative_to(vault_path))
    except ValueError:
        return None


def project_exists(vault_path: Path, project_rel: str) -> bool:
    """Check if the vault project directory exists."""
    return (vault_path / project_rel).is_dir()


def read_operational_home(vault_path: Path, project_rel: str) -> OperationalHome | None:
    """Read the operational_home field from the empiria-side engagement record.

    project_rel is e.g. 'projects/friendly-fox/infinite-worlds'.
    The record note is {dir}/{slug}.md per the component.py convention
    (slug = directory basename — Pitfall 3).

    Returns:
        OperationalHome if the field is present and well-formed.
        None if the note does not exist or the field is absent.

    Raises:
        ValueError if the field is present but malformed (Open Q2 lock:
        a present-but-malformed operational_home is a config bug — FAIL loudly,
        never fall through silently to the empiria-resident path).
    """
    project_dir = vault_path / project_rel
    note_path = project_dir / f"{project_dir.name}.md"
    if not note_path.is_file():
        return None
    oh = frontmatter.load(str(note_path)).metadata.get("operational_home")
    if oh is None:
        return None
    if not isinstance(oh, dict) or "vault" not in oh or "path" not in oh:
        raise ValueError(
            f"operational_home in {note_path} is malformed: expected a dict with "
            f"'vault' and 'path' keys, got {oh!r}"
        )
    return OperationalHome(vault=str(oh["vault"]), path=str(oh["path"]))


def vault_by_name(name: str) -> VaultConfig | None:
    """Return the registered VaultConfig for *name*, or None if unregistered.

    Linear scan over read_vaults_config(). Returns None when name is absent
    from [vaults.*] in config.toml.
    """
    for v in read_vaults_config():
        if v.name == name:
            return v
    return None


def validate_vault_rules() -> list[str]:
    """Cross-check [[vault_rules]] against the registered [vaults.*] set (D-F1).

    Returns a list of problem strings. Empty list means config is consistent.

    Checks:
    - Orphan 'from': a vault_rules entry whose 'from' names a vault not in [vaults.*].
    - Unregistered 'can_read' ref: a can_read target not in [vaults.*].

    Returns [] when no [vaults.*] are registered (empty registry is not an error;
    the doctor will message it as skipped).
    """
    registered = {v.name for v in read_vaults_config()}
    if not registered:
        return []
    rules = get_vault_rules()
    problems: list[str] = []
    for frm, can_read_list in rules.items():
        if frm not in registered:
            problems.append(f"orphan rule: from='{frm}' not in [vaults.*]")
        for target in can_read_list:
            if target not in registered:
                problems.append(
                    f"unregistered can_read ref: '{target}' (rule from='{frm}')"
                )
    return problems


def is_within(root: Path, candidate: Path) -> bool:
    """Return True if *candidate* resolves inside *root* (containment guard).

    Both paths are resolved before comparison so that symlinks and '..'
    components cannot bypass the check (Security V5/V12).
    """
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    return resolved_candidate == resolved_root or resolved_candidate.is_relative_to(resolved_root)
