// Synthetic SDK calls only; run under the network-denying review sandbox.
const { test, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const path = require('node:path');
const req = createRequire(path.resolve(process.env.OPENAI_CONSUMER_DIR, 'package.json'));
const OpenAI = req('openai');
const { OpenAIInstrumentation } = req('@traceai/openai');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = req('@opentelemetry/context-async-hooks');
const { trace, context } = req('@opentelemetry/api');
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const tracer = provider.getTracer('recall');
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
let instrumentation;
function instrument(traceConfig = {}) {
  instrumentation = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider); instrumentation.manuallyInstrument(OpenAI);
}
const fixture = { id: 'chat-synthetic', object: 'chat.completion', model: 'synthetic', choices: [{ index: 0, message: { role: 'assistant', content: 'SYNTHETIC-PRIVATE-ANSWER' }, finish_reason: 'stop' }] };
function client(body = fixture, child = false) {
  return new OpenAI({ apiKey: 'synthetic-private-key', maxRetries: 0, fetch: async () => {
    if (child) { const span = tracer.startSpan('synthetic-transport'); span.end(); }
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  } });
}
function attrs() { return exporter.getFinishedSpans()[0].attributes; }
afterEach(async () => { instrumentation?.disable(); instrumentation = undefined; exporter.reset(); });
after(async () => { context.disable(); await provider.shutdown(); });
for (const [flag, token, messages] of [
  ['FI_HIDE_INPUT_TEXT', 'SYNTHETIC-PRIVATE-PROMPT', [{ role: 'user', content: 'SYNTHETIC-PRIVATE-PROMPT' }]],
  ['FI_HIDE_OUTPUT_TEXT', 'SYNTHETIC-PRIVATE-ANSWER', []],
  ['FI_HIDE_INPUT_IMAGES', 'SYNTHETIC-PRIVATE-IMAGE', [{ role: 'user', content: [{ type: 'image_url', image_url: { url: 'https://image.example.invalid/SYNTHETIC-PRIVATE-IMAGE' } }] }]],
]) {
  test(`${flag} covers copied raw/value payloads`, async () => {
    process.env[flag] = 'true';
    try {
      instrument(); await client().chat.completions.create({ model: 'synthetic', messages });
      const leakingKeys = Object.entries(attrs()).filter(([k, v]) => String(v).includes(token)).map(([k]) => k);
      assert.deepEqual(leakingKeys, [], `sensitive marker survived in ${leakingKeys.join(', ')}`);
    } finally { delete process.env[flag]; }
  });
}
if (OpenAI.Responses) test('FI_HIDE_INPUTS covers Responses system instructions', async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  try {
    instrument(); await client({ id: 'resp-synthetic', object: 'response', status: 'completed', model: 'synthetic', output: [], instructions: 'SYNTHETIC-PRIVATE-SYSTEM' }).responses.create({ model: 'synthetic', input: 'synthetic', instructions: 'SYNTHETIC-PRIVATE-SYSTEM' });
    const leakingKeys = Object.entries(attrs()).filter(([k, v]) => String(v).includes('SYNTHETIC-PRIVATE-SYSTEM')).map(([k]) => k);
    assert.deepEqual(leakingKeys, [], `system prompt survived in ${leakingKeys.join(', ')}`);
  } finally { delete process.env.FI_HIDE_INPUTS; }
});
test('concurrent calls preserve parents and transport child context', async () => {
  instrument(); const c = client(fixture, true);
  await Promise.all([1, 2].map(async n => {
    const parent = tracer.startSpan(`parent-${n}`);
    try { await context.with(trace.setSpan(context.active(), parent), () => c.chat.completions.create({ model: 'synthetic', messages: [] })); }
    finally { parent.end(); }
  }));
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 6);
  const requests = spans.filter(s => s.name === 'OpenAI Chat Completions');
  assert.equal(new Set(requests.map(s => s.parentSpanContext.spanId)).size, 2);
  const requestIds = requests.map(s => s.spanContext().spanId);
  for (const child of spans.filter(s => s.name === 'synthetic-transport')) assert.ok(requestIds.includes(child.parentSpanContext.spanId));
  assert.ok(!JSON.stringify(spans.map(s => s.attributes)).includes('synthetic-private-key'));
});
