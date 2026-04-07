#!/usr/bin/env node
// Statusline wrapper — calls mnemosyne statusline (merged CLI command)
// Lives in mnemosyne-cli/scripts/, available to client projects via symlink.
// Configured as the statusLine command in project .claude/settings.json.

const { execSync } = require('child_process');

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  try {
    const result = execSync('mnemosyne statusline', {
      input,
      encoding: 'utf8',
      timeout: 3000
    });
    process.stdout.write(result);
  } catch (e) {
    // Silent failure — statusline is non-critical
  }
});
