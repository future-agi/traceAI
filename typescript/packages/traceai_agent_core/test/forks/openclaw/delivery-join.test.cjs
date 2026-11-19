const { test } = require('node:test');
const assert = require('node:assert/strict');
const { BoundedSpanProcessor } = require('../../../dist/src/index.js');

test('a terminal flush joining an active export waits for its durable metadata', async () => {
  const processor = new BoundedSpanProcessor({ enabled: false, paths: {} });
  // Deterministically model an HTTP export whose metadata write has not settled.
  let settle;
  processor.active = Promise.resolve();
  processor.metadata = new Promise((resolve) => { settle = resolve; });
  let complete = false;
  const joined = processor.forceFlush().then(() => { complete = true; });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(complete, false);
  settle();
  await joined;
  assert.equal(complete, true);
  processor.active = undefined;
  await processor.shutdown();
});
