#!/bin/bash
set -e
rm -f /tmp/.entrypoint-ready
WORKDIR="$(pwd)"

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

# Claude Code auth — only when credentials are absent (legacy Podman fallback)
# SCION's Claude harness writes .credentials.json before entrypoint runs
if [[ ! -f ~/.claude/.credentials.json ]]; then
    if [[ -n "${CLAUDE_CODE_CREDENTIALS:-}" || -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
        existing="{}"
        [[ -f ~/.claude.json ]] && existing=$(cat ~/.claude.json)
        ws="${WORKSPACE_PATH:-/workspace}"
        echo "$existing" | jq --arg ws "$ws" '. + {"hasCompletedOnboarding":true,"theme":"dark","numStartups":1,"projects":{($ws):{"hasTrustDialogAccepted":true,"allowedTools":[]}}}' > ~/.claude.json
        mkdir -p ~/.claude
        if [[ -n "${CLAUDE_CODE_CREDENTIALS:-}" ]]; then
            echo "$CLAUDE_CODE_CREDENTIALS" > ~/.claude/.credentials.json
        else
            printf '{"claudeAiOauth":{"accessToken":"%s"}}\n' "$CLAUDE_CODE_OAUTH_TOKEN" > ~/.claude/.credentials.json
        fi
    fi
fi

# Mnemosyne CLI — install from mounted source or published package
if command -v uv &>/dev/null; then
    if ! command -v mnemosyne &>/dev/null; then
        if [[ -d /mnemosyne-cli/src/mnemosyne_cli ]]; then
            uv pip install -e /mnemosyne-cli/ 2>&1 || echo "WARN: mnemosyne CLI install failed (non-fatal)"
        else
            echo "Installing mnemosyne-cli from GitHub..."
            uv pip install "mnemosyne-cli @ git+https://github.com/Empiria/mnemosyne-cli.git" 2>&1 || echo "WARN: mnemosyne CLI install from GitHub failed (non-fatal)"
        fi
    fi
fi

# Link vault agent commands into Claude Code (only claude-code-command.md files)
if [[ -d /vault/agents ]]; then
    mkdir -p ~/.claude/commands
    for cmd_file in $(find /vault/agents -name claude-code-command.md 2>/dev/null); do
        [[ -f "$cmd_file" ]] || continue
        name=$(basename "$(dirname "$cmd_file")")
        ln -sf "$cmd_file" ~/.claude/commands/"$name".md
    done
fi

if command -v claude &>/dev/null; then
    echo "Claude Code $(claude --version) ready"
else
    echo "Agent environment ready"
fi

touch /tmp/.entrypoint-ready
cd "$WORKDIR"
exec "$@"
