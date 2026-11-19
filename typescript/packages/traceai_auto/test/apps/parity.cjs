const assert = require('node:assert/strict');
const mode = process.env.MODE, error = new Error('parity transport failure');
let original, promise, unhandled = 0, handled = 0, spans = 0;
const url = 'https://api.openai.com/v1/chat/completions', init = { method: 'POST', body: '{"messages":[]}' };
globalThis.fetch = () => {
  if (mode.startsWith('fetch')) return (original = Promise.reject(error));
  return (original = Promise.resolve(new Response(new ReadableStream({
    pull(controller) { if (!mode.startsWith('cancel')) controller.error(error); },
    cancel() { return Promise.reject(error); },
  }, { highWaterMark: 0 }))));
};
if (process.env.AGENT === '1') require('@traceai/auto').init({ apiKey: 'fixture-private-key', secretKey: 'fixture-private-secret', instrumentations: ['wire'], exporter: { export(_spans, done) { spans++; done({ code: 0 }); }, async shutdown() {} } });
if (!mode.endsWith('fatal')) {
  process.on('unhandledRejection', (reason, rejected) => {
    assert.equal(reason, error); assert.equal(rejected, promise); unhandled++;
    setImmediate(() => promise.catch(() => {}));
  });
  process.on('rejectionHandled', rejected => { assert.equal(rejected, promise); handled++; });
}
(async () => {
  if (mode.startsWith('fetch')) { promise = fetch(url, init); assert.equal(promise, original); }
  else {
    const response = await fetch(url, init);
    promise = mode.startsWith('cancel') ? response.body.cancel('consumer') : response.body.getReader().read();
  }
  if (mode.endsWith('await')) { try { await promise; assert.fail('must reject'); } catch (e) { assert.equal(e, error); } }
  if (mode.endsWith('fatal')) return;
  // Native rejection notifications run on consecutive turns, without timer races.
  await new Promise(resolve => setImmediate(() => setImmediate(() => setImmediate(resolve))));
  console.log(JSON.stringify({ unhandled, handled }));
})().catch(failure => { console.error(failure); process.exitCode = 1; });
