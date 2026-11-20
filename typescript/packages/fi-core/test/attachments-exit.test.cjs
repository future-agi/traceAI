/* Process-exit regression probes (design 8.5, T-LC-06..11). Each case runs in a separate Node process. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const packageJson = process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json');
const probe = path.join(__dirname, 'attachments-exit-probe.cjs');
for (const [name, limit] of [['disabled', 10000], ['enabled-idle', 10000], ['enabled-blocked', 10000], ['enabled-no-shutdown', 10000], ['mid-recording', 15000], ['import-pending', 10000]]) {
  test(`exit probe ${name} exits 0 under ${limit} ms`, () => {
    const start = Date.now();
    const result = spawnSync(process.execPath, [probe, name], { env: { ...process.env, FI_CORE_PACKAGE_JSON: packageJson }, timeout: limit, encoding: 'utf8' });
    const elapsed = Date.now() - start;
    assert.equal(result.signal, null, `${name} was killed (kept alive): ${result.stderr}`);
    assert.equal(result.status, 0, result.stderr); assert.match(result.stdout, /DONE/); assert.ok(elapsed < limit);
    console.log(`# probe ${name} ${elapsed} ms ${result.stdout.trim().split('\n').pop()}`);
  });
}
