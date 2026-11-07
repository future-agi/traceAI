// Packed-consumer contract. All HTTP traffic goes to the loopback OTLP receiver;
// provider requests use the real OpenAI SDK's fetch seam with synthetic data.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const consumer = resolve(process.argv[2]);
const require = createRequire(resolve(consumer, 'package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const ai = await load('ai');
const { createOpenAI } = await load('@ai-sdk/openai');
const { BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { Resource } = require('@opentelemetry/resources');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const cjs = require('@traceai/vercel');
const esmPath = require.resolve('@traceai/vercel').replace('/dist/src/index.js', '/dist/esm/index.js');
const esm = await import(pathToFileURL(esmPath).href);
assert.equal(typeof esm.FISimpleSpanProcessor, 'function', 'packed ESM entry point');
const major = Number(require('ai/package.json').version.split('.')[0]);
const packets = [];
const server = createServer((req, res) => {
  const chunks = [];
  req.on('data', chunk => chunks.push(chunk));
  req.on('end', () => {
    packets.push({ path: req.url, headers: req.headers, data: JSON.parse(Buffer.concat(chunks).toString()) });
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const contextManager = new AsyncLocalStorageContextManager().enable();
const provider = new BasicTracerProvider({ resource: new Resource({
  'service.name': 'vercel-contract', project_name: 'vercel-contract', project_type: 'observe',
}) });
provider.register({ contextManager });
const Processor = process.argv[3] === 'batch' ? esm.FIBatchSpanProcessor : cjs.FISimpleSpanProcessor;
provider.addSpanProcessor(new Processor({
  exporter: new OTLPTraceExporter({
    url: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    headers: { 'X-Api-Key': 'synthetic-api', 'X-Secret-Key': 'synthetic-secret' },
  }),
  spanFilter: cjs.isFISpan,
}));
const tracer = provider.getTracer('vercel-contract');
if (major >= 7) {
  const { OpenTelemetry } = await load('@ai-sdk/otel');
  ai.registerTelemetry(new OpenTelemetry({ tracer }));
}
const telemetry = (record = true) => major >= 7
  ? { telemetry: { recordInputs: record, recordOutputs: record } }
  : { experimental_telemetry: { isEnabled: true, recordInputs: record, recordOutputs: record, tracer } };
const attrs = list => Object.fromEntries(list.map(({ key, value }) => [key,
  value.stringValue ?? (value.intValue !== undefined ? Number(value.intValue) : undefined) ?? value.boolValue ?? value.doubleValue ?? value.arrayValue?.values.map(v => v.stringValue),
]));
async function flush() {
  await provider.forceFlush();
  const spans = packets.splice(0).flatMap(packet => {
    assert.equal(packet.path, '/tracer/v1/traces');
    assert.equal(packet.headers['x-api-key'], 'synthetic-api');
    assert.equal(packet.headers['x-secret-key'], 'synthetic-secret');
    return packet.data.resourceSpans.flatMap(resource => {
      assert.equal(attrs(resource.resource.attributes).project_name, 'vercel-contract');
      assert.equal(attrs(resource.resource.attributes).project_type, 'observe');
      return resource.scopeSpans.flatMap(scope => scope.spans.map(span => ({ ...span, attrs: attrs(span.attributes) })));
    });
  });
  assert.equal(new Set(spans.map(span => span.spanId)).size, spans.length, 'no duplicate exports');
  for (const span of spans) {
    assert.equal(typeof span.attrs['fi.span.kind'], 'string');
    if (span.parentSpanId) assert.ok(spans.some(parent => parent.spanId === span.parentSpanId), `missing parent for ${span.name}`);
  }
  return spans;
}
const completion = (message, finish = 'stop') => ({
  id: 'completion-test', object: 'chat.completion', created: 1, model: 'test-model',
  choices: [{ index: 0, message: { role: 'assistant', ...message }, finish_reason: finish }],
  usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 },
});
function model(responses) {
  let count = 0;
  return createOpenAI({ apiKey: 'synthetic-openai', baseURL: 'https://fixture.invalid/v1',
    fetch: async (url, init) => {
      assert.equal(String(url), 'https://fixture.invalid/v1/chat/completions');
      const request = JSON.parse(init.body);
      const response = responses[Math.min(count++, responses.length - 1)];
      if (response instanceof Response) return response;
      if (request.stream) {
        const chunks = [
          { choices: [{ index: 0, delta: { role: 'assistant', content: 'hello-stream' }, finish_reason: null }] },
          { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 } },
        ].map(chunk => `data: ${JSON.stringify({ id: 'stream-test', object: 'chat.completion.chunk', created: 1, model: 'test-model', ...chunk })}\n\n`).join('') + 'data: [DONE]\n\n';
        return new Response(chunks, { headers: { 'content-type': 'text/event-stream' } });
      }
      return Response.json(response);
    },
  }).chat('test-model');
}
function checkLLM(spans, count = 1) {
  const calls = spans.filter(span => span.attrs['fi.span.kind'] === 'LLM');
  assert.equal(calls.length, count, 'one LLM span per actual provider invocation');
  for (const call of calls) {
    assert.equal(call.attrs['gen_ai.request.model'], 'test-model');
    assert.equal(call.attrs['gen_ai.usage.input_tokens'], 7);
    assert.equal(call.attrs['gen_ai.usage.output_tokens'], 3);
  }
  return calls;
}

try {
  tracer.startSpan('unrelated-http').end();
  assert.equal((await flush()).length, 0, 'unrelated spans filtered');
  const generated = await ai.generateText({ model: model([completion({ content: 'hello' })]), prompt: 'synthetic-prompt', ...telemetry() });
  assert.equal(generated.text, 'hello');
  let spans = await flush();
  checkLLM(spans);
  assert.equal(spans.length, major >= 7 ? 3 : 2, 'native hierarchy, no wrapper duplicates');

  const stream = ai.streamText({ model: model([{}]), prompt: 'synthetic-stream-prompt', ...telemetry() });
  // SDK 6 resolves .text before its async stream finalizer ends the root span.
  // Drain the stream before flushing the batch processor.
  await stream.consumeStream();
  assert.equal(await stream.text, 'hello-stream');
  checkLLM(await flush());

  const embeddingModel = createOpenAI({ apiKey: 'synthetic-openai', baseURL: 'https://fixture.invalid/v1',
    fetch: async url => {
      assert.equal(String(url), 'https://fixture.invalid/v1/embeddings');
      return Response.json({ object: 'list', data: [{ object: 'embedding', index: 0, embedding: [0.1, 0.2] }],
        model: 'text-embedding-3-small', usage: { prompt_tokens: 7, total_tokens: 7 } });
    },
  }).embedding('text-embedding-3-small');
  const embedded = await ai.embed({ model: embeddingModel, value: 'synthetic-embedding', ...telemetry() });
  assert.deepEqual(embedded.embedding, [0.1, 0.2]);
  spans = await flush();
  assert.equal(spans.length, 2, 'native embedding hierarchy');
  assert.equal(spans.filter(span => span.attrs['fi.span.kind'] === 'EMBEDDING').length, major >= 7 ? 2 : 1);

  const tools = Object.fromEntries(['one', 'two'].map(name => [name, ai.tool({
    description: name, inputSchema: ai.jsonSchema({ type: 'object', properties: { city: { type: 'string' } }, required: ['city'], additionalProperties: false }),
    execute: async () => name === 'one' ? 0 : false,
  })]));
  const toolResponse = completion({ content: null, tool_calls: ['one', 'two'].map(name => ({
    id: `call-${name}`, type: 'function', function: { name, arguments: '{"city":"Paris"}' },
  })) }, 'tool_calls');
  await ai.generateText({ model: model([toolResponse, completion({ content: 'tool-finished' })]), tools,
    stopWhen: ai.stepCountIs(2), prompt: 'synthetic-tools', ...telemetry() });
  spans = await flush();
  const calls = checkLLM(spans, 2);
  assert.equal(spans.filter(span => span.attrs['fi.span.kind'] === 'TOOL').length, 2);
  const outputs = calls.map(call => call.attrs['gen_ai.output.messages']).filter(Boolean).join('');
  assert.ok(outputs.includes('call-one') && outputs.includes('call-two'), 'both output tool call IDs');
  assert.ok(outputs.includes('Paris'), 'output tool arguments');
  const inputs = calls.map(call => call.attrs['gen_ai.input.messages']).filter(Boolean).join('');
  assert.ok(inputs.includes('call-one') && inputs.includes('call-two'), 'both tool results in conversation history');
  if (major < 7) {
    const history = calls.map(call => JSON.parse(call.attrs['gen_ai.input.messages'] ?? '[]')).flat();
    assert.ok(history.some(message => message.role === 'tool' && message.content === '0'));
    assert.ok(history.some(message => message.role === 'tool' && message.content === 'false'));
  }

  await ai.generateText({ model: model([completion({ content: 'private-output-marker' })]), prompt: 'private-input-marker', ...telemetry(false) });
  spans = await flush();
  checkLLM(spans);
  assert.ok(!JSON.stringify(spans).includes('private-input-marker'));
  assert.ok(!JSON.stringify(spans).includes('private-output-marker'));

  await assert.rejects(ai.generateText({
    model: model([Response.json({ error: { message: 'synthetic upstream failure', type: 'invalid_request_error' } }, { status: 400 })]),
    prompt: 'synthetic-error', maxRetries: 0, ...telemetry(false),
  }));
  spans = await flush();
  assert.ok(spans.some(span => span.status?.code === 2), 'native errors reach OTLP');
  assert.equal(spans.filter(span => span.attrs['fi.span.kind'] === 'LLM').length, 1);
  console.log(JSON.stringify({ sdk: require('ai/package.json').version, processor: Processor.name,
    passed: ['packed CJS/ESM', 'OTLP resource/auth/path', 'native hierarchy', 'generate', 'stream', 'embedding', 'two tools and results', 'privacy switches', 'errors', 'no duplicate spans'] }));
} finally {
  await provider.shutdown();
  contextManager.disable();
  await new Promise(resolve => server.close(resolve));
}
