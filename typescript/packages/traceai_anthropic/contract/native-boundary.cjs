'use strict';
// Real SDKs, synthetic fetch, local OTLP receiver. No credentials or provider calls.
// Usage: node native-boundary.cjs <dependency-dir> <sdk-package> <instrumentation.js>
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const deps = path.resolve(process.argv[2]);
const sdkPackage = process.argv[3];
process.env.NODE_PATH = deps + '/node_modules';
require('node:module').Module._initPaths();
const req = createRequire(deps + '/package.json');
const { trace, context, SpanStatusCode, diag } = req('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = req('@opentelemetry/context-async-hooks');
const { OTLPTraceExporter } = req('@opentelemetry/exporter-trace-otlp-http');
const Anthropic = req(sdkPackage).default;
const sdkVersion = JSON.parse(require('node:fs').readFileSync(path.join(path.dirname(req.resolve(sdkPackage)), 'package.json'), 'utf8')).version;
const native = 'openTelemetry' in Anthropic.prototype;
const warnings = [];
diag.setLogger({ warn: message => warnings.push(message), error() {}, info() {}, debug() {}, verbose() {} });
const params = { model: 'synthetic-model', max_tokens: 32,
  messages: [{ role: 'user', content: 'PRIVATE_INPUT_CANARY' }] };
const message = { id: 'msg_synthetic', type: 'message', role: 'assistant', model: params.model,
  content: [{ type: 'text', text: 'PRIVATE_OUTPUT_CANARY' }], stop_reason: 'end_turn', stop_sequence: null,
  usage: { input_tokens: 7, output_tokens: 3 } };
const events = [
  { type: 'message_start', message: { ...message, content: [], stop_reason: null,
    usage: { input_tokens: 7, output_tokens: 0 } } },
  { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
  { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'PRIVATE_OUTPUT_CANARY' } },
  { type: 'content_block_stop', index: 0 },
  { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 3 } },
  { type: 'message_stop' },
];
let requests = 0;
const fetch = async (_url, init) => {
  requests++;
  const body = JSON.parse(init.body);
  if (body.model === 'synthetic-error') {
    return new Response(JSON.stringify({ type: 'error', error: { type: 'invalid_request_error', message: 'SYNTHETIC_ERROR_DETAIL' } }),
      { status: 400, headers: { 'content-type': 'application/json', 'request-id': 'req_error' } });
  }
  return new Response(body.stream
    ? events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('')
    : JSON.stringify(message), { status: 200, headers: {
      'content-type': body.stream ? 'text/event-stream' : 'application/json', 'request-id': 'req_synthetic',
    } });
};
async function main() {
  const wire = [];
  const receiver = http.createServer((request, response) => {
    assert.equal(request.url, '/v1/traces');
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => { wire.push(JSON.parse(Buffer.concat(chunks))); response.end('{}'); });
  });
  await new Promise(resolve => receiver.listen(0, '127.0.0.1', resolve));
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${receiver.address().port}/v1/traces` }))] });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  trace.setGlobalTracerProvider(provider);
  const { AnthropicInstrumentation, isPatched } = require(path.resolve(process.argv[4]));
  const instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  const original = Anthropic.Messages.prototype.create;
  const cases = [];
  try {
    instrumentation.manuallyInstrument(Anthropic);
    if (native) {
      assert.equal(Anthropic.Messages.prototype.create, original, 'native SDK must not be monkeypatched');
      assert.equal(isPatched(), false);
      assert.equal(Anthropic.fiPatched, undefined);
      assert.ok(warnings.some(w => w.includes('traceConfig') && w.includes('native')));
      class DerivedAnthropic extends Anthropic {}
      instrumentation.manuallyInstrument(DerivedAnthropic);
      assert.equal(Anthropic.Messages.prototype.create, original, 'inherited native capability must also be respected');
    } else {
      assert.notEqual(Anthropic.Messages.prototype.create, original);
      assert.equal(isPatched(), true);
    }
    const client = new Anthropic({ apiKey: 'not-a-real-key', baseURL: 'https://offline.invalid', maxRetries: 0,
      fetch, ...(native ? { openTelemetry: { tracerProvider: provider, traces: { contentMode: 'metadata_only' } } } : {}) });
    async function run(label, operation, count = 1, status = SpanStatusCode.OK) {
      memory.reset();
      wire.length = 0;
      await operation();
      await provider.forceFlush();
      const spans = memory.getFinishedSpans();
      assert.equal(spans.length, count, label);
      const wireSpans = wire.flatMap(r => r.resourceSpans).flatMap(r => r.scopeSpans).flatMap(s => s.spans);
      assert.equal(wireSpans.length, count, label + ' on OTLP wire');
      if (count) assert.equal(spans[0].status.code, status, label + ' status');
      if (native && status === SpanStatusCode.OK) {
        const serialized = JSON.stringify(wire);
        assert.ok(!serialized.includes('PRIVATE_INPUT_CANARY'), label + ': metadata-only input');
        assert.ok(!serialized.includes('PRIVATE_OUTPUT_CANARY'), label + ': metadata-only output');
      }
      cases.push({ label, spans: count, wireSpans: wireSpans.length, ...(count ? { status: spans[0].status.code } : {}) });
      return spans;
    }
    const spans = await run('await', async () => assert.equal((await client.messages.create(params)).id, message.id));
    assert.equal(spans[0].attributes['gen_ai.usage.input_tokens'], 7);
    assert.equal(spans[0].attributes['gen_ai.usage.output_tokens'], 3);
    if (!native) {
      // Keep the existing supported legacy entry point. No claim of native parity here.
      assert.equal(spans[0].attributes['fi.span.kind'], 'LLM');
    } else {
      await run('withResponse', async () => {
        const promise = client.messages.create(params);
        assert.equal(typeof promise.withResponse, 'function');
        const { data, response, request_id } = await promise.withResponse();
        assert.equal(data.id, message.id);
        assert.equal(response.status, 200);
        assert.equal(request_id, 'req_synthetic');
      });
      await run('asResponse retains unread body', async () => {
        const raw = await client.messages.create(params).asResponse();
        assert.equal(raw.bodyUsed, false);
        assert.equal((await raw.json()).id, message.id);
      });
      await run('stream withResponse', async () => {
        const { data: stream } = await client.messages.create({ ...params, stream: true }).withResponse();
        assert.equal(typeof stream.tee, 'function');
        assert.equal(memory.getFinishedSpans().length, 0, 'stream must not be drained in the background');
        const received = [];
        for await (const event of stream) received.push(event);
        assert.deepEqual(received, events);
      });
      await run('stream helper', async () => {
        const result = await client.messages.stream(params).finalMessage();
        assert.equal(result.content[0].text, 'PRIVATE_OUTPUT_CANARY');
      });
      await run('stream cancellation', async () => {
        const stream = await client.messages.create({ ...params, stream: true });
        for await (const event of stream) { assert.equal(event.type, 'message_start'); break; }
        assert.equal(stream.controller.signal.aborted, true);
      }, 1, SpanStatusCode.UNSET);
      await run('API error remains typed', async () => {
        await assert.rejects(client.messages.create({ ...params, model: 'synthetic-error' }), Anthropic.BadRequestError);
      }, 1, SpanStatusCode.ERROR);
      const errorWireContainsDetail = JSON.stringify(wire).includes('SYNTHETIC_ERROR_DETAIL');
      cases[cases.length - 1].errorWireContainsDetail = errorWireContainsDetail;
      await run('native disabled remains disabled', async () => {
        const quiet = new Anthropic({ apiKey: 'not-a-real-key', baseURL: 'https://offline.invalid', maxRetries: 0, fetch, openTelemetry: false });
        const result = await quiet.messages.create(params).withResponse();
        assert.equal(result.data.id, message.id);
      }, 0);
      instrumentation.disable();
      assert.equal(Anthropic.Messages.prototype.create, original);
      await run('disable does not alter native SDK', async () => { await client.messages.create(params); });
    }
    console.log(JSON.stringify({ status: 'PASS', sdkVersion, native, syntheticRequests: requests, providerCalls: 0,
      cases, limits: 'OTLP wire only; no Future AGI backend/UI qualification. Native FI privacy settings are not applied.' }, null, 2));
  } finally {
    instrumentation.disable();
    await provider.shutdown();
    manager.disable();
    await new Promise(resolve => receiver.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
