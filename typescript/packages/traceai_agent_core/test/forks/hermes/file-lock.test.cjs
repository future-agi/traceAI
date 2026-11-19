const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const core = require('../../../dist/src/index.js');

test('a live writer renews its lease; the waiting JSON update re-reads instead of losing changes', { timeout: 20000 }, async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-lock-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const file = path.join(home, 'control.json');
  await core.writeProtected(file, '{"count":0}');
  const script = `
const fs=require('node:fs/promises'); const open=fs.open;
fs.open=async function(p,...args){const h=await open.call(fs,p,...args);
  if(p.endsWith('.tmp')){process.stdout.write('HELD\\n');await new Promise(r=>setTimeout(r,6000));}return h;};
require(${JSON.stringify(require.resolve('../../../dist/src/index.js'))}).updateJson(${JSON.stringify(file)},v=>({count:v.count+1})).catch(()=>process.exit(1));`;
  const writer = spawn(process.execPath, ['-e', script], { stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => writer.kill('SIGKILL'));
  const done = once(writer, 'exit');
  const [out] = await once(writer.stdout, 'data');
  assert.equal(out.toString(), 'HELD\n');
  const started = Date.now();
  await core.updateJson(file, old => ({ count: old.count + 1 }));
  assert.ok(Date.now() - started >= 5500, 'must wait beyond stale threshold for the live writer');
  assert.equal((await done)[0], 0);
  assert.deepEqual(await core.readJson(file), { count: 2 });
  await assert.rejects(fs.stat(file + '.lock-v2'), { code: 'ENOENT' });
});
