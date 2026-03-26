#!/usr/bin/env node
// Mnemosyne Staleness — Statusline prefix
// Shows warning when local main is behind origin/main
// Installed into client projects via the `mnemosyne_scripts` symlink.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const homeDir = os.homedir();
const cacheFile = path.join(homeDir, '.claude', 'cache', 'mnemosyne-status.json');
const STALE_SECONDS = 60;

let cache;
try {
  cache = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
} catch (e) {
  // No cache yet — bootstrap by running the CLI synchronously
  try {
    const { execSync } = require('child_process');
    execSync('mnemosyne status --json', { stdio: ['ignore', 'pipe', 'ignore'], timeout: 15000 });
    cache = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
  } catch (e2) {
    // CLI failed or no vault found — nothing to show
    process.exit(0);
  }
}

// If cache is stale, fire-and-forget a background refresh
const age = Math.floor(Date.now() / 1000) - (cache.checked || 0);
if (age > STALE_SECONDS) {
  try {
    const child = spawn('mnemosyne', ['status', '--json'], {
      stdio: 'ignore',
      detached: true
    });
    child.unref();
  } catch (e) {}
}

// Show status
if (cache.behind > 0) {
  process.stdout.write(`\x1b[33m⚠ mnemosyne: ${cache.behind} behind\x1b[0m │ `);
} else if (cache.ahead > 0) {
  process.stdout.write(`\x1b[33m⚠ mnemosyne: ${cache.ahead} ahead\x1b[0m │ `);
} else {
  process.stdout.write(`\x1b[32m✓ mnemosyne\x1b[0m │ `);
}
