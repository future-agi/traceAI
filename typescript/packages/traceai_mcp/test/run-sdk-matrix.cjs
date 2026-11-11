// Cloud/CI only. Installs exact real upstream SDKs into isolated temporary directories.
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const pins = require('./sdk-versions.json');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'media-sdk-matrix-'));
const evidence = path.resolve(process.argv[2] || dir);
fs.mkdirSync(evidence, { recursive: true });
try {
  for (const [kind, name, versions, testFile, envKey] of [
    ['mcp', '@modelcontextprotocol/sdk', pins.mcp, path.join(__dirname, 'real-sdk.test.cjs'), 'MCP_SDK_ROOT'],
    ['agent', '@anthropic-ai/claude-agent-sdk', pins.claudeAgent, path.resolve(__dirname, '../../traceai_claude_agent_sdk/test/real-sdk.test.cjs'), 'AGENT_SDK_ROOT'],
  ]) {
    for (const [tier, version] of Object.entries(versions)) {
      const prefix = path.join(dir, `${kind}-${tier}`); fs.mkdirSync(prefix);
      console.log(`STAGE ${kind} ${tier} ${version}`);
      const install = execFileSync('npm', ['install', '--prefix', prefix, '--ignore-scripts', '--no-audit', '--no-fund', `${name}@${version}`], { encoding: 'utf8', timeout: 180000 });
      fs.writeFileSync(path.join(evidence, `${kind}-${tier}-install.log`), install);
      const actual = JSON.parse(fs.readFileSync(path.join(prefix, 'node_modules', name, 'package.json'))).version;
      if (actual !== version) throw Error('version mismatch');
      const result = spawnSync(process.execPath, [path.join(__dirname, 'run-attachments.cjs'), testFile], { encoding: 'utf8', timeout: 240000, env: { ...process.env, [envKey]: path.join(prefix, 'node_modules', name) } });
      const log = (result.stdout || '') + (result.stderr || '');
      fs.writeFileSync(path.join(evidence, `${kind}-${tier}.log`), log); console.log(log);
      if (result.error) throw result.error;
      if (result.status !== 0) throw Error(`${kind} ${version} qualification failed`);
    }
  }
  fs.writeFileSync(path.join(evidence, 'sdk-versions.json'), JSON.stringify({ node: process.version, ...pins }, null, 2));
} finally { fs.rmSync(dir, { recursive: true, force: true }); }
