"""Component path resolution for multi-repo projects (Phase 32).

Mnemosyne is the canonical multi-repo project — vault + mnemosyne-cli + scion
fork + ops. The vault project note declares each component in its `components:`
frontmatter; the per-machine `~/.config/mnemosyne/config.toml` declares where
each component is checked out on this machine via `[components.<name>]` tables.

This module owns the parsing of those tables and the resolver that other CLI
code (the SCION template generator, the doctor pre-flight check, future
multi-repo-aware commands) calls to look up a component's host path.

Schema (mirrors the strawman in 32-CONTEXT.md `<specifics>`):

    [components.mnemosyne-cli]
    local_path = "~/projects/empiria/mnemosyne-cli"

    [components.scion]
    local_path = "~/projects/empiria/scion"

    [components.ops]
    local_path = "~/projects/personal/ops"

Component names are slugs that match `components[*].name` in the project note.
The vault itself is NOT typically declared here — its path is resolved through
the existing `vault_path` / `[vaults.*]` mechanism in vault.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mnemosyne_cli.lib import vault


@dataclass
class ComponentConfig:
    """Per-machine configuration for a single project component."""

    name: str
    local_path: Path  # absolute, ~-expanded; not necessarily existing on disk

    def exists_on_disk(self) -> bool:
        """Return True if local_path is a directory that exists."""
        return self.local_path.is_dir()


class ComponentNotConfigured(Exception):
    """Raised when a component name is not present in [components.*] config."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def remediation(self) -> str:
        return (
            f"Component '{self.name}' is not configured in "
            f"~/.config/mnemosyne/config.toml. Add:\n\n"
            f"    [components.{self.name}]\n"
            f'    local_path = "~/projects/<org>/{self.name}"\n'
        )


class ComponentNotCloned(Exception):
    """Raised when a component's configured local_path does not exist on disk."""

    def __init__(self, name: str, path: Path) -> None:
        super().__init__(name, path)
        self.name = name
        self.path = path

    def remediation(self) -> str:
        return (
            f"Component '{self.name}' is configured at {self.path} "
            f"but that path does not exist. Clone it:\n\n"
            f"    git clone <repo-url> {self.path}\n\n"
            f"(See projects/empiria/mnemosyne/mnemosyne.md `components:` "
            f"for the canonical repo URL.)"
        )


def read_components_config() -> dict[str, ComponentConfig]:
    """Read every [components.<name>] table from config.toml.

    Returns a dict keyed by component name. Returns {} if no [components]
    section exists. Does NOT validate that local_path exists on disk —
    callers handle that via ComponentConfig.exists_on_disk() or
    resolve_component_path().
    """
    data = vault._read_config()
    section = data.get("components", {})
    result: dict[str, ComponentConfig] = {}
    for name, entry in section.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("local_path")
        if not raw:
            continue
        result[name] = ComponentConfig(
            name=name,
            local_path=Path(raw).expanduser().resolve(strict=False),
        )
    return result


def resolve_component_path(name: str) -> Path:
    """Resolve a single component's local host path.

    Raises:
        ComponentNotConfigured: name is missing from [components.*]
        ComponentNotCloned:     configured local_path is not a directory

    Returns the absolute, ~-expanded Path on success.
    """
    components = read_components_config()
    cfg = components.get(name)
    if cfg is None:
        raise ComponentNotConfigured(name)
    if not cfg.exists_on_disk():
        raise ComponentNotCloned(name, cfg.local_path)
    return cfg.local_path


def write_component_to_config(component: ComponentConfig) -> None:
    """Add or update a single [components.<name>] entry in config.toml.

    Preserves all other config sections. Stores local_path verbatim
    (do not pre-expand ~ when writing — the user may have provided a
    ~-prefixed path they want to keep portable).
    """
    data = vault._read_config()
    if "components" not in data:
        data["components"] = {}
    home = str(Path.home())
    raw = str(component.local_path)
    if raw.startswith(home):
        raw = "~" + raw[len(home):]
    data["components"][component.name] = {"local_path": raw}
    vault._write_config(data)
