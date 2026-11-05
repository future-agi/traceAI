// Each subprocess gets a fresh module cache so automatic instrumentation is real.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

for (const mode of ['unpatched', 'manual-cjs', 'manual-esm', 'automatic',
  'mixed-classes', 'repeated', 'unpatch-one', 'foreign-wrapper']) {
  test(`real SDK entrypoints: ${mode}`, () => {
    const result = spawnSync(process.execPath, [path.join(__dirname, 'fixtures/entrypoints.cjs'), mode], {
      env: process.env, encoding: 'utf8', timeout: 15000,
    });
    assert.ifError(result.error);
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  });
}
