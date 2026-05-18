#!/bin/bash
set -euo pipefail
VAULT="${MNEMOSYNE_VAULT:-/vault}"
SETUP_SCRIPT="${VAULT}/agents/scion-template/setup-commands.sh"
if [ -f "$SETUP_SCRIPT" ]; then
  bash "$SETUP_SCRIPT"
fi
