const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { opencodeAdapter, OPENCODE_PACKAGE } = require('../dist/adapters/opencode.js');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-opencode-adapter-'));
  const host = path.join(root, 'host'); const configDir = path.join(root, 'fi');
  const projectDir = path.join(root, 'project');
  for (const p of [host, configDir, projectDir]) await fs.mkdir(p, { mode: 0o700 });
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return { root, host, context: { config: { paths: { user: path.join(host, 'futureagi.json'), configDir } }, scope: 'user', projectDir } };
}
test('OpenCode inspects JSONC without rewriting comments or unrelated configuration', async t => {
  const f = await fixture(t); const file = path.join(f.host, 'opencode.jsonc');
  const text = `// retained\n{"plugins":["other-plugin", {"package":"${OPENCODE_PACKAGE}"},], "model":"my/model"}`;
  await fs.writeFile(file, text, { mode: 0o600 });
  const result = await opencodeAdapter.inspect(f.context);
  assert.equal(result.registered, true); assert.equal(result.activated, undefined);
  assert.equal(await fs.readFile(file, 'utf8'), text);
});
test('OpenCode reports duplicate global/project packages', async t => {
  const f = await fixture(t);
  for (const base of [f.host, f.context.projectDir])
    await fs.writeFile(path.join(base, 'opencode.json'), JSON.stringify({ plugins: [OPENCODE_PACKAGE] }), { mode: 0o600 });
  const result = await opencodeAdapter.inspect(f.context);
  assert.equal(result.registered, false); assert.equal(result.duplicate, true);
});
test('OpenCode rejects symlink and malformed config without modifying either', async t => {
  const f = await fixture(t); const target = path.join(f.root, 'target');
  await fs.writeFile(target, '{}', { mode: 0o600 });
  await fs.symlink(target, path.join(f.host, 'opencode.json'));
  await assert.rejects(opencodeAdapter.inspect(f.context));
  assert.equal(await fs.readFile(target, 'utf8'), '{}');
  await fs.unlink(path.join(f.host, 'opencode.json'));
  await fs.writeFile(path.join(f.host, 'opencode.json'), '{broken', { mode: 0o600 });
  await assert.rejects(opencodeAdapter.inspect(f.context), /INVALID_OPENCODE_CONFIG/);
});
test('OpenCode uninstall preserves manually registered plugins without an ownership record', async t => {
  const f = await fixture(t); const file = path.join(f.host, 'opencode.json');
  const text = JSON.stringify({ plugins: [OPENCODE_PACKAGE, 'other'] });
  await fs.writeFile(file, text, { mode: 0o600 });
  assert.equal((await opencodeAdapter.uninstall(f.context)).registered, true);
  assert.equal(await fs.readFile(file, 'utf8'), text);
});
