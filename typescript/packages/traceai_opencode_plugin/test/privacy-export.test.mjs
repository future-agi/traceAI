import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setup, resolveConfig, createTurnTracer } from '@traceai/agent-core';
import { Observer } from '../dist/observer.js';

async function fixture(t, hide = true, env = {}) {
  const home = await mkdtemp(join(tmpdir(), 'opencode-privacy-'));
  const bodies = [];
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
    res.setHeader('content-type', 'application/json'); res.end('{}');
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const options = { home, agent: 'opencode', env: { FI_HIDE_ERROR_DETAILS: String(hide), ...env } };
  await setup({ ...options, configureOnly: true,
    credentials: { apiKey: 'synthetic-fi-api', secretKey: 'synthetic-fi-secret' },
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` });
  const config = await resolveConfig(options);
  const observer = new Observer(createTurnTracer({ config }), config, { directory: '/public-workspace' });
  t.after(async () => { await observer.close(); server.closeAllConnections(); await new Promise(r => server.close(r)); await rm(home, { recursive: true, force: true }); });
  let seq = 0;
  const event = (type, data = {}) => observer.event({ id: `e-${++seq}`, type, created: Date.now(),
    location: { directory: '/public-workspace' }, data: { sessionID: 's', ...data } });
  event('session.inbox.enqueued', { inboxID: 'u', item: { type: 'user', payload: { text: 'public prompt' } } });
  event('session.inbox.delivered', { inboxID: 'u' });
  const step = id => {
    event('session.step.started', { assistantMessageID: id, model: { id: 'mock', providerID: 'mock' }, started: Date.now() });
    event('session.step.ended', { assistantMessageID: id });
  };
  return { observer, event, step, bodies, spans: () => bodies.flatMap(b => b.resourceSpans).flatMap(r => r.scopeSpans).flatMap(s => s.spans) };
}

for (const format of ['object', 'json-text', 'nested-json']) test(`credential aliases are absent from exported tool output (${format})`, async t => {
  const f = await fixture(t);
  const credentials = { access_token: 'SYNTH_ACCESS_VALUE', refreshToken: 'SYNTH_REFRESH_VALUE', api_key: 'SYNTH_API_VALUE',
    clientSecret: 'SYNTH_CLIENT_VALUE', id_token: 'SYNTH_ID_VALUE', authorization: 'SYNTH_AUTH_VALUE', safe: 'public result' };
  const output = format === 'object' ? credentials : [{ type: 'text', text: format === 'json-text'
    ? JSON.stringify(credentials) : JSON.stringify({ payload: JSON.stringify(credentials) }) }];
  const original = structuredClone(output);
  f.observer.toolBefore('s', 'call', 'fetch', {});
  f.observer.toolAfter('s', 'call', output, false);
  f.event('session.execution.succeeded');
  await f.observer.close();
  assert.deepEqual(output, original, 'host-owned content is unchanged');
  const serialized = JSON.stringify(f.spans());
  for (const [key, value] of Object.entries(credentials)) if (key !== 'safe') assert.ok(!serialized.includes(value), key);
  assert.ok(serialized.includes('public result'));
});

for (const hide of [true, false]) for (const kind of ['primary', 'compaction']) test(`error-valued context is filtered before snapshot (${kind}, hide=${hide})`, async t => {
  const f = await fixture(t, hide);
  const messages = [{ role: 'tool', content: [
    { type: 'tool-result', id: 'scalar', name: 'read', result: { type: 'error', value: 'SYNTH_ERROR_SCALAR' } },
    { type: 'tool-result', id: 'structured', name: 'read', result: { type: 'error', value: { message: 'SYNTH_ERROR_STRUCTURED' } } },
    { type: 'tool-result', id: 'ok', name: 'read', result: { type: 'text', value: 'public result' } },
  ] }];
  const original = structuredClone(messages);
  if (kind === 'compaction') f.event('session.compaction.started');
  f.observer.context('s', messages, [], kind);
  if (kind === 'primary') f.step('a');
  else f.event('session.compaction.ended', { text: 'public summary' });
  f.event('session.execution.succeeded'); await f.observer.close();
  assert.deepEqual(messages, original, 'neutral context hook never mutates source messages');
  const spans = f.spans(), serialized = JSON.stringify(spans);
  assert.ok(serialized.includes('public result'));
  if (hide) {
    assert.ok(!serialized.includes('SYNTH_ERROR_'));
    assert.ok(serialized.includes('[ERROR_DETAILS_HIDDEN]'));
  } else assert.ok(serialized.includes('SYNTH_ERROR_SCALAR'));
  for (const span of spans) {
    const input = span.attributes.find(a => a.key === 'input.value')?.value.stringValue;
    if (input?.startsWith('{')) assert.doesNotThrow(() => JSON.parse(input));
  }
});

test('structured secrets are redacted before the observer truncates a large field', async t => {
  const f = await fixture(t);
  const secret = 'SYNTH_CUT_SECRET';
  const large = JSON.stringify({ ['API-KEY']: secret.repeat(2000), safe: 'public result' });
  f.observer.toolBefore('s', 'call', 'fetch', {});
  f.observer.toolAfter('s', 'call', large, false);
  f.event('session.execution.succeeded'); await f.observer.close();
  assert.ok(!JSON.stringify(f.spans()).includes(secret));
});


for (const alias of ['FI_HIDE_OUTPUTS','FI_HIDE_OUTPUT_TEXT','FI_HIDE_OUTPUT_MESSAGES']) for (const kind of ['primary','compaction'])
  test(`mixed ${kind} history respects ${alias} at OTLP boundary`, async t => {
    const f = await fixture(t, true, {[alias]:'true'});
    f.observer.context('s', [{role:'user', content:'public prompt'}, {role:'assistant',content:'SYNTH_ASSISTANT_PRIVATE'},
      {role:'tool',content:'SYNTH_TOOL_PRIVATE'}], [], kind);
    f.step('m'); f.event('session.execution.succeeded'); await f.observer.close();
    const text = JSON.stringify(f.spans());
    assert.ok(!text.includes('SYNTH_ASSISTANT_PRIVATE')); assert.ok(!text.includes('SYNTH_TOOL_PRIVATE'));
    assert.ok(f.spans().length >= 2);
  });
test('native aborted model step exports interrupted with non-error status', async t => {
  const f=await fixture(t); f.event('session.step.started',{assistantMessageID:'m', model:{id:'mock',providerID:'mock'},started:Date.now()});
  f.event('session.step.failed',{assistantMessageID:'m',error:{type:'aborted',message:'Step interrupted'}});
  f.event('session.execution.interrupted',{reason:'user'}); await f.observer.close();
  const model=f.spans().find(s=>s.attributes.some(a=>a.key==='fi.span.kind'&&a.value.stringValue==='LLM'));
  assert.ok(model); assert.ok(model.attributes.some(a=>a.key==='fi.agent.outcome'&&a.value.stringValue==='interrupted'));
  assert.notEqual(model.status.code,2);
});
