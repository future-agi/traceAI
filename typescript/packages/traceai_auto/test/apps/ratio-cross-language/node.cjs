// Loaded with the real @traceai/auto/register preload. No application provider,
// sampler, propagator, processor, exporter or sampling metadata is installed here.
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const auto = require('@traceai/auto');

assert.equal(process.env.FI_SAMPLE_RATE, undefined);
assert.equal(process.env.OTEL_TRACES_SAMPLER, undefined);
assert.equal(process.env.OTEL_TRACES_SAMPLER_ARG, undefined);
assert.equal(auto.report().active, true, 'The real Node preload must have started');
for (const name of ['http', 'wire']) {
  assert.equal(auto.report().integrations.find(item => item.name === name)?.status, 'instrumented');
}

let calls = 0;
const server = createServer(async (req, res) => {
  try {
    assert.equal(req.url, '/ratio');
    assert.equal(++calls, 1);
    // AI work admits the real incoming HTTP SERVER span to the AI-only route.
    // The mock model and both production exporters talk only to the local stub.
    const response = await fetch(process.env.TEST_COLLECTOR + '/v1/chat/completions', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'ratio fixture' }] }),
      signal: AbortSignal.timeout(10000),
    });
    assert.equal(response.status, 200);
    await response.json();
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ traceparent: req.headers.traceparent, tracestate: req.headers.tracestate ?? null }));
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
    res.writeHead(500).end('{}');
  }
});

(async () => {
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    console.log(JSON.stringify({ ready: `http://127.0.0.1:${server.address().port}` }));
    // The runner closes stdin only after Python has received the response. Unlike
    // an extra shutdown HTTP request, this creates no unrelated root trace.
    await new Promise(resolve => { process.stdin.once('end', resolve); process.stdin.resume(); });
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await auto.forceFlush();
    await auto.shutdown();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
