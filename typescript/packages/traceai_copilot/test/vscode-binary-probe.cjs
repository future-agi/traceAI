// Metadata probe only. No prompts, sign-in, extension installation or tracing qualification.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { createReadStream } = require('node:fs');
const { spawnSync } = require('node:child_process');
const assert = require('node:assert/strict');
const fixture = require('./vscode-qualification.json');
async function main() {
  const [archive, extracted, output] = process.argv.slice(2);
  const hash = crypto.createHash('sha256');
  for await (const chunk of createReadStream(archive)) hash.update(chunk);
  assert.equal(hash.digest('hex'), fixture.binaryProbe.sha256);
  const isolated = path.join(output, 'editor-home');
  await fs.mkdir(isolated, { recursive: true, mode: 0o700 });
  const binary = path.join(extracted, 'VSCode-linux-x64/bin/code');
  const env = { HOME: isolated, PATH: '/usr/bin:/bin', XDG_CONFIG_HOME: path.join(isolated, '.config') };
  const args = ['--no-sandbox', '--user-data-dir', path.join(isolated, 'data')];
  const version = spawnSync(binary, [...args, '--version'], { env, cwd: output, encoding: 'utf8', timeout: 30000 });
  assert.equal(version.status, 0, version.stderr);
  assert.match(version.stdout, new RegExp(fixture.binaryProbe.productVersion.replaceAll('.', '\\.')));
  assert.ok(version.stdout.includes(fixture.binaryProbe.commit));
  const help = spawnSync(binary, [...args, '--help'], { env, cwd: output, encoding: 'utf8', timeout: 30000 });
  assert.equal(help.status, 0, help.stderr);
  assert.match(help.stdout, /--profile/);
  assert.match(help.stdout, /--user-data-dir/);
  const receipt = {
    productVersion: fixture.binaryProbe.productVersion, commit: fixture.binaryProbe.commit,
    binarySha256: fixture.binaryProbe.sha256, binaryVersionExit: version.status, binaryHelpExit: help.status,
    realEditorPromptRun: false, modelRequests: 0, observedTraceSpans: 0, actualGuiValidation: false,
    qualified: false, blocker: fixture.blocker,
    note: 'Real distributed editor executable metadata only; not a CLI or editor tracing E2E.',
  };
  await fs.writeFile(path.join(output, 'binary-probe.json'), JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify(receipt));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
