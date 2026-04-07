# Mnemosyne Agent Containers

Podman container images for running autonomous Claude Code agents with `--dangerously-skip-permissions`. The container IS the safety boundary.

## Images

| Image | Containerfile | Purpose |
|-------|---------------|---------|
| `mnemosyne-base` | `base/Containerfile` | Debian bookworm-slim with Node 22, git, Starship, hapi |
| `mnemosyne-claude` | `claude/Containerfile` | Extends base: adds gh, ripgrep, uv, Claude Code, qmd, ctx7 |
| `mnemosyne-hapi-hub` | `hapi-hub/Containerfile` | Standalone hapi relay hub for mobile access |

## Getting Images

Pre-built images are published to `ghcr.io/empiria` on every push to main. Pull them with:

```sh
mnemosyne refresh
```

This pulls both `mnemosyne-base` and `mnemosyne-claude` from the registry and tags them as `localhost/` for local use.

### Building locally

For contributors modifying Containerfiles:

```sh
mnemosyne refresh --build
```

Or manually, in order (base first, then dependent images):

```sh
podman build --platform linux/amd64 -t mnemosyne-base containers/base/
podman build --platform linux/amd64 -t mnemosyne-claude containers/claude/
podman build --platform linux/amd64 -t mnemosyne-hapi-hub containers/hapi-hub/
```

The `claude/Containerfile` accepts a `BASE_IMAGE` build-arg (defaults to `localhost/mnemosyne-base:latest`). CI overrides this to `ghcr.io/empiria/mnemosyne-base:latest`.

## Running

The preferred way to launch an agent session is `mnemosyne agent start <project>`. The commands below are for reference or debugging.

### Claude agent

```sh
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

```sh
podman run -d --rm \
  -p 3006:3006 \
  -e HAPI_MODE=relay \
  -e CLI_API_TOKEN="$CLI_API_TOKEN" \
  mnemosyne-hapi-hub
```

## Configuration

### /config mount

Mount `containers/config/` at `/config` inside the container. The entrypoint merges:

- `/config/.claude/` -> `~/.claude/` (symlinks, non-destructive — existing files are not overwritten)
- `/config/zellij/` -> `~/.config/zellij/` (copied, for non-Claude containers that use Zellij)

This allows shared agent config (MCP servers, commands, rules) without baking it into the image.

### Volume mounts

| Host path | Container path | Mode | Purpose |
|-----------|---------------|------|---------|
| `/path/to/project` | `/path/to/project` | rw | Project repository (mounted at host path) |
| `$MNEMOSYNE_VAULT` | `/vault` | ro | Mnemosyne knowledge vault |
| `containers/config` | `/config` | ro | Shared agent config |
| `$SSH_AUTH_SOCK` | `/tmp/ssh-auth-sock` | rw | SSH agent for git auth |

### Project dependencies

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

See `projects/<org>/<slug>/container.toml` in the Mnemosyne vault for project-specific declarations.

## Authentication

### Claude Code (OAuth)

Claude Code authenticates via OAuth subscription token, not an API key.

1. Authenticate on the host: `claude auth login`
2. Find the token: `cat ~/.claude/.credentials.json`
3. Pass as env var: `-e CLAUDE_CODE_OAUTH_TOKEN="<token>"`

The entrypoint writes `~/.claude.json` with `hasCompletedOnboarding: true` automatically when the token is present, bypassing the interactive setup.

### GitHub CLI

The `gh` CLI authenticates via the `GH_TOKEN` environment variable. Add it to `~/.config/mnemosyne/agent.env`:

```
GH_TOKEN=ghp_xxxxxxxxxxxx
```

`mnemosyne agent start` passes it into the container automatically. Create a token at https://github.com/settings/tokens (classic with `repo` scope, or fine-grained).

### SSH agent forwarding

Pass the host SSH agent socket to avoid storing keys inside the container:

```sh
-v $SSH_AUTH_SOCK:/tmp/ssh-auth-sock -e SSH_AUTH_SOCK=/tmp/ssh-auth-sock
```

The entrypoint creates a stable symlink at `~/.ssh-auth-sock` so the socket path remains constant across container restarts.

### Remote control (Claude containers)

Claude containers run `claude remote-control` as their main process. This makes the session available from claude.ai/code or the Claude mobile app — no WireGuard or port forwarding needed.

Get the session URL:

```bash
mnemosyne agent remote [project]
```

Add `--qr` for a QR code to scan from your phone. Each remote session creates its own git worktree automatically.

### hapi mobile relay

For non-Claude agent containers (e.g. opencode), set `CLI_API_TOKEN` to enable the hapi relay. The entrypoint starts hapi automatically when this variable is present. Claude containers use Claude's built-in remote control instead — see [Remote control](#remote-control-claude-containers) above.
