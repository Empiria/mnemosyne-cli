# Container Images

Container images for running autonomous Claude Code agents, built on the SCION capability-image stack.

## Image Hierarchy

```
scion-claude (Google — SCION base)
  └── empiria-claude (Empiria base: qmd, ctx7, uv, gh, ripgrep, mnemosyne-cli)
        └── empiria-claude-anvil (Anvil: Playwright, pytest-playwright)
```

## Getting Images

Pre-built images are published to `ghcr.io/empiria/` on every push to `containers/` on main.

```bash
podman pull ghcr.io/empiria/empiria-claude:latest
podman pull ghcr.io/empiria/empiria-claude-anvil:latest
```

## Building Locally

For contributors modifying Containerfiles:

```bash
# empiria-claude (from scion-claude base)
podman build -t empiria-claude:latest containers/empiria-claude/

# empiria-claude-anvil (from empiria-claude)
podman build -t empiria-claude-anvil:latest \
  --build-arg BASE_IMAGE=localhost/empiria-claude:latest \
  containers/empiria-claude-anvil/
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

## Authentication

### Claude Code

Auth is handled automatically by SCION's Claude harness — no manual credential management.

### GitHub CLI

The `gh` CLI authenticates via the `GH_TOKEN` environment variable, configured as a secret in `scion-agent.yaml` — SCION prompts for it if not set.

### SSH agent forwarding

The SCION template configures host SSH agent forwarding automatically via `scion-agent.yaml`, so git auth works without storing keys in the container.

## CI Pipeline

The GitHub Actions workflow (`.github/workflows/publish-images.yml`) builds the SCION capability images on push to `containers/` on main:

| Job | Image | Depends on |
|-----|-------|-----------|
| `empiria-claude` | `empiria-claude` | — |
| `empiria-claude-anvil` | `empiria-claude-anvil` | `empiria-claude` |

Manual dispatch is also available via `workflow_dispatch`.
