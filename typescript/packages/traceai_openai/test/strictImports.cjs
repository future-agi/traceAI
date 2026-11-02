const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

// Run in a clean pnpm consumer with both hoisting settings disabled. Importing
// from the workspace or an npm consumer would hide missing direct dependencies.
const cwd = process.env.OPENAI_STRICT_CONSUMER_DIR;
assert.ok(cwd, 'OPENAI_STRICT_CONSUMER_DIR must name the non-hoisted consumer');
for (const mode of ['commonjs', 'module']) {
  test(`non-hoisted ${mode} import loads the packed instrumentation`, () => {
    const load = mode === 'module'
      ? 'import { OpenAIInstrumentation } from "@traceai/openai";'
      : 'const { OpenAIInstrumentation } = require("@traceai/openai");';
    const result = spawnSync(process.execPath, [`--input-type=${mode}`, '-e',
      `${load} if (typeof OpenAIInstrumentation !== "function") throw new Error("Missing constructor");`],
    { cwd, encoding: 'utf8' });
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr);
  });
}
