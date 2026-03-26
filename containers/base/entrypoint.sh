#!/bin/bash
set -e
rm -f /tmp/.entrypoint-ready
WORKDIR="$(pwd)"

# Merge /config/.claude into ~/.claude
if [[ -d /config/.claude ]]; then
    mkdir -p ~/.claude
    for item in /config/.claude/* /config/.claude/.*; do
        [[ "$(basename "$item")" == "." || "$(basename "$item")" == ".." ]] && continue
        [[ ! -e "$item" && ! -L "$item" ]] && continue
        name=$(basename "$item")
        target=~/.claude/"$name"
        # Deep-merge JSON files, symlink everything else
        if [[ "$name" == *.json && -f "$target" ]]; then
            merged=$(jq -s '.[0] * .[1]' "$target" "$item")
            # Remove symlink first so we write a real file, not through a RO mount
            [[ -L "$target" ]] && rm "$target"
            echo "$merged" > "$target"
        elif [[ ! -e "$target" && ! -L "$target" ]]; then
            if [[ -L "$item" && ! -e "$item" ]]; then
                mkdir -p "$target"
            else
                ln -s /config/.claude/"$name" "$target"
            fi
        fi
    done
fi

# Copy zellij config if present
[[ -d /config/zellij ]] && mkdir -p ~/.config && cp -r /config/zellij ~/.config/

# SSH agent socket — create a stable symlink so it survives socket path changes
if [[ -n "${SSH_AUTH_SOCK:-}" && -S "${SSH_AUTH_SOCK}" ]]; then
    mkdir -p ~/.ssh
    ln -sf "${SSH_AUTH_SOCK}" /home/agent/.ssh-auth-sock
    export SSH_AUTH_SOCK=/home/agent/.ssh-auth-sock
fi

# Mark execution context as inside a Mnemosyne container (used by 'mnemosyne work start')
export MNEMOSYNE_CONTAINER=1

# Trust mounted volumes (ownership differs across container boundary)
git config --global --add safe.directory "${WORKSPACE_PATH:-/workspace}"
git config --global --add safe.directory /vault
if [[ -n "${MNEMOSYNE_VAULT_WORKTREE:-}" ]]; then
    git config --global --add safe.directory "${MNEMOSYNE_VAULT_WORKTREE}"
fi
if [[ -n "${MNEMOSYNE_EXTRA_VAULTS:-}" ]]; then
    for entry in ${MNEMOSYNE_EXTRA_VAULTS}; do
        IFS=: read -r _vault_name mount <<< "$entry"
        git config --global --add safe.directory "$mount"
    done
fi

# Set MNEMOSYNE_VAULT — prefer project-specific worktree over main vault
if [[ -d /vault ]]; then
    if [[ -n "${MNEMOSYNE_VAULT_WORKTREE:-}" && -d "${MNEMOSYNE_VAULT_WORKTREE}" ]]; then
        export MNEMOSYNE_VAULT="${MNEMOSYNE_VAULT_WORKTREE}"
    else
        export MNEMOSYNE_VAULT=/vault
    fi
fi

# Set up qmd: configure collections for all mounted vaults
if command -v qmd &>/dev/null; then
    mkdir -p ~/.config/qmd

    # Start with primary vault collection
    vault_name="${MNEMOSYNE_PRIMARY_VAULT_NAME:-mnemosyne}"
    cat > ~/.config/qmd/index.yml << QMDEOF
collections:
  ${vault_name}:
    path: /vault
    pattern: "**/*.md"
QMDEOF

    # Append additional vault collections if passed
    if [[ -n "${MNEMOSYNE_EXTRA_VAULTS:-}" ]]; then
        for entry in ${MNEMOSYNE_EXTRA_VAULTS}; do
            IFS=: read -r name mount <<< "$entry"
            if [[ -d "$mount" ]]; then
                printf '  %s:\n    path: %s\n    pattern: "**/*.md"\n' "$name" "$mount" \
                    >> ~/.config/qmd/index.yml
            fi
        done
    fi

    # Build index + embeddings on first run (named volume persists across restarts)
    needs_index=false
    for collection in $(grep -E '^\s{2}\w+:$' ~/.config/qmd/index.yml | sed 's/://;s/^  //'); do
        if [[ -z "$(qmd ls "$collection" 2>/dev/null | grep -v '^No files')" ]]; then
            needs_index=true
            break
        fi
    done

    if $needs_index; then
        echo "Building qmd index (background)..."
        (
            qmd update
            qmd embed
            qmd context add "qmd://${vault_name}" "Empiria institutional knowledge vault — technology standards, project history, and reference material"
            qmd context add "qmd://${vault_name}/technologies" "Technology standards, learnings, decisions, and reference notes"
            qmd context add "qmd://${vault_name}/projects" "Client project GSD planning data, phase summaries, and research"
            echo "qmd index ready"
        ) &>/tmp/qmd-init.log &
    fi
fi

# Claude Code auth + onboarding bypass
if [[ -n "${CLAUDE_CODE_CREDENTIALS:-}" || -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    # Merge onboarding fields into existing claude.json (install may have created one)
    existing="{}"
    [[ -f ~/.claude.json ]] && existing=$(cat ~/.claude.json)
    ws="${WORKSPACE_PATH:-/workspace}"
    echo "$existing" | jq --arg ws "$ws" '. + {"hasCompletedOnboarding":true,"theme":"dark","numStartups":1,"projects":{($ws):{"hasTrustDialogAccepted":true,"allowedTools":[]}}}' > ~/.claude.json
    mkdir -p ~/.claude
    # Write credentials: full JSON from keychain (preferred) or access-token-only fallback
    if [[ -n "${CLAUDE_CODE_CREDENTIALS:-}" ]]; then
        echo "$CLAUDE_CODE_CREDENTIALS" > ~/.claude/.credentials.json
    else
        printf '{"claudeAiOauth":{"accessToken":"%s"}}\n' "$CLAUDE_CODE_OAUTH_TOKEN" > ~/.claude/.credentials.json
    fi
fi

# Mnemosyne CLI — install from extracted package (skip if already installed)
if command -v uv &>/dev/null; then
    if ! command -v mnemosyne &>/dev/null; then
        # Install from mounted CLI source (development) or published package
        if [[ -d /mnemosyne-cli/src/mnemosyne_cli ]]; then
            uv pip install -e /mnemosyne-cli/ 2>&1 || echo "WARN: mnemosyne CLI install failed (non-fatal)"
        else
            echo "Installing mnemosyne-cli from PyPI..."
            uv pip install mnemosyne-cli 2>&1 || echo "WARN: mnemosyne CLI install from PyPI failed (non-fatal)"
        fi
    fi
fi

# Link vault agent commands into Claude Code (only claude-code-command.md files)
if [[ -d /vault/agents ]]; then
    mkdir -p ~/.claude/commands
    for cmd_file in /vault/agents/*/claude-code-command.md; do
        [[ -f "$cmd_file" ]] || continue
        name=$(basename "$(dirname "$cmd_file")")
        ln -sf "$cmd_file" ~/.claude/commands/"$name".md
    done
fi

# Install project dependencies from container.toml
if [[ -f /config/container.toml ]] && [[ -s /config/container.toml ]]; then
    # Set cache dirs to named volume subdirs (defence-in-depth; also set by agent.py -e flags)
    export UV_CACHE_DIR=/home/agent/.dep-cache/uv
    export PLAYWRIGHT_BROWSERS_PATH=/home/agent/.dep-cache/ms-playwright
    export CARGO_HOME=/home/agent/.dep-cache/cargo
    export NPM_CONFIG_PREFIX=/home/agent/.dep-cache/npm-global
    export PATH="$NPM_CONFIG_PREFIX/bin:$HOME/.cargo/bin:$PATH"

    # Pre-create cache subdirs in case volume is freshly created
    mkdir -p "$UV_CACHE_DIR" "$PLAYWRIGHT_BROWSERS_PATH" "$CARGO_HOME" "$NPM_CONFIG_PREFIX"

    # Cache apt .deb files and package lists on the persistent dep-cache volume
    sudo mkdir -p /home/agent/.dep-cache/apt/archives/partial /home/agent/.dep-cache/apt/lists/partial
    cat <<'APT_CONF' | sudo tee /etc/apt/apt.conf.d/99dep-cache >/dev/null
Dir::Cache::archives "/home/agent/.dep-cache/apt/archives";
Dir::State::lists "/home/agent/.dep-cache/apt/lists";
APT_CONF

    echo "Processing container.toml dependencies..."

    # Parse TOML to JSON using stdlib tomllib
    DEPS_JSON=$(python -c "
import tomllib, json
with open('/config/container.toml', 'rb') as f:
    data = tomllib.load(f)
print(json.dumps(data.get('dependencies', {})))
") || { echo "ERROR: Failed to parse /config/container.toml"; exit 1; }

    # apt packages (requires sudo — installed in base image)
    APT_PKGS=$(echo "$DEPS_JSON" | jq -r '(.apt // []) | .[]' 2>/dev/null)
    if [[ -n "$APT_PKGS" ]]; then
        echo "Installing apt packages: $(echo "$APT_PKGS" | tr '\n' ' ')"
        # shellcheck disable=SC2086
        sudo apt-get install -y --no-install-recommends $APT_PKGS || {
            echo "ERROR: apt install failed"; exit 1
        }
    fi

    # pip packages via uv (installed into the container venv)
    PIP_PKGS=$(echo "$DEPS_JSON" | jq -r '(.pip // []) | .[]' 2>/dev/null)
    if [[ -n "$PIP_PKGS" ]]; then
        echo "Installing pip packages: $(echo "$PIP_PKGS" | tr '\n' ' ')"
        # shellcheck disable=SC2086
        uv pip install $PIP_PKGS || {
            echo "ERROR: pip install failed"; exit 1
        }
    fi

    # npm packages
    NPM_PKGS=$(echo "$DEPS_JSON" | jq -r '(.npm // []) | .[]' 2>/dev/null)
    if [[ -n "$NPM_PKGS" ]]; then
        echo "Installing npm packages: $(echo "$NPM_PKGS" | tr '\n' ' ')"
        # shellcheck disable=SC2086
        npm install -g $NPM_PKGS || {
            echo "ERROR: npm install failed"; exit 1
        }
    fi

    # cargo crates
    CARGO_PKGS=$(echo "$DEPS_JSON" | jq -r '(.cargo // []) | .[]' 2>/dev/null)
    if [[ -n "$CARGO_PKGS" ]]; then
        echo "Installing cargo crates: $(echo "$CARGO_PKGS" | tr '\n' ' ')"
        while IFS= read -r pkg; do
            [[ -z "$pkg" ]] && continue
            echo "  cargo install $pkg"
            cargo install "$pkg" || { echo "ERROR: cargo install $pkg failed"; exit 1; }
        done <<< "$CARGO_PKGS"
    fi

    # run commands (arbitrary shell commands in order)
    RUN_CMDS=$(echo "$DEPS_JSON" | jq -r '(.run // []) | .[]' 2>/dev/null)
    if [[ -n "$RUN_CMDS" ]]; then
        while IFS= read -r run_cmd; do
            [[ -z "$run_cmd" ]] && continue
            echo "Running: $run_cmd"
            eval "$run_cmd" || { echo "ERROR: Command failed: $run_cmd"; exit 1; }
        done <<< "$RUN_CMDS"
    fi

    echo "Dependencies installed."
fi

if command -v claude &>/dev/null; then
    echo "Claude Code $(claude --version) ready"
else
    echo "Agent environment ready"
fi

# Start hapi relay if token is set
if [[ -n "${CLI_API_TOKEN:-}" ]] && command -v hapi &>/dev/null; then
    hub_url="${HAPI_API_URL:-http://localhost:3006}"
    echo "Starting hapi relay (hub: ${hub_url})..."

    # Purge inactive sessions from previous container runs
    jwt=$(curl -sf -X POST "${hub_url}/api/auth" \
        -H 'content-type: application/json' \
        -d "{\"accessToken\":\"${CLI_API_TOKEN}\"}" \
        | jq -r '.token // empty' 2>/dev/null) || true
    if [[ -n "$jwt" ]]; then
        curl -sf -H "Authorization: Bearer ${jwt}" "${hub_url}/api/sessions" \
            | jq -r '.sessions[] | select(.active == false) | .id' 2>/dev/null \
            | while read -r sid; do
                curl -sf -X DELETE -H "Authorization: Bearer ${jwt}" \
                    "${hub_url}/api/sessions/${sid}" >/dev/null 2>&1
            done
        echo "Cleaned up stale hub sessions"
    fi

    HOME=/tmp hapi --dangerously-skip-permissions &>/tmp/hapi.log &
fi

touch /tmp/.entrypoint-ready
cd "$WORKDIR"
exec "$@"
