/* Real @cursor/sdk Agent/Run lifecycle and usage, with only its HTTP/SSE
 * backend replaced by loopback. No provider account or local agent runtime.
 * Run against the peer floor and current SDK as well as the pinned version. */
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { setTimeout: delay } = require('node:timers/promises');
const { test } = require('node:test');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { CursorSDKInstrumentation } = require('../dist/src/index.js');

// Install before loading the SDK: an accidental real request must fail closed.
const nativeFetch = global.fetch;
global.fetch = (input, ...args) => {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  assert.equal(url.hostname, '127.0.0.1', 'real SDK contracts must stay on loopback');
  return nativeFetch(input, ...args);
};
const SDK = require('@cursor/sdk');
const marker = 'private-real-sdk-contract-content';
const apiKey = 'synthetic_cursor_key_not_real';
const usage = { inputTokens: 10, outputTokens: 5, cacheReadTokens: 3, cacheWriteTokens: 2, totalTokens: 20 };

async function setup(t, initialStatus = 'RUNNING', options = {}) {
  const requests = [];
  const runs = new Map();
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const text = Buffer.concat(chunks).toString();
      const body = text ? JSON.parse(text) : {};
      const path = new URL(req.url, 'http://127.0.0.1').pathname;
      requests.push(`${req.method} ${path}`);
      const json = (value, status = 200) => {
        res.writeHead(status, { 'content-type': 'application/json' });
        res.end(JSON.stringify(value));
      };
      if (path === '/v1/me') return json({ apiKeyName: 'synthetic', createdAt: new Date().toISOString() });
      if (path === '/v1/models') return json({ items: [{ id: 'auto', name: 'Auto', params: [] }] });
      if (req.method === 'POST' && (path === '/v1/agents' || path.endsWith('/runs'))) {
        const agentId = body.agentId ?? path.split('/')[3] ?? 'bc-contract';
        const run = {
          id: `run-${runs.size + 1}`, agentId, status: initialStatus,
          createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
        };
        runs.set(run.id, run);
        return json(path === '/v1/agents' ? {
          agent: { id: agentId, status: 'ACTIVE', latestRunId: run.id, url: 'http://127.0.0.1/agent', createdAt: run.createdAt, updatedAt: run.updatedAt },
          run,
        } : { run });
      }
      if (path.endsWith('/stream')) {
        res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
        const emit = (type, value) => res.write(`event: ${type}\ndata: ${JSON.stringify(value)}\n\n`);
        const name = options.taskName ?? 'read';
        emit('tool_call', { callId: 'tool-1', name, status: 'running', args: { path: marker } });
        const toolResult = options.toolResult ?? marker;
        emit('tool_call', { callId: 'tool-1', name, status: 'completed', result: toolResult });
        emit('assistant', { text: marker });
        emit('interaction_update', { type: 'turn-ended', usage });
        emit('result', { status: 'FINISHED', result: marker, durationMs: 12 });
        return res.end();
      }
      if (req.method === 'GET' && /\/runs\/[^/]+$/.test(path)) {
        return json({ ...runs.get(path.split('/')[5]), status: 'FINISHED', result: marker });
      }
      if (path.endsWith('/cancel')) return json({ id: path.split('/')[5] });
      return json({ error: 'unexpected mocked endpoint' }, 404);
    } catch (error) {
      res.writeHead(500);
      res.end(String(error));
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const previousBackend = process.env.CURSOR_BACKEND_URL;
  process.env.CURSOR_BACKEND_URL = `http://127.0.0.1:${server.address().port}`;
  const exporter = new InMemorySpanExporter();
  let started = 0, ended = 0;
  const provider = new BasicTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    { onStart() { started++; }, onEnd() { ended++; }, async forceFlush() {}, async shutdown() {} },
  ] });
  const instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider, runEndGraceMs: 10, traceConfig: options.traceConfig });
  instrumentation.manuallyInstrument(SDK);
  let agent;
  t.after(async () => {
    // Cleanup runs after the assertions, never masking a span that failed to end.
    try { await agent?.[Symbol.asyncDispose]?.(); }
    finally {
      instrumentation.unpatch();
      instrumentation.disable();
      await provider.shutdown();
      if (previousBackend === undefined) delete process.env.CURSOR_BACKEND_URL;
      else process.env.CURSOR_BACKEND_URL = previousBackend;
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
  });
  agent = await SDK.Agent.create({ apiKey, model: { id: 'auto' }, cloud: { env: { type: 'cloud' } } });
  return { agent, exporter, counts: () => ({ started, ended }), requests };
}

test('already-terminal real SDK run closes without stream, wait or flush', { timeout: 10000 }, async (t) => {
  const { agent, exporter, counts } = await setup(t, 'FINISHED');
  const run = await agent.send(marker);
  assert.equal(run.status, 'finished');
  assert.equal(exporter.getFinishedSpans().length, 0, 'allow a grace period for buffered events');
  await delay(100);
  assert.deepEqual(counts(), { started: 1, ended: 1 }, 'terminal run must end automatically');
  const [root] = exporter.getFinishedSpans();
  assert.equal(root.name, 'Cursor Agent run');
  assert.equal(root.attributes['cursor.run.status'], 'finished');
  assert.equal(root.status.code, 1);
});

for (const consumption of ['stream', 'wait']) {
  test(`real SDK ${consumption}: cache-inclusive normalized total, preserved raw usage`, { timeout: 10000 }, async (t) => {
    const { agent, exporter, counts, requests } = await setup(t);
    const callbacks = [];
    const run = await agent.send(marker, { onDelta({ update }) { callbacks.push(update.type); } });
    if (consumption === 'stream') {
      for await (const event of run.stream()) assert.ok(event.type);
    }
    const result = await run.wait();
    assert.equal(result.status, 'finished');
    const spans = exporter.getFinishedSpans();
    const root = spans.find((span) => span.name === 'Cursor Agent run');
    assert.ok(root);
    t.diagnostic(`SDK usage: ${JSON.stringify(result.usage)}`);
    assert.equal(root.attributes['gen_ai.usage.input_tokens'], 15);
    assert.equal(root.attributes['gen_ai.usage.output_tokens'], 5);
    assert.equal(root.attributes['gen_ai.usage.total_tokens'], 20);
    assert.equal(root.attributes['cursor.usage.total_tokens'], result.usage.totalTokens);
    assert.equal(root.attributes['cursor.usage.input_tokens'], 10);
    assert.equal(root.attributes['gen_ai.provider.name'], 'cursor');
    assert.equal(root.attributes['gen_ai.request.model'], 'auto');
    // Cloud callback dispatch differs by SDK version. Capture what the real
    // client exposes, without claiming local-runtime tools were exercised here.
    assert.equal(spans.length, consumption === 'stream' ? 3 : callbacks.includes('text-delta') ? 2 : 1);
    if (callbacks.includes('text-delta')) assert.equal(spans.filter(span => span.name === 'Cursor assistant message').length, 1);
    for (const child of spans.filter((span) => span !== root)) {
      assert.equal(child.parentSpanContext.spanId, root.spanContext().spanId);
    }
    assert.equal(counts().started, counts().ended);
    const exported = JSON.stringify(spans.map(({ attributes, events, status }) => ({ attributes, events, status })));
    assert.ok(!exported.includes(marker));
    assert.ok(!exported.includes(apiKey));
    assert.ok(requests.some((request) => request.endsWith('/stream')), 'exercise the real SDK SSE parser');
  });
}

for (const taskName of ['task', 'Task', 'Agent']) {
  test(`real SDK transport reconstructs ${taskName} subagent tree`, { timeout: 10000 }, async t => {
    const inner = { type: 'toolCall', message: { type: 'read', callId: 'inner-read', args: { path: marker }, result: { status: 'success', value: marker } } };
    const toolResult = { status: 'success', value: { conversationSteps: [
      inner, inner, { type: 'assistantMessage', message: { text: marker } },
    ] } };
    const { agent, exporter, counts } = await setup(t, 'RUNNING', { taskName, toolResult });
    const run = await agent.send(marker);
    const events = [];
    for await (const event of run.stream()) events.push(event);
    await run.wait();
    assert.deepEqual(events.find(event => event.type === 'tool_call' && event.status === 'completed').result, toolResult);
    const spans = exporter.getFinishedSpans();
    const root = spans.find(span => span.name === 'Cursor Agent run');
    const task = spans.find(span => span.attributes['gen_ai.tool.call.id'] === 'tool-1');
    const subagent = spans.find(span => span.name === 'Cursor subagent');
    const reads = spans.filter(span => span.attributes['gen_ai.tool.call.id'] === 'inner-read');
    assert.equal(reads.length, 1, 'nested tool survives real SDK transport exactly once');
    assert.equal(subagent.attributes['fi.span.kind'], 'AGENT');
    assert.equal(subagent.parentSpanContext.spanId, task.spanContext().spanId);
    assert.equal(reads[0].parentSpanContext.spanId, subagent.spanContext().spanId);
    assert.equal(task.parentSpanContext.spanId, root.spanContext().spanId);
    const ms = time => time[0] * 1000 + time[1] / 1e6;
    for (const span of spans) {
      assert.equal(span.spanContext().traceId, root.spanContext().traceId);
      assert.equal(span.instrumentationScope.name, '@traceai/cursor-sdk');
      assert.ok(ms(span.startTime) >= ms(root.startTime));
      assert.ok(ms(span.endTime) <= ms(root.endTime));
    }
    const exported = JSON.stringify(spans.map(({ name, attributes, events, status }) => ({ name, attributes, events, status })));
    assert.ok(!exported.includes(marker));
    assert.equal(counts().started, counts().ended);
  });
}
