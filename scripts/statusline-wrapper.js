#!/usr/bin/env node
// Statusline wrapper — combines mnemosyne prefix with GSD statusline
// Lives in the vault's mnemosyne_scripts/ dir, available to client projects via symlink.
// Configured as the statusLine command in project .claude/settings.json.

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const scriptsDir = __dirname;
const globalHooksDir = path.join(os.homedir(), '.claude', 'hooks');

// Collect stdin to pass to GSD statusline
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  let output = '';

  // Mnemosyne staleness prefix (co-located script)
  try {
    const result = execSync(`node "${path.join(scriptsDir, 'mnemosyne-statusline.js')}"`, {
      encoding: 'utf8',
      timeout: 2000
    });
    output += result;
  } catch (e) {}

  // GSD statusline (global hooks dir, installed by GSD)
  const gsdStatusline = path.join(globalHooksDir, 'gsd-statusline.js');
  try {
    if (fs.existsSync(gsdStatusline)) {
      const result = execSync(`node "${gsdStatusline}"`, {
        input,
        encoding: 'utf8',
        timeout: 2000
      });
      output += result;
    }
  } catch (e) {}

  process.stdout.write(output);
});
