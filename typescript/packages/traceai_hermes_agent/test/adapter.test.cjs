const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { discoverHermesDirectory, hermesAdapter, NATIVE_SOURCE } = require('../dist/index.js');

async function fixture(fn) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-adapter-'));
  const old = { ...process.env };
  const bin = path.join(root, 'bin'); await fs.mkdir(bin);
  const log = path.join(root, 'calls.jsonl');
  await fs.writeFile(path.join(bin, 'hermes'), `#!${process.execPath}\nconst fs=require('fs');const a=process.argv.slice(2);fs.appendFileSync(process.env.CALLS,JSON.stringify({args:a,home:process.env.HERMES_HOME})+'\\n');if(a[0]==='profile')console.log('Path:           '+process.env.PROFILE);else if(a.includes('--help'))console.log('--enable --json');else if(a[1]==='list')console.log(process.env.ROWS||'[]');`, { mode: 0o700 });
  process.env.PATH = bin + path.delimiter + process.env.PATH;
  process.env.PROFILE = path.join(root, 'active-profile'); process.env.CALLS = log; delete process.env.HERMES_HOME;
  const context = { config: { paths: { user: path.join(root, 'active-profile', 'futureagi.json'), configDir: path.join(root, 'config') } }, scope: 'user' };
  try { await fn({ root, context, calls: async () => (await fs.readFile(log, 'utf8')).trim().split('\n').map(JSON.parse) }); }
  finally { for (const key of Object.keys(process.env)) if (!(key in old)) delete process.env[key]; Object.assign(process.env, old); await fs.rm(root, { recursive: true, force: true }); }
}

test('native active profile discovery, explicit override and invalid path', async () => fixture(async ({ root, calls }) => {
  assert.equal(await discoverHermesDirectory(), path.join(root, 'active-profile'));
  assert.equal(await discoverHermesDirectory({ HERMES_HOME: '~/named', HOME: root }), path.join(root, 'named'));
  await assert.rejects(discoverHermesDirectory({ HERMES_HOME: '$UNEXPANDED/profile' }), /PATH_INVALID/);
  assert.deepEqual((await calls())[0].args, ['profile']);
}));

test('install preserves native consent and pins discovered profile', async () => fixture(async ({ context, calls }) => {
  assert.equal((await hermesAdapter.detect(context)).supported, true);
  const result = await hermesAdapter.install(context);
  assert.equal(result.registered, false); assert.equal(result.trust, 'required');
  const install = (await calls()).find(c => c.args[1] === 'install' && !c.args.includes('--help'));
  assert.deepEqual(install.args, ['plugins', 'install', NATIVE_SOURCE, '--enable']);
  assert.equal(install.home, path.dirname(context.config.paths.user));
}));

test('repeat setup is idempotent; unowned install is never removed', async () => fixture(async ({ context, calls }) => {
  process.env.ROWS = JSON.stringify([{ name: 'futureagi', version: '0.1.1', status: 'enabled', source: 'user' }, { name: 'other', version: '1', status: 'enabled', source: 'user' }]);
  assert.equal((await hermesAdapter.install(context)).registered, true);
  assert.equal((await hermesAdapter.uninstall(context)).registered, true);
  assert.ok((await calls()).every(c => c.args[1] === 'list' || c.args[0] === '--version'));
}));

test('old and duplicate plugins are not presented as registered', async () => fixture(async ({ context }) => {
  process.env.ROWS = JSON.stringify([{ name: 'futureagi', version: '0.1.0', status: 'enabled' }]);
  assert.equal((await hermesAdapter.inspect(context)).registered, false);
  process.env.ROWS = JSON.stringify([{ name: 'futureagi', version: '0.1.1', status: 'enabled' }, { name: 'futureagi', version: '0.1.1', status: 'enabled' }]);
  assert.equal((await hermesAdapter.inspect(context)).duplicate, true);
}));
