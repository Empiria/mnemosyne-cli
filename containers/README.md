# Container Images

Container images for running autonomous Claude Code agents. Two image families exist: SCION capability images (recommended) and legacy Podman images (deprecated).

## Image Hierarchy

```
scion-claude (Google — SCION base)
  └── empiria-claude (Empiria base: qmd, ctx7, uv, gh, ripgrep, mnemosyne-cli)
        └── empiria-claude-anvil (Anvil: Playwright, pytest-playwright)

mnemosyne-base (legacy — Debian bookworm-slim, Node 22, git, hapi)
  └── mnemosyne-claude (legacy — extends base: gh, ripgrep, uv, Claude Code, qmd, ctx7)
```

## Getting Images

Pre-built images are published to `ghcr.io/empiria/` on every push to `containers/` on main.

```bash
# SCION images (recommended)
podman pull ghcr.io/empiria/empiria-claude:latest
podman pull ghcr.io/empiria/empiria-claude-anvil:latest

# Legacy images (deprecated — for mnemosyne agent start)
podman pull ghcr.io/empiria/mnemosyne-base:latest
podman pull ghcr.io/empiria/mnemosyne-claude:latest
```

Or use the CLI shortcut for legacy images:

```bash
mnemosyne refresh
```

## Building Locally

For contributors modifying Containerfiles:

### SCION capability images

```bash
# empiria-claude (from scion-claude base)
podman build -t empiria-claude:latest containers/empiria-claude/

# empiria-claude-anvil (from empiria-claude)
podman build -t empiria-claude-anvil:latest \
  --build-arg BASE_IMAGE=localhost/empiria-claude:latest \
  containers/empiria-claude-anvil/
```

### Legacy images

```bash
podman build --platform linux/amd64 -t mnemosyne-base containers/base/
podman build --platform linux/amd64 -t mnemosyne-claude containers/claude/
```

Or via the CLI:

```bash
mnemosyne refresh --build
```

## SCION Setup

To use these images with SCION:

1. **Install SCION** — follow the [SCION installation docs](https://googlecloudplatform.github.io/scion/overview/)

2. **Use the Empiria template** — the shared template at `agents/scion-template/` in the Mnemosyne vault configures vault access, qmd search, and mnemosyne CLI:

   ```bash
   scion start my-agent "implement feature X" \
     --template /path/to/mnemosyne/agents/scion-template \
     --image ghcr.io/empiria/empiria-claude:latest
   ```

   For Anvil projects:

   ```bash
   scion start my-agent "fix form validation" \
     --template /path/to/mnemosyne/agents/scion-template \
     --image ghcr.io/empiria/empiria-claude-anvil:latest
   ```

3. **Configure a SCION profile** (optional — avoids repeating flags):

   ```bash
   scion profile create empiria \
     --template empiria-agent \
     --harness-config claude \
     --runtime podman

   scion profile use empiria
   ```

See `docs/how-to/scion-migration.md` in the vault for the full migration guide.

## Running (Legacy)

The preferred legacy method is `mnemosyne agent start <project>`. These commands are deprecated — use SCION instead. See below for reference.

### Claude agent

```bash
PROJECT_DIR=/path/to/project
podman run -it --rm \
  -v $PROJECT_DIR:$PROJECT_DIR \
  --workdir $PROJECT_DIR \
  -e WORKSPACE_PATH=$PROJECT_DIR \
  -v $MNEMOSYNE_VAULT:/vault:ro \
  -v $(pwd)/containers/config:/config:ro \
  -v $SSH_AUTH_SOCK:/tmp/ssh-auth-sock \
  -e SSH_AUTH_SOCK=/tmp/ssh-auth-sock \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -e MNEMOSYNE_PROJECT="<project-slug>" \
  mnemosyne-claude
```

### hapi hub (for mobile relay)

```bash
podman run -d --rm \
  -p 3006:3006 \
  -e HAPI_MODE=relay \
  -e CLI_API_TOKEN="$CLI_API_TOKEN" \
  mnemosyne-hapi-hub
```

## Configuration

### /config mount (legacy)

Mount `containers/config/` at `/config` inside the container. The entrypoint merges:

- `/config/.claude/` -> `~/.claude/` (symlinks, non-destructive — existing files are not overwritten)
- `/config/zellij/` -> `~/.config/zellij/` (copied, for non-Claude containers that use Zellij)

### Volume mounts

| Host path | Container path | Mode | Purpose |
|-----------|---------------|------|---------|
| `/path/to/project` | `/path/to/project` | rw | Project repository (mounted at host path) |
| `$MNEMOSYNE_VAULT` | `/vault` | rw | Mnemosyne knowledge vault |
| `containers/config` | `/config` | ro | Shared agent config (legacy) |
| `$SSH_AUTH_SOCK` | `/run/ssh-agent` | rw | SSH agent for git auth |

### Project dependencies (legacy)

Project-specific tools and packages are declared in a `container.toml` file in the project's vault directory (e.g. `projects/<org>/<slug>/container.toml`). The entrypoint installs these at startup.

Supported sections: `apt`, `pip`, `npm`, `cargo`, `run`. Example:

```toml
[apt]
packages = ["libpq-dev"]

[pip]
packages = ["pytest-playwright"]

[run]
commands = ["playwright install chromium"]
```

**Note:** SCION capability images replace `container.toml` — deps are baked into the image (e.g. `empiria-claude-anvil` includes Playwright). No runtime installation needed.

## Authentication

### Claude Code

- **SCION:** Auth is handled automatically by SCION's Claude harness — no manual credential management
- **Legacy:** Pass `CLAUDE_CODE_OAUTH_TOKEN` env var or mount credentials JSON

### GitHub CLI

The `gh` CLI authenticates via the `GH_TOKEN` environment variable.

- **SCION:** Configured as a secret in `scion-agent.yaml` — SCION prompts for it if not set
- **Legacy:** Add to `~/.config/mnemosyne/agent.env`, passed automatically by `mnemosyne agent start`

### SSH agent forwarding

Pass the host SSH agent socket to avoid storing keys inside the container:

```bash
-v $SSH_AUTH_SOCK:/run/ssh-agent -e SSH_AUTH_SOCK=/run/ssh-agent
```

The SCION template configures this automatically via `scion-agent.yaml`.

## CI Pipeline

The GitHub Actions workflow (`.github/workflows/publish-images.yml`) builds all four images on push to `containers/` on main:

| Job | Image | Depends on | Platform |
|-----|-------|-----------|----------|
| `legacy-base` | `mnemosyne-base` | — | amd64, arm64 |
| `legacy-claude` | `mnemosyne-claude` | `legacy-base` | amd64, arm64 |
| `empiria-claude` | `empiria-claude` | — | amd64 |
| `empiria-claude-anvil` | `empiria-claude-anvil` | `empiria-claude` | amd64 |

Manual dispatch is also available via `workflow_dispatch`.
