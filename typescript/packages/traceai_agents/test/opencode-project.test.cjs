const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createServer } = require('node:http');
const cli = path.resolve(__dirname, '../dist/cli.js');

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'opencode-project-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const project = path.join(home, 'project'), bin = path.join(home, 'bin');
  await fs.mkdir(project); await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, 'opencode'), '#!/bin/sh\nprintf "1.19.0\\n"\n', { mode: 0o755 });
  const server = createServer((req, res) => { req.resume(); req.on('end', () => { res.setHeader('content-type', 'application/json'); res.end('{}'); }); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const env = { HOME: home, PATH: `${bin}:${process.env.PATH}`, XDG_CONFIG_HOME: path.join(home, '.config'), XDG_STATE_HOME: path.join(home, '.state') };
  const credentials = { FI_API_KEY: 'synthetic-public', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` };
  async function run(args, vars = {}, cwd = project) {
    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [cli, ...args, '--json'], { cwd, env: { ...env, ...vars }, stdio: ['ignore', 'pipe', 'pipe'] });
      let stdout = '', stderr = '';
      child.stdout.on('data', x => stdout += x); child.stderr.on('data', x => stderr += x);
      child.on('error', reject); child.on('close', code => resolve({ code, data: JSON.parse(stdout), stderr }));
    });
  }
  return { home, project, credentials, run };
}
test('project setup followed by unqualified status and uninstall uses the current project', async t => {
  const f = await fixture(t);
  const setup = await f.run(['setup', 'opencode-v1', '--scope', 'project', '--project', 'project-test'], f.credentials);
  assert.equal(setup.code, 0, JSON.stringify(setup));
  const status = await f.run(['status', 'opencode-v1']);
  assert.equal(status.data.enabled, true);
  assert.equal(status.data.registration, 'REGISTERED');
  assert.deepEqual(status.data.issues, ['ACTIVATION_NOT_OBSERVED']);
  assert.equal((await f.run(['uninstall', 'opencode-v1'])).code, 0);
  const host = JSON.parse(await fs.readFile(path.join(f.project, 'opencode.json'), 'utf8'));
  assert.deepEqual(host.plugin, []);
});
test('default setup detects a copied project observer instead of claiming registration', async t => {
  const f = await fixture(t);
  await fs.mkdir(path.join(f.project, '.opencode/plugins'), { recursive: true });
  await fs.writeFile(path.join(f.project, '.opencode/plugins/futureagi-otlp.ts'), '// older observer');
  const setup = await f.run(['setup', 'opencode-v1', '--project', 'project-test'], f.credentials);
  assert.equal(setup.code, 2);
  assert.equal(setup.data.registered, false);
  const status = await f.run(['status', 'opencode-v1']);
  assert.ok(status.data.issues.includes('DUPLICATE_REGISTRATION'));
});
test('explicit project-dir overrides cwd for configuration and owned uninstall', async t => {
  const f = await fixture(t);
  assert.equal((await f.run(['setup', 'opencode-v1', '--scope', 'project', '--project', 'project-test'], f.credentials)).code, 0);
  const status = await f.run(['status', 'opencode-v1', '--project-dir', f.project], {}, f.home);
  assert.equal(status.data.enabled, true);
  assert.equal(status.data.registration, 'REGISTERED');
  assert.equal((await f.run(['uninstall', 'opencode-v1', '--project-dir', f.project], {}, f.home)).code, 0);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.project, 'opencode.json'), 'utf8')).plugin, []);
});
