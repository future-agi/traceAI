import assert from 'node:assert/strict';
import { test } from 'node:test';
import { HooksManager, callModel, tool } from '@openrouter/agent';
import { OpenRouter } from '@openrouter/sdk';
import { HTTPClient } from '@openrouter/sdk/lib/http';
import { z } from 'zod';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { buildTracerProvider, createTraceSession } from '@traceai/openrouter-agent';

for (const name of ['FI_CAPTURE_CONTENT', 'FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_ERROR_DETAILS']) process.env[name] = 'false';

function setup(t) {
  const exporter = new InMemorySpanExporter();
  const provider = buildTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  t.after(() => provider.shutdown());
  return { exporter, provider, session: createTraceSession(provider) };
}
const kind = (exporter, name) => exporter.getFinishedSpans().filter(s => s.attributes['fi.span.kind'] === name);

test('real Agent aborted request closes a single root without inventing a completed model span', { timeout: 5000 }, async t => {
  const { exporter, session } = setup(t);
  const abort = new AbortController();
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const client = new OpenRouter({ apiKey: 'synthetic-not-a-key', retryConfig: { strategy: 'none' }, httpClient: new HTTPClient({ fetcher: request => new Promise((resolve, reject) => {
    request.signal.addEventListener('abort', () => reject(request.signal.reason), { once: true });
    started();
  }) }) });
  const result = callModel(client, { model: 'synthetic/model', input: 'PRIVATE_INPUT', signal: abort.signal, hooks: session.hooks });
  const consumed = session.getText(result);
  const rejected = assert.rejects(consumed);
  await ready;
  abort.abort(new DOMException('PRIVATE_ABORT_REASON', 'AbortError'));
  await rejected;
  assert.equal(kind(exporter, 'AGENT').length, 1);
  assert.equal(kind(exporter, 'LLM').length, 0);
  const [root] = kind(exporter, 'AGENT');
  assert.equal(root.status.code, 2);
  assert.doesNotMatch(JSON.stringify({ attrs: root.attributes, status: root.status, events: root.events }), /PRIVATE/);
});

test('approval pause and resume close roots even when upstream skips session boundary hooks', async t => {
  const { exporter, provider, session } = setup(t);
  let state = null, executions = 0;
  const store = { load: async () => state, save: async next => { state = next; } };
  const lookup = tool({ name: 'lookup', requireApproval: true, inputSchema: z.object({ city: z.string() }), execute: ({ city }) => { executions++; return `${city} result`; } });
  const outputs = [
    [{ id: 'c1', call_id: 'c1', type: 'function_call', name: 'lookup', arguments: '{"city":"Paris"}', status: 'completed' }],
    [{ id: 'm1', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: 'Done', annotations: [] }] }],
  ];
  let calls = 0;
  const client = new OpenRouter({ apiKey: 'synthetic-not-a-key', retryConfig: { strategy: 'none' }, httpClient: new HTTPClient({ fetcher: async () => {
    assert.ok(outputs[calls], 'unexpected SDK request');
    return Response.json({
      id: `r${calls}`, object: 'response', created_at: 1, completed_at: 2, status: 'completed', model: 'synthetic/model', output: outputs[calls++],
      error: null, incomplete_details: null, instructions: null, metadata: null, frequency_penalty: null, presence_penalty: null,
      parallel_tool_calls: true, temperature: null, top_p: null, tools: [], tool_choice: 'auto',
    });
  } }) });
  await session.getText(callModel(client, { model: 'synthetic/model', input: 'Weather', tools: [lookup], state: store, hooks: session.hooks }));
  assert.equal(state.status, 'awaiting_approval');
  assert.equal(executions, 0);
  assert.equal(kind(exporter, 'TOOL').length, 0);
  const next = createTraceSession(provider, { captureContent: true });
  const text = await next.getText(callModel(client, { model: 'synthetic/model', tools: [lookup], state: store, approveToolCalls: ['c1'], hooks: next.hooks }));
  assert.equal(text, 'Done');
  assert.equal(executions, 1);
  assert.equal(calls, 2);
  const roots = kind(exporter, 'AGENT');
  assert.equal(roots.length, 2);
  assert.equal(roots[0].attributes['session.id'], roots[1].attributes['session.id']);
  const [span] = kind(exporter, 'TOOL');
  assert.equal(span.attributes['input.value'], '{"city":"Paris"}');
  assert.equal(span.parentSpanContext.spanId, roots[1].spanContext().spanId);
});

test('two concurrently consumed runs do not share session or parent state', async t => {
  const { exporter, provider } = setup(t);
  async function run(id) {
    const session = createTraceSession(provider, { captureContent: true });
    const manager = new HooksManager();
    for (const [name, entries] of Object.entries(session.hooks)) for (const entry of entries) manager.on(name, entry);
    manager.setSessionId(id);
    return session.getText({ async getText() {
      await manager.emit('SessionStart', {});
      await manager.emit('PostToolUse', { toolName: 'lookup', toolInput: { id }, toolOutput: id, durationMs: 1 });
      await manager.emit('SessionEnd', { reason: 'complete' });
      return id;
    } });
  }
  await Promise.all([run('run-a'), run('run-b')]);
  const roots = kind(exporter, 'AGENT');
  assert.equal(roots.length, 2);
  for (const child of kind(exporter, 'TOOL')) {
    const root = roots.find(s => s.spanContext().spanId === child.parentSpanContext.spanId);
    assert.equal(root.attributes['session.id'], child.attributes['session.id']);
    assert.equal(root.attributes['output.value'], child.attributes['output.value']);
  }
});

test('content opt-in never serializes a thrown object stack', async t => {
  const { exporter, provider } = setup(t);
  const session = createTraceSession(provider, { captureContent: true });
  const error = { message: 'error', stack: 'PRIVATE_STACK' };
  await assert.rejects(session.getText({ async getText() { throw error; } }), e => e === error);
  const [root] = kind(exporter, 'AGENT');
  assert.equal(root.attributes['error.message'], '[non-Error thrown]');
  assert.doesNotMatch(JSON.stringify({ attrs: root.attributes, events: root.events, status: root.status }), /PRIVATE_STACK/);
});


test('closing during consumption never labels later text as a completed answer', async t => {
  const { exporter, provider } = setup(t);
  const session = createTraceSession(provider, { captureContent: true });
  const consumed = session.getText({ async getText() { session.close(); return 'not a completed answer'; } });
  assert.equal(await consumed, 'not a completed answer');
  const [root] = kind(exporter, 'AGENT');
  assert.equal(root.status.code, 2);
  assert.equal(root.attributes['output.value'], undefined);
});
