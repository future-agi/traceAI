const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

test('README TraceAI Core setup traces a real dynamic ESM import without a loader', () => {
  const result = spawnSync(process.execPath, [path.join(__dirname, 'fixtures/readme.cjs')], {
    env: process.env, encoding: 'utf8', timeout: 15000,
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
