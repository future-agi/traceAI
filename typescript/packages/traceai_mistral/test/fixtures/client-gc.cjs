const assert = require('node:assert/strict');
const { MistralInstrumentation, isPatched } = require('../../dist/src');
const instrumentation = new MistralInstrumentation();
const weak = (() => {
  const client = { chat: { complete: async () => ({}) } };
  instrumentation.instrumentClient(client);
  return new WeakRef(client.chat);
})();
(async () => {
  for (let attempt = 0; attempt < 20; attempt++) {
    await new Promise(setImmediate);
    global.gc();
    if (!weak.deref()) break;
  }
  assert.equal(weak.deref(), undefined, 'instrumentation retained a discarded client resource');
  assert.equal(isPatched(), false);
  instrumentation.disable();
  instrumentation.enable();
  instrumentation.disable();
})().catch(error => { console.error(error); process.exitCode = 1; });
