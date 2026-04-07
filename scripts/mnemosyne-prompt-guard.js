#!/usr/bin/env node
// Prompt guard hook -- pipes stdin to mnemosyne CLI
const { execSync } = require('child_process');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  try {
    const result = execSync('mnemosyne hook prompt-guard', {
      input,
      encoding: 'utf8',
      timeout: 3000
    });
    if (result.trim()) process.stdout.write(result);
  } catch (e) {}
});
