// Exercise only public lifecycle APIs, with fresh module caches and real SDKs.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

for (const entrypoint of ['manual-cjs', 'manual-esm', 'automatic-cjs']) {
  for (const scenario of ['disable', 'provider', 'foreign-wrapper', 'opaque-wrapper']) {
    test(`public lifecycle: ${entrypoint}, ${scenario}`, () => {
      const result = spawnSync(process.execPath, [
        path.join(__dirname, 'fixtures/lifecycle.cjs'), entrypoint, scenario,
      ], { env: process.env, encoding: 'utf8', timeout: 15000 });
      assert.ifError(result.error);
      assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
    });
  }
}
