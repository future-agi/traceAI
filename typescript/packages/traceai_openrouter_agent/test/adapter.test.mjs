import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { ROOT_CONTEXT, trace } from '@opentelemetry/api';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { HooksManager, callModel, tool } from '@openrouter/agent';
import { OpenRouter } from '@openrouter/sdk';
import { HTTPClient } from '@openrouter/sdk/lib/http';
import { z } from 'zod';
import { buildTracerProvider, createTraceHooks, createTraceSession } from '@traceai/openrouter-agent';

// Tests never inherit content flags or credentials. Fake transport never sends a request.
for (const name of ['FI_CAPTURE_CONTENT', 'FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_ERROR_DETAILS']) process.env[name] = 'false';

function hookManager(hooks) {
  const manager = new HooksManager(undefined, { throwOnHandlerError: true });
  for (const [name, entries] of Object.entries(hooks)) for (const entry of entries) manager.on(name, entry);
  return manager;
}

function fixture(t, options = {}) {
  const exporter = new InMemorySpanExporter();
  const provider = buildTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  t.after(() => provider.shutdown());
  const session = createTraceSession(provider, options);
  const manager = hookManager(session.hooks);
  manager.setSessionId('session-offline');
  return { exporter, provider, session, emit: (name, data) => manager.emit(name, data) };
}
const usage = { inputTokens: 10, outputTokens: 4, totalTokens: 14, cachedTokens: 3, reasoningTokens: 2, cost: 0.002 };
const modelEvent = { sessionId: 'session-offline', model: 'synthetic/model', responseId: 'r1', durationMs: 1, turnType: 'final', turnNumber: 1, usage };
const spansOf = (exporter, kind) => exporter.getFinishedSpans().filter(s => s.attributes['fi.span.kind'] === kind);

test('CJS and ESM entry points load without importing ESM-only Agent at runtime', () => {
  const cjs = createRequire(import.meta.url)('@traceai/openrouter-agent');
  assert.equal(typeof cjs.createTraceSession, 'function');
});

test('concurrent same-name tools use completion-local input, including mutations and identical inputs', async t => {
  const { exporter, emit } = fixture(t, { captureContent: true });
  await emit('SessionStart', {});
  await emit('PreToolUse', { toolName: 'lookup', toolInput: { city: 'London' } });
  await emit('PreToolUse', { toolName: 'lookup', toolInput: { city: 'Paris' } });
  for (const city of ['Paris', 'London', 'London', 'changed']) {
    await emit('PostToolUse', { toolName: 'lookup', toolInput: { city }, toolOutput: `${city} result`, durationMs: 2 });
  }
  await emit('SessionEnd', { reason: 'complete' });
  const tools = spansOf(exporter, 'TOOL');
  assert.equal(tools.length, 4);
  for (const span of tools) assert.equal(`${JSON.parse(span.attributes['input.value']).city} result`, span.attributes['output.value']);
  assert.equal(new Set(tools.map(s => s.spanContext().spanId)).size, 4);
  const [root] = spansOf(exporter, 'AGENT');
  for (const span of tools) {
    assert.equal(span.parentSpanContext.spanId, root.spanContext().spanId);
    assert.equal(span.attributes['session.id'], 'session-offline');
  }
});

test('failure input comes from its event; failure duration is explicitly unavailable', async t => {
  const { exporter, emit } = fixture(t, { captureContent: true });
  await emit('PostToolUseFailure', { toolName: 'lookup', toolInput: { city: 'Paris' }, error: new Error('synthetic failure') });
  await emit('SessionEnd', { reason: 'complete' });
  const [span] = spansOf(exporter, 'TOOL');
  assert.equal(span.attributes['input.value'], '{"city":"Paris"}');
  assert.equal(span.attributes['openrouter.agent.duration_unavailable'], true);
  assert.equal(span.status.code, 2);
  assert.equal(span.attributes['error.message'], 'synthetic failure');
  assert.equal(spansOf(exporter, 'AGENT')[0].status.code, 0);
  assert.equal(span.events.length, 0); // never export exception stacks
});

test('session and all usage fields propagate once; parent is captured explicitly', async t => {
  const parentContext = trace.setSpanContext(ROOT_CONTEXT, { traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1 });
  const { exporter, emit } = fixture(t, { parentContext });
  await emit('PostModelCall', modelEvent);
  await emit('SessionEnd', { reason: 'complete', totalUsage: { ...usage, modelCalls: 1 } });
  const [llm] = spansOf(exporter, 'LLM');
  assert.equal(llm.attributes['gen_ai.usage.cache_read_tokens'], 3);
  assert.equal(llm.attributes['gen_ai.usage.output_tokens.reasoning'], 2);
  assert.equal(llm.attributes['gen_ai.cost.total'], .002);
  const [root] = spansOf(exporter, 'AGENT');
  assert.equal(root.attributes['session.id'], 'session-offline');
  assert.equal(root.attributes['gen_ai.usage.total_tokens'], undefined);
  assert.equal(root.parentSpanContext.spanId, 'b'.repeat(16));
  assert.equal(llm.spanContext().traceId, 'a'.repeat(32));
});

test('getText observes consumption without double execution and records output before root export', async t => {
  const { exporter, session, emit } = fixture(t, { captureContent: true });
  let calls = 0;
  const result = { async getText() {
    calls++;
    await emit('SessionStart', {});
    await emit('UserPromptSubmit', { prompt: 'synthetic prompt' });
    await emit('PostModelCall', modelEvent);
    await emit('SessionEnd', { reason: 'complete' });
    assert.equal(spansOf(exporter, 'AGENT').length, 0);
    return 'synthetic answer';
  } };
  assert.equal(await session.getText(result), 'synthetic answer');
  assert.equal(calls, 1);
  assert.equal(spansOf(exporter, 'AGENT')[0].attributes['output.value'], 'synthetic answer');
  await assert.rejects(session.getText(result), /new trace session/);
});

test('stream is lazy, forwards chunks and completes after SessionEnd', async t => {
  const { exporter, session, emit } = fixture(t, { captureContent: true });
  let reads = 0;
  const stream = session.getTextStream({ async *getTextStream() {
    await emit('SessionStart', {});
    for (const value of ['hello', ' world']) { reads++; yield value; }
    await emit('SessionEnd', { reason: 'complete' });
  } });
  assert.equal(reads, 0);
  const values = [];
  for await (const value of stream) values.push(value);
  assert.deepEqual(values, ['hello', ' world']);
  assert.equal(reads, 2);
  assert.equal(spansOf(exporter, 'AGENT')[0].attributes['output.value'], 'hello world');
});

test('abandoned stream closes upstream iterator without draining, marks incomplete, and drops late hooks', async t => {
  const { exporter, session, emit } = fixture(t, { captureContent: true });
  let reads = 0, returned = false;
  for await (const _ of session.getTextStream({ async *getTextStream() {
    try { reads++; yield 'partial'; reads++; yield 'must not consume'; }
    finally { returned = true; await emit('SessionEnd', { reason: 'complete' }); }
  } })) break;
  assert.equal(reads, 1);
  assert.equal(returned, true);
  const [root] = spansOf(exporter, 'AGENT');
  assert.equal(root.status.code, 2);
  assert.equal(root.attributes['output.value'], undefined);
  assert.equal(root.attributes['openrouter.agent.consumption_incomplete'], true);
  await emit('PostModelCall', modelEvent);
  assert.equal(exporter.getFinishedSpans().length, 1);
});

test('errors and aborts preserve thrown identity without exporting messages by default', async t => {
  const { exporter, session } = fixture(t);
  const error = new Error('PRIVATE_ERROR'); error.name = 'PRIVATE_NAME';
  await assert.rejects(session.getText({ async getText() { throw error; } }), e => e === error);
  const [root] = spansOf(exporter, 'AGENT');
  assert.equal(root.status.code, 2);
  assert.equal(root.attributes['error.type'], 'Error');
  assert.doesNotMatch(JSON.stringify({ attributes: root.attributes, status: root.status, events: root.events }), /PRIVATE/);
});

test('explicit close is idempotent and does not invent a span for an unused session', async t => {
  const { exporter, session, emit } = fixture(t);
  await emit('SessionStart', {});
  session.close(); session.close();
  assert.equal(exporter.getFinishedSpans().length, 1);
  assert.equal(exporter.getFinishedSpans()[0].status.code, 2);
  const second = fixture(t); second.session.close();
  assert.equal(second.exporter.getFinishedSpans().length, 0);
});

test('hook-only entry point keeps legacy usage and avoids a second provider', async t => {
  const { exporter, provider } = fixture(t);
  const manager = hookManager(createTraceHooks(provider));
  await manager.emit('SessionStart', {});
  await manager.emit('SessionEnd', { reason: 'complete' });
  assert.equal(exporter.getFinishedSpans().length, 1);
});

test('bounded content handles undefined, BigInt and cycles without breaking callbacks', async t => {
  const { exporter, emit } = fixture(t, { captureContent: true });
  const cyclic = {}; cyclic.self = cyclic;
  for (const output of [undefined, 1n, cyclic, 'x'.repeat(40000)]) {
    await emit('PostToolUse', { toolName: 'lookup', toolInput: {}, toolOutput: output, durationMs: 0 });
  }
  await emit('SessionEnd', { reason: 'complete' });
  const values = spansOf(exporter, 'TOOL').map(s => s.attributes['output.value']);
  assert.deepEqual(values.slice(0, 3), ['null', '[unserializable]', '[unserializable]']);
  assert.equal(values[3].length, 32014);
});

test('hide flags override explicit opt-in', async t => {
  for (const key of ['FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_ERROR_DETAILS']) process.env[key] = 'true';
  const { exporter, emit } = fixture(t, { captureContent: true });
  for (const key of ['FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_ERROR_DETAILS']) process.env[key] = 'false';
  await emit('UserPromptSubmit', { prompt: 'PRIVATE_PROMPT' });
  await emit('PostToolUse', { toolName: 'lookup', toolInput: { data: 'PRIVATE_INPUT' }, toolOutput: 'PRIVATE_OUTPUT', durationMs: 0 });
  await emit('PostToolUseFailure', { toolName: 'lookup', toolInput: {}, error: new Error('PRIVATE_ERROR') });
  await emit('SessionEnd', { reason: 'complete' });
  assert.doesNotMatch(JSON.stringify(exporter.getFinishedSpans().map(s => ({ attributes: s.attributes, status: s.status, events: s.events }))), /PRIVATE/);
});

// The real Agent and SDK run against an in-memory HTTPClient, not a vendor API.
function response(output, id = 'r1') {
  return {
    id, object: 'response', created_at: 1, completed_at: 2, status: 'completed', model: 'synthetic/model',
    output, error: null, incomplete_details: null, instructions: null, metadata: null,
    frequency_penalty: null, presence_penalty: null, parallel_tool_calls: true,
    temperature: null, top_p: null, tools: [], tool_choice: 'auto',
    usage: { input_tokens: 10, output_tokens: 4, total_tokens: 14, input_tokens_details: { cached_tokens: 3 }, output_tokens_details: { reasoning_tokens: 2 }, cost: .002 },
  };
}
const answer = text => ({ id: 'm1', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] });
const toolCall = (id, city) => ({ type: 'function_call', id, call_id: id, name: 'lookup', arguments: JSON.stringify({ city }), status: 'completed' });
function clientFor(responses) {
  let calls = 0;
  const client = new OpenRouter({ apiKey: 'synthetic-not-a-key', retryConfig: { strategy: 'none' }, httpClient: new HTTPClient({
    fetcher: async request => {
      const next = responses[calls++];
      assert.ok(next, 'unexpected SDK request');
      if (typeof next === 'function') return next(request);
      return new Response(JSON.stringify(next), { headers: { 'Content-Type': 'application/json' } });
    },
  }) });
  return { client, calls: () => calls };
}

test('real Agent parallel tools finish out of order without mixing inputs, outputs or usage', async t => {
  const { exporter, session } = fixture(t, { captureContent: true });
  let releaseParis;
  const parisFinished = new Promise(resolve => { releaseParis = resolve; });
  const lookup = tool({ name: 'lookup', inputSchema: z.object({ city: z.string() }), execute: async ({ city }) => {
    if (city === 'London') await parisFinished;
    else releaseParis();
    return `${city} result`;
  } });
  const { client, calls } = clientFor([response([toolCall('c1', 'London'), toolCall('c2', 'Paris')]), response([answer('Done')], 'r2')]);
  const result = callModel(client, { model: 'synthetic/model', input: 'Compare weather', tools: [lookup], hooks: session.hooks });
  assert.equal(await session.getText(result), 'Done');
  assert.equal(calls(), 2);
  const tools = spansOf(exporter, 'TOOL');
  assert.equal(tools.length, 2);
  assert.equal(tools[0].attributes['output.value'], 'Paris result');
  for (const span of tools) assert.equal(`${JSON.parse(span.attributes['input.value']).city} result`, span.attributes['output.value']);
  assert.equal(spansOf(exporter, 'LLM').length, 2);
  assert.equal(spansOf(exporter, 'AGENT').length, 1);
  assert.equal(spansOf(exporter, 'AGENT')[0].attributes['output.value'], 'Done');
});

test('real Agent state resumes in separate traces with the same session identity', async t => {
  const { exporter, provider, session } = fixture(t, { captureContent: true });
  let state = null;
  const accessor = { load: async () => state, save: async next => { state = next; } };
  const { client } = clientFor([response([answer('First')]), response([answer('Second')], 'r2')]);
  await session.getText(callModel(client, { model: 'synthetic/model', input: 'First question', state: accessor, hooks: session.hooks }));
  const next = createTraceSession(provider, { captureContent: true });
  await next.getText(callModel(client, { model: 'synthetic/model', input: 'Follow up', state: accessor, hooks: next.hooks }));
  const roots = spansOf(exporter, 'AGENT');
  assert.equal(roots.length, 2);
  assert.ok(roots[0].attributes['session.id']);
  assert.equal(roots[0].attributes['session.id'], roots[1].attributes['session.id']);
  assert.notEqual(roots[0].spanContext().traceId, roots[1].spanContext().traceId);
});

test('real SDK streaming emits one model span and preserves consumed text', async t => {
  const { exporter, session } = fixture(t, { captureContent: true });
  const completed = response([answer('Hello world')]);
  const events = [
    { type: 'response.created', sequence_number: 0, response: { ...completed, status: 'in_progress', output: [] } },
    { type: 'response.output_text.delta', sequence_number: 1, item_id: 'm1', output_index: 0, content_index: 0, logprobs: [], delta: 'Hello' },
    { type: 'response.output_text.delta', sequence_number: 2, item_id: 'm1', output_index: 0, content_index: 0, logprobs: [], delta: ' world' },
    { type: 'response.completed', sequence_number: 3, response: completed },
  ];
  const { client } = clientFor([() => new Response(events.map(e => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } })]);
  const result = callModel(client, { model: 'synthetic/model', input: 'hello', hooks: session.hooks });
  let text = '';
  for await (const delta of session.getTextStream(result)) text += delta;
  assert.equal(text, 'Hello world');
  assert.equal(spansOf(exporter, 'LLM').length, 1);
  assert.equal(spansOf(exporter, 'AGENT')[0].attributes['output.value'], text);
});

for (const captureContent of [false, true]) test(`OTLP wire preserves endpoint, resource, hierarchy and captureContent=${captureContent}`, async t => {
  let body, path, headers;
  const server = createServer(async (req, res) => {
    path = req.url; headers = req.headers;
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    body = Buffer.concat(chunks).toString();
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const provider = buildTracerProvider({ projectName: 'offline-project', baseUrl: `http://127.0.0.1:${server.address().port}/`, apiKey: 'synthetic-api', secretKey: 'synthetic-secret' });
  t.after(() => provider.shutdown());
  const session = createTraceSession(provider, { captureContent });
  const manager = hookManager(session.hooks); manager.setSessionId('wire-session');
  await session.getText({ async getText() {
    await manager.emit('UserPromptSubmit', { prompt: 'PRIVATE_PROMPT' });
    await manager.emit('PostModelCall', modelEvent);
    await manager.emit('PostToolUse', { toolName: 'lookup', toolInput: { data: 'PRIVATE_INPUT' }, toolOutput: 'PRIVATE_OUTPUT', durationMs: 1 });
    const error = new Error('PRIVATE_ERROR'); error.name = 'PRIVATE_NAME';
    await manager.emit('PostToolUseFailure', { toolName: 'lookup', toolInput: {}, error });
    await manager.emit('SessionEnd', { reason: 'complete' });
    return 'PRIVATE_ANSWER';
  } });
  await provider.forceFlush();
  assert.equal(path, '/tracer/v1/traces');
  assert.equal(headers['x-api-key'], 'synthetic-api');
  assert.equal(headers['x-secret-key'], 'synthetic-secret');
  assert.doesNotMatch(body, /synthetic-api|synthetic-secret|exception.stacktrace|PRIVATE_NAME/);
  if (captureContent) {
    for (const marker of ['PRIVATE_PROMPT', 'PRIVATE_INPUT', 'PRIVATE_OUTPUT', 'PRIVATE_ERROR', 'PRIVATE_ANSWER']) assert.ok(body.includes(marker));
  } else assert.doesNotMatch(body, /PRIVATE/);
  const payload = JSON.parse(body);
  const attrs = items => Object.fromEntries(items.map(a => [a.key, Object.values(a.value)[0]]));
  assert.equal(attrs(payload.resourceSpans[0].resource.attributes).project_name, 'offline-project');
  assert.equal(attrs(payload.resourceSpans[0].resource.attributes).project_type, 'observe');
  const spans = payload.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans));
  assert.equal(spans.length, 4);
  const root = spans.find(s => attrs(s.attributes)['fi.span.kind'] === 'AGENT');
  for (const span of spans.filter(s => s !== root)) {
    assert.equal(span.parentSpanId, root.spanId);
    assert.equal(span.traceId, root.traceId);
    assert.equal(attrs(span.attributes)['session.id'], 'wire-session');
  }
});
