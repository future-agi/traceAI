import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createTurnTracer, resolveConfig } from '@traceai/agent-core';
import { observe } from '../dist/observer.js';

const attrs = span => Object.fromEntries(span.attributes.map(a => [a.key, a.value.stringValue ?? a.value.intValue ?? a.value.doubleValue ?? a.value.boolValue]));
async function fixture(t, env = {}) {
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    requests.push(JSON.parse(body)); res.setHeader('content-type', 'application/json'); res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const home = await mkdtemp(join(tmpdir(), 'opencode-unified-'));
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await rm(home, { recursive: true, force: true }); });
  const config = await resolveConfig({ agent: 'opencode-v1', home, env: {
    FI_AGENT_ENABLED: 'true', FI_API_KEY: 'synthetic-public', FI_SECRET_KEY: 'synthetic-secret-boundary',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/traces`, ...env,
  } });
  const tracer = createTurnTracer({ config });
  const hooks = observe(config, tracer);
  t.after(() => hooks.dispose());
  const event = (type, properties) => hooks.event({ event: { type, properties } });
  const user = text => hooks['chat.message']({}, { message: { id: 'prompt', sessionID: 'session', time: { created: Date.now() } }, parts: [{ type: 'text', text }] });
  const message = (id, error) => event('message.updated', { info: { id, parentID: 'prompt', sessionID: 'session', role: 'assistant', modelID: 'test', providerID: 'mock', time: { created: Date.now() - 10, completed: Date.now() }, tokens: { input: 10, output: 4, cache: { read: 2, write: 1 }, reasoning: 1 }, cost: 0.002, ...(error ? { error: { name: error } } : {}) } });
  const part = (messageID, fields) => event('message.part.updated', { part: { messageID, sessionID: 'session', ...fields } });
  return { hooks, event, user, message, part, async finish() {
    await event('session.idle', { sessionID: 'session' }); await hooks.dispose();
    return requests.flatMap(r => r.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(r => r.spans ?? []);
  } };
}

test('unified core exports canonical cost, tool IDs and per-request usage exactly once', async t => {
  const f = await fixture(t); await f.user('Read sample');
  for (const id of ['first', 'last']) {
    await f.message(id); await f.part(id, { type: 'step-start' });
    if (id === 'first') await f.part(id, { type: 'tool', tool: 'read', callID: 'call-source', state: { status: 'completed', input: { file: 'sample' }, output: 'contents', time: { start: Date.now() - 5, end: Date.now() } } });
    else await f.part(id, { type: 'text', id: 'answer', text: 'done' });
  }
  const spans = await f.finish();
  const root = spans.find(s => !s.parentSpanId), models = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
  assert.equal(root.status.code, 1); assert.equal(models.length, 2);
  assert.equal(attrs(root)['gen_ai.usage.total_tokens'], undefined);
  for (const model of models) {
    const a = attrs(model);
    assert.equal(Number(a['gen_ai.usage.input_tokens']), 13); assert.equal(Number(a['gen_ai.usage.output_tokens']), 5);
    assert.equal(Number(a['gen_ai.cost.total']), 0.002); assert.equal(a['gen_ai.usage.cost'], undefined);
    assert.equal(a['fi.usage.contribution'], 'additive'); assert.equal(a['fi.agent.cost.source'], 'host-estimate');
    assert.equal(a['gen_ai.server.time_to_first_token'], undefined, 'never invent TTFT from message timestamps');
    assert.ok(a['input.value']); assert.ok(a['output.value']);
  }
  const tool = spans.find(s => attrs(s)['fi.span.kind'] === 'TOOL');
  assert.equal(attrs(tool)['tool.name'], 'read'); assert.equal(attrs(tool)['tool_call.id'], 'call-source');
});

test('a later successful generation recovers a prior fatal attempt without erasing it', async t => {
  const f = await fixture(t); await f.user('retry');
  await f.event('session.error', { sessionID: 'session', error: { name: 'APIError' } });
  await f.message('failed', 'APIError');
  await f.message('recovered'); await f.part('recovered', { type: 'step-start' });
  const spans = await f.finish();
  assert.equal(spans.find(s => !s.parentSpanId).status.code, 1);
  const models = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
  assert.equal(models.length, 2); assert.equal(models.filter(s => s.status.code === 2).length, 1);
});

test('replayed pre-error success cannot recover a later session failure', async t => {
  const f = await fixture(t); await f.user('retry');
  await f.message('previous'); await f.part('previous', { type: 'step-start' });
  await f.event('session.error', { sessionID: 'session', error: { name: 'APIError' } });
  await f.message('previous');
  const spans = await f.finish();
  assert.equal(spans.find(s => !s.parentSpanId).status.code, 2);
});

test('full structured fields are redacted before the adapter truncates prompt and tool data', async t => {
  const f = await fixture(t);
  const marker = 'PRIVATE_STRUCTURED_VALUE';
  const prompt = JSON.stringify({ 'api key': { nested: marker }, padding: 'x'.repeat(24000) });
  await f.user(prompt); await f.message('model'); await f.part('model', { type: 'step-start' });
  await f.part('model', { type: 'tool', tool: 'read', callID: 'call-source', state: {
    status: 'completed', input: { 'access token': [marker], nested: JSON.stringify({ 'private key': marker }), boundary: 'x'.repeat(19990) + 'synthetic-secret-boundary' },
    output: JSON.stringify({ 'authorization': { password: marker }, padding: 'x'.repeat(24000) }), time: { start: Date.now() - 5, end: Date.now() },
  } });
  const spans = await f.finish(), serialized = JSON.stringify(spans);
  assert.equal(serialized.includes(marker), false); assert.equal(serialized.includes('synthetic-secret'), false);
  assert.ok(serialized.includes('[REDACTED]'));
});

for (const hideErrorDetails of ['false', 'true']) test(`successful JSON fixture read stays successful (hideErrorDetails=${hideErrorDetails})`, async t => {
  const f = await fixture(t, { FI_HIDE_ERROR_DETAILS: hideErrorDetails }); await f.user('Read fixture');
  const output = JSON.stringify({ isError: true, is_error: true, detail: 'ordinary file content' });
  await f.message('first'); await f.part('first', { type: 'step-start' });
  await f.part('first', { type: 'tool', tool: 'read', callID: 'call-source', state: {
    status: 'completed', input: {}, output, time: { start: Date.now() - 5, end: Date.now() },
  } });
  const spans = await f.finish(), tool = spans.find(s => attrs(s)['fi.span.kind'] === 'TOOL');
  assert.equal(spans.length, 3);
  assert.equal(tool.status.code, 1); assert.equal(attrs(tool)['fi.agent.outcome'], 'success');
  assert.equal(attrs(tool)['output.value'], output);
});

test('native tool failures stay errors and hidden details cannot leak through later model context', async t => {
  const f = await fixture(t, { FI_HIDE_ERROR_DETAILS: 'true' }); await f.user('Read sample');
  const failure = JSON.stringify({ isError: true, detail: 'PRIVATE_FAILURE_DETAIL' });
  await f.message('first'); await f.part('first', { type: 'step-start' });
  await f.part('first', { type: 'tool', tool: 'read', callID: 'call-source', state: { status: 'error', input: {}, error: failure, time: { start: Date.now() - 5, end: Date.now() } } });
  await f.message('last');
  await f.hooks['experimental.chat.messages.transform']({}, { messages: [{ info: { role: 'tool', sessionID: 'session' }, parts: [{ type: 'tool', tool: 'read', state: { status: 'error', input: {}, error: failure } }] }] });
  await f.part('last', { type: 'step-start' }); await f.part('last', { type: 'text', id: 'answer', text: 'Could not read sample.' });
  const spans = await f.finish();
  const tool = spans.find(s => attrs(s)['fi.span.kind'] === 'TOOL');
  assert.equal(tool.status.code, 2); assert.equal(attrs(tool)['output.value'], undefined);
  assert.equal(JSON.stringify(spans).includes('PRIVATE_FAILURE_DETAIL'), false);
});
