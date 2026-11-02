/* Offline OTLP wire check. Real @cursor/sdk is loaded, while its remote transport
 * is replaced by a synthetic agent: no Cursor account, login or network needed.
 * The exporter sends a real OTLP/JSON request to a loopback receiver. */
const assert = require('node:assert/strict');
const http = require('node:http');
const { Agent } = require('@cursor/sdk');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { CursorSDKInstrumentation } = require('../dist/src/index.js');

assert.equal(typeof Agent.create, 'function');
const secret = 'private summarized conversation';
let body;
const server = http.createServer((req, res) => {
  assert.equal(req.method, 'POST');
  assert.equal(req.url, '/v1/traces');
  assert.match(req.headers['content-type'], /application\/json/);
  const chunks = [];
  req.on('data', (chunk) => chunks.push(chunk));
  req.on('end', () => {
    body = Buffer.concat(chunks);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end();
  });
});
(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const exporter = new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` });
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory), new SimpleSpanProcessor(exporter)] });
  const instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider });
  const sdk = {
    Agent: class {
      static async create() {
        return {
          agentId: 'agent-synthetic',
          async send() { throw new Error('unexpected send'); },
          async summarize() {
            const events = [
              { type: 'summary', status: 'running', agent_id: 'agent-synthetic', run_id: 'run-synthetic' },
              { type: 'summary', status: 'completed', fallback: true, agent_id: 'agent-synthetic', run_id: 'run-synthetic' },
            ];
            return {
              id: 'run-synthetic', agentId: 'agent-synthetic', status: 'finished',
              async *stream() { yield* events; },
              async wait() { return { id: 'run-synthetic', status: 'finished', result: secret }; },
            };
          },
        };
      }
    }
  };
  assert.equal(instrumentation.manuallyInstrument(sdk), true);
  const agent = await sdk.Agent.create();
  const run = await agent.summarize();
  assert.deepEqual((await Array.fromAsync(run.stream())).map((event) => event.status), ['running', 'completed']);
  assert.equal((await run.wait()).result, secret);
  await provider.forceFlush();
  assert.equal(memory.getFinishedSpans().length, 1, 'one completed run span');
  assert.ok(body?.length, 'OTLP receiver received a JSON trace');
  const wire = JSON.parse(body.toString('utf8'));
  const spans = wire.resourceSpans.flatMap((resource) => resource.scopeSpans.flatMap((scope) => scope.spans));
  assert.equal(spans.length, 1);
  assert.equal(spans[0].name, 'Cursor Agent run');
  assert.ok(spans[0].events.some((event) => event.name === 'cursor.summary' &&
    event.attributes.some((attr) => attr.key === 'cursor.summary.fallback' && attr.value.boolValue === true)));
  assert.ok(!body.includes(Buffer.from(secret)), 'OTLP hides summary content by default');
  await provider.shutdown();
  instrumentation.unpatch();
  console.log('PASS loopback OTLP/JSON summary span and privacy');
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
