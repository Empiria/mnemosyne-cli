"""Read/write .envrc with idempotency for MNEMOSYNE_VAULT."""

from __future__ import annotations

from pathlib import Path

from mnemosyne_cli.lib.symlinks import CheckResult


def set_envrc_vault(client_path: Path, vault_path: Path) -> bool:
    """Write or update the MNEMOSYNE_VAULT export in .envrc.

    Behaviour:
    - If the line already exists with the correct value: no-op, return False.
    - If the line exists with a different value: update in place, return True.
    - If the line is absent but the file exists: append, return True.
    - If the file does not exist: create it, return True.
    """
    envrc = client_path / ".envrc"
    export_line = f'export MNEMOSYNE_VAULT="{vault_path}"'

    if envrc.exists():
        lines = envrc.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith("export MNEMOSYNE_VAULT="):
                if line == export_line:
                    return False  # already correct
                lines[i] = export_line
                envrc.write_text("\n".join(lines) + "\n")
                return True
        # Not found — append
        with open(envrc, "a") as f:
            f.write(f"\n{export_line}\n")
        return True
    else:
        envrc.write_text(f"{export_line}\n")
        return True


def check_envrc_vault(client_path: Path, vault_path: Path) -> CheckResult:
    """Return a CheckResult for the MNEMOSYNE_VAULT line in .envrc."""
    envrc = client_path / ".envrc"
    export_line = f'export MNEMOSYNE_VAULT="{vault_path}"'

    if not envrc.exists():
        return CheckResult(
            ok=False,
            message=".envrc does not exist",
            fix_cmd=f'echo \'{export_line}\' > .envrc && direnv allow',
        )

    lines = envrc.read_text().splitlines()
    for line in lines:
        if line.startswith("export MNEMOSYNE_VAULT="):
            if line == export_line:
                return CheckResult(ok=True, message=f".envrc has correct MNEMOSYNE_VAULT")
            else:
                actual_value = line.split("=", 1)[1]
                return CheckResult(
                    ok=False,
                    message=f".envrc has MNEMOSYNE_VAULT={actual_value}, expected {vault_path}",
                    fix_cmd=None,  # set_envrc_vault handles the update
                )

    return CheckResult(
        ok=False,
        message=".envrc exists but MNEMOSYNE_VAULT is not set",
        fix_cmd=f'echo \'{export_line}\' >> .envrc && direnv allow',
    )
