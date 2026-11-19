import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Observer } from '../dist/observer.js';

function fixture(overrides = {}) {
  const spans = [];
  let n = 0;
  const handle = (span) => ({ event(value) { (span.events ??= []).push(value); }, end(value = {}) { if (!span.end) span.end = value; },
    child(input) { const child = { ...input, root: span.turnId }; spans.push(child); return handle(child); } });
  const tracer = { startTurn(input) { const span = { ...input, kind: 'root' }; spans.push(span); return handle(span); }, async flush() {}, async shutdown() {} };
  const config = { enabled: true, hideInputs: false, hideOutputs: false, hideErrorDetails: true,
    paths: { manifest: '/nonexistent/fi-observer-test/manifest.json' }, agent: 'opencode', generation: 0,
    credentials: { apiKey: 'fixture-api-key', secretKey: 'fixture-secret-key' }, ...overrides };
  const observer = new Observer(tracer, config, { directory: '/workspace' });
  const event = (type, data = {}, extra = {}) => observer.event({ id: `evt_${++n}`, created: n * 1000, type,
    location: { directory: '/workspace' }, data: { sessionID: 's1', ...data }, ...extra });
  const prompt = (id = 'u1', sessionID = 's1') => {
    event('session.inbox.enqueued', { sessionID, inboxID: id, item: { type: 'user', payload: { text: 'hello fixture-api-key' }, delivery: 'queue' } });
    event('session.inbox.delivered', { sessionID, inboxID: id });
  };
  return { spans, observer, event, prompt };
}
const model = { id: 'mock-model', providerID: 'mock' };
const tokens = { input: 10, output: 3, reasoning: 2, cache: { read: 4, write: 1 } };
test('one immutable root per delivered prompt; model steps own usage; final turn closes', () => {
  const f = fixture();
  for (const [u, m] of [['u1', 'a1'], ['u2', 'a2']]) {
    f.prompt(u); f.observer.context('s1', [{ role: 'user', content: 'input' }], []);
    f.event('session.step.started', { assistantMessageID: m, model });
    f.event('session.text.ended', { assistantMessageID: m, text: 'answer' });
    f.event('session.step.ended', { assistantMessageID: m, tokens });
    f.event('session.execution.succeeded');
  }
  assert.equal(f.spans.filter(s => s.kind === 'root').length, 2);
  assert.ok(f.spans.every(s => s.end));
  assert.equal(f.spans[0].input, 'hello [REDACTED]');
  assert.equal(f.spans[1].fidelity, 'native-request');
  assert.equal(f.spans[1].end.usage.reasoningInOutput, false);
  assert.equal(f.spans[1].end.usage.cacheInInput, false);
  assert.equal(f.spans[0].end.usage, undefined);
});
test('foreign locations and duplicate process-wide events are ignored', () => {
  const f = fixture(); f.prompt();
  const data = { assistantMessageID: 'a', model };
  f.event('session.step.started', data, { id: 'evt_same' });
  f.event('session.step.started', data, { id: 'evt_same' });
  f.event('session.step.started', data, { location: { directory: '/other' } });
  assert.equal(f.spans.length, 2);
});
test('retry owns a failed attempt and a new generation; no invented usage', () => {
  const f = fixture(); f.prompt();
  f.event('session.step.started', { assistantMessageID: 'a', model });
  f.event('session.retry.scheduled', { assistantMessageID: 'a', attempt: 1 });
  f.event('session.step.started', { assistantMessageID: 'a', model });
  f.event('session.step.ended', { assistantMessageID: 'a', tokens });
  f.event('session.execution.succeeded');
  assert.equal(f.spans.length, 3);
  assert.equal(f.spans[1].end.outcome, 'error');
  assert.equal(f.spans[1].end.usage, undefined);
  assert.equal(f.spans[2].end.usage.input, 10);
});
test('tool failures remain errors; concurrent sessions never exchange parentage', () => {
  const f = fixture(); f.prompt('u1'); f.prompt('u2', 's2');
  for (const sessionID of ['s1', 's2']) {
    f.event('session.step.started', { sessionID, assistantMessageID: `${sessionID}-a`, model });
    f.observer.toolBefore(sessionID, 'same-call', 'read', { path: 'sample' });
  }
  f.observer.toolAfter('s1', 'same-call', 'private failure', true);
  f.observer.toolAfter('s2', 'same-call', 'public result', false);
  const tools = f.spans.filter(s => s.kind === 'tool');
  assert.equal(tools.length, 2);
  assert.notEqual(tools[0].root, tools[1].root);
  assert.equal(tools[0].end.outcome, 'error');
  assert.equal(tools[0].end.output, undefined);
  assert.equal(tools[1].end.output, 'public result');
});
test('compaction is a child, never a prompt; synthetic input is not a root', () => {
  const f = fixture(); f.prompt();
  f.event('session.compaction.started', { reason: 'auto' });
  f.observer.context('s1', [{ text: 'summarize' }], [], 'compaction');
  f.event('session.compaction.ended', { text: 'summary', tokens });
  f.event('session.inbox.enqueued', { inboxID: 'synthetic', item: { type: 'synthetic', payload: { text: 'notice' } } });
  f.event('session.inbox.delivered', { inboxID: 'synthetic' });
  assert.deepEqual(f.spans.map(s => s.kind), ['root', 'compaction']);
  assert.equal(f.spans[1].fidelity, 'aggregate-turn');
  assert.equal(f.spans[1].end.usage.input, 10);
});
test('verified child sessions share the parent trace and delay its completion', () => {
  const f = fixture(); f.prompt();
  f.event('session.created', { sessionID: 'child', parentID: 's1' });
  f.prompt('child-user', 'child');
  f.event('session.execution.succeeded');
  assert.equal(f.spans[0].end, undefined);
  f.event('session.execution.succeeded', { sessionID: 'child' });
  assert.equal(f.spans[0].end.outcome, 'success');
  assert.deepEqual(f.spans.map(s => s.kind), ['root', 'agent']);
});
test('privacy off retains no prompt or tool content, and caps huge fields', () => {
  const f = fixture({ hideInputs: true, hideOutputs: true }); f.prompt();
  f.observer.toolBefore('s1', 'call', 'read', { content: 'secret' });
  f.observer.toolAfter('s1', 'call', 'secret', false);
  assert.equal(f.spans[0].input, undefined);
  assert.equal(f.spans[1].input, undefined);
  assert.equal(f.spans[1].end.output, undefined);
  const g = fixture(); assert.ok(g.observer.text('x'.repeat(1_000_000)).length <= 20_000);
});
test('aborted execution is interrupted, not successful', () => {
  const f = fixture(); f.prompt(); f.event('session.execution.interrupted');
  assert.equal(f.spans[0].end.outcome, 'interrupted');
});
test('next context hook can precede prior durable step end without losing model input', () => {
  const f = fixture(); f.prompt();
  f.observer.context('s1', [{ text: 'first request' }], []);
  f.event('session.step.started', { assistantMessageID: 'a1', model });
  f.observer.context('s1', [{ text: 'second request' }], []);
  f.event('session.step.ended', { assistantMessageID: 'a1', tokens });
  f.event('session.step.started', { assistantMessageID: 'a2', model });
  f.event('session.step.ended', { assistantMessageID: 'a2', tokens });
  f.event('session.execution.succeeded', {}, { location: undefined });
  assert.match(f.spans[1].input, /first request/);
  assert.match(f.spans[2].input, /second request/);
  assert.equal(f.spans[0].end.outcome, 'success');
});
test('reused provider call IDs in later requests get distinct tool identities', () => {
  const f = fixture(); f.prompt();
  for (const id of ['request-1', 'request-2']) {
    f.event('session.step.started', { assistantMessageID: id, model });
    f.observer.toolBefore('s1', 'reused-call', 'read', {}, id);
    f.observer.toolAfter('s1', 'reused-call', 'result', false);
    f.event('session.step.ended', { assistantMessageID: id, tokens });
  }
  const tools = f.spans.filter(s => s.kind === 'tool');
  assert.equal(tools.length, 2); assert.notEqual(tools[0].id, tools[1].id);
});
test('new prompt does not interrupt surviving child work; native completion closes its original root', () => {
  const f = fixture(); f.prompt('old');
  f.event('session.created', { sessionID: 'child', parentID: 's1' });
  f.prompt('child-prompt', 'child');
  f.prompt('new');
  assert.equal(f.spans[0].end, undefined);
  assert.equal(f.spans[1].end, undefined);
  assert.equal(f.spans.filter(s => s.kind === 'root').length, 2);
  f.event('session.execution.succeeded');
  f.event('session.execution.succeeded', { sessionID: 'child' });
  assert.ok(f.spans.every(s => s.end.outcome === 'success'));
});
for (const dynamic of [false, true]) for (const intervening of [false, 'succeeded', 'interrupted']) test(`background result delivery and parent continuation stay inside the original turn (dynamic=${dynamic}, intervening=${intervening})`, () => {
  const f = fixture(); f.prompt();
  f.event('session.step.started', { assistantMessageID: 'parent-a', model });
  f.observer.toolBefore('s1', 'spawn', 'subagent', { background: !dynamic });
  f.event('session.tool.progress', { id: 'spawn', metadata: { sessionID: 'child', status: 'running' } });
  f.event('session.tool.success', { id: 'spawn', metadata: { sessionID: 'child', status: 'running' } });
  f.prompt('child-prompt', 'child');
  f.event('session.step.ended', { assistantMessageID: 'parent-a', tokens });
  f.event('session.execution.succeeded');
  if (intervening) {
    f.prompt('intervening');
    f.event('session.step.started', { assistantMessageID: 'new-a', model });
    f.event('session.step.ended', { assistantMessageID: 'new-a', tokens });
    f.event(`session.execution.${intervening}`);
    assert.equal(f.spans.find(s => s.kind === 'root' && s.turnId === 'intervening').end.outcome, intervening === 'succeeded' ? 'success' : 'interrupted');
    assert.equal(f.spans[0].end, undefined);
  }
  f.event('session.step.started', { sessionID: 'child', assistantMessageID: 'child-a', model });
  f.event('session.step.ended', { sessionID: 'child', assistantMessageID: 'child-a', tokens });
  f.event('session.execution.succeeded', { sessionID: 'child' });
  assert.equal(f.spans[0].end, undefined, 'child end is not parent completion');
  f.event('session.inbox.enqueued', { inboxID: 'notice', item: { type: 'synthetic', payload: {
    metadata: { source: 'subagent', childID: 'child', state: 'completed' } } } });
  f.event('session.execution.started');
  f.observer.context('s1', [{ text: 'continuation-only request' }], []);
  f.event('session.inbox.delivered', { inboxID: 'notice' });
  f.event('session.step.started', { assistantMessageID: 'parent-b', model });
  f.observer.toolBefore('s1', 'read', 'read', {});
  f.observer.toolAfter('s1', 'read', 'public file', false);
  f.event('session.text.ended', { assistantMessageID: 'parent-b', text: 'final answer' });
  f.event('session.step.ended', { assistantMessageID: 'parent-b', tokens });
  f.event('session.execution.succeeded');
  assert.equal(f.spans.filter(s => s.kind === 'root').length, intervening ? 2 : 1);
  assert.equal(f.spans.filter(s => s.kind === 'llm').length, intervening ? 4 : 3);
  assert.equal(f.spans.find(s => s.id === 'parent-b:attempt:0').root, 'u1');
  assert.equal(f.spans.find(s => s.id === 'child-a:attempt:0').root, 'u1');
  assert.equal(f.spans.filter(s => s.kind === 'tool').length, 2);
  assert.equal(f.spans[0].end.output, 'final answer');
  assert.equal(f.spans[0].end.outcome, 'success');
  assert.match(f.spans.find(s => s.id === 'parent-b:attempt:0').input, /continuation-only request/);
  assert.ok(f.spans.every(s => s.end));
});
test('native subagent progress recovers ancestry without a session.created event after reload', () => {
  const f = fixture(); f.prompt();
  f.observer.toolBefore('s1', 'resume', 'subagent', { sessionID: 'existing-child' });
  f.event('session.tool.progress', { id: 'resume', metadata: { sessionID: 'existing-child' } });
  f.prompt('resumed-prompt', 'existing-child');
  f.event('session.step.started', { sessionID: 'existing-child', assistantMessageID: 'a', model });
  f.event('session.step.ended', { sessionID: 'existing-child', assistantMessageID: 'a', tokens });
  f.event('session.execution.succeeded', { sessionID: 'existing-child' });
  f.observer.toolAfter('s1', 'resume', 'complete', false);
  f.event('session.execution.succeeded');
  assert.equal(f.spans.filter(s => s.kind === 'root').length, 1);
  const branch = f.spans.find(s => s.kind === 'agent'), tool = f.spans.find(s => s.kind === 'tool');
  assert.equal(branch.parentId, tool.id);
  assert.equal(branch.root, f.spans[0].turnId);
});
test('foreground subagent progress does not falsely wait for a background notification', () => {
  const f = fixture(); f.prompt();
  f.observer.toolBefore('s1', 'spawn', 'subagent', { background: false });
  f.event('session.tool.progress', { id: 'spawn', metadata: { sessionID: 'child', status: 'running' } });
  f.prompt('child-prompt', 'child');
  f.event('session.execution.succeeded', { sessionID: 'child' });
  f.event('session.tool.success', { id: 'spawn', metadata: { sessionID: 'child', status: 'completed' } });
  f.event('session.execution.succeeded');
  assert.equal(f.spans[0].end.outcome, 'success');
});
test('arbitrary tool metadata cannot attach an unrelated conversation as a subagent', () => {
  const f = fixture(); f.prompt();
  f.observer.toolBefore('s1', 'read', 'read', {});
  f.event('session.tool.progress', { id: 'read', metadata: { sessionID: 'unrelated' } });
  f.prompt('unrelated-prompt', 'unrelated');
  assert.equal(f.spans.filter(s => s.kind === 'root').length, 2);
});
test('pending background notices survive new prompts but do not prevent forced shutdown', async () => {
  const f = fixture(); f.prompt('old');
  f.observer.toolBefore('s1', 'spawn', 'subagent', { background: true });
  f.event('session.tool.progress', { id: 'spawn', metadata: { sessionID: 'child' } });
  f.prompt('child-prompt', 'child');
  f.event('session.execution.succeeded');
  f.event('session.execution.succeeded', { sessionID: 'child' });
  f.prompt('new');
  assert.equal(f.spans[0].end, undefined);
  await f.observer.close();
  assert.equal(f.spans[0].end.outcome, 'incomplete');
  assert.ok(f.spans.every(s => s.end));
});

test('overflow compaction abandons only old request snapshots and preserves the next turn input', () => {
  const f = fixture(); f.prompt();
  f.observer.context('s1', [{ text: 'abandoned-overflow' }], []);
  f.event('session.compaction.started', { reason: 'auto' });
  f.observer.context('s1', [{ text: 'summary-request' }], [], 'compaction');
  f.event('session.compaction.ended', { text: 'summary', tokens });
  f.observer.context('s1', [{ text: 'recovered-request' }], []);
  f.event('session.step.started', { assistantMessageID: 'recovered', model });
  f.event('session.step.ended', { assistantMessageID: 'recovered', tokens });
  // A discarded request must not leak into the next user's turn either.
  f.observer.context('s1', [{ text: 'abandoned-at-turn-end' }], []);
  f.event('session.execution.succeeded');
  f.prompt('next');
  f.observer.context('s1', [{ text: 'next-user-unique' }], []);
  f.event('session.step.started', { assistantMessageID: 'next', model });
  const generations = f.spans.filter(s => s.kind === 'llm');
  assert.match(generations[0].input, /recovered-request/);
  assert.match(generations[1].input, /next-user-unique/);
  assert.ok(generations.every(s => !s.input.includes('abandoned')));
});
test('tool-call-only generations retain a privacy-filtered model output', () => {
  const f = fixture(); f.prompt();
  f.event('session.step.started', { assistantMessageID: 'a', model });
  f.observer.toolBefore('s1', 'call', 'read', { path: 'sample.txt', secret: 'not-captured' });
  f.observer.toolAfter('s1', 'call', 'sample', false);
  f.event('session.step.ended', { assistantMessageID: 'a', tokens });
  const output = f.spans.find(s => s.kind === 'llm').end.output;
  assert.match(output, /tool_calls/); assert.match(output, /read/);
  assert.ok(!output.includes('not-captured'));
});
test('unknown consumed event shapes record a structural gap without closing or dropping the turn', () => {
  const f = fixture(); f.prompt();
  f.event('session.step.started', { assistantMessageID: 'bad', model: null });
  assert.equal(f.spans[1].end.gap, 'UNSUPPORTED_EVENT_SHAPE');
  f.event('session.step.started', { assistantMessageID: 'good', model });
  f.event('session.step.ended', { assistantMessageID: 'good', tokens });
  f.event('session.execution.succeeded');
  assert.equal(f.spans.filter(s => s.kind === 'llm').length, 1);
  assert.equal(f.spans[0].end.outcome, 'success');
});

for (const outcome of ['succeeded', 'interrupted', 'failed']) test(`co-delivered prompts share the final response and source link, not usage (${outcome})`, () => {
  const f = fixture(); f.prompt('first');
  f.event('session.step.started', { assistantMessageID: 'a1', model });
  f.prompt('second'); f.prompt('third');
  f.event('session.step.ended', { assistantMessageID: 'a1', tokens });
  f.event('session.step.started', { assistantMessageID: 'a2', model });
  f.event('session.text.ended', { assistantMessageID: 'a2', text: 'shared final answer' });
  f.event('session.step.ended', { assistantMessageID: 'a2', tokens });
  f.event(`session.execution.${outcome}`);
  const roots = f.spans.filter(s => s.kind === 'root');
  assert.equal(roots.length, 3);
  assert.equal(f.spans.filter(s => s.kind === 'llm').length, 2);
  for (const root of roots) {
    assert.equal(root.end.output, 'shared final answer');
    assert.equal(root.end.outcome, { succeeded: 'success', interrupted: 'interrupted', failed: 'error' }[outcome]);
    assert.equal(root.end.usage, undefined);
    assert.deepEqual(JSON.parse(root.events.at(-1).input), { ownerTurn: 'third', request: 'a2' });
  }
});
test('background completion participates in a running user prompt without stealing its answer', () => {
  const f = fixture(); f.prompt('original');
  f.observer.toolBefore('s1', 'spawn', 'subagent', { background: true });
  f.event('session.tool.success', { id: 'spawn', metadata: { sessionID: 'child', status: 'running' } });
  f.prompt('child-prompt', 'child'); f.event('session.execution.succeeded');
  f.prompt('intervening');
  f.event('session.step.started', { assistantMessageID: 'user-a', model });
  f.event('session.execution.succeeded', { sessionID: 'child' });
  f.event('session.inbox.enqueued', { inboxID: 'notice', item: { type: 'synthetic', payload: { metadata: { source: 'subagent', childID: 'child' } } } });
  f.event('session.inbox.delivered', { inboxID: 'notice' });
  f.event('session.step.ended', { assistantMessageID: 'user-a', tokens });
  f.event('session.step.started', { assistantMessageID: 'continuation', model });
  f.event('session.text.ended', { assistantMessageID: 'continuation', text: 'answer to both' });
  f.event('session.step.ended', { assistantMessageID: 'continuation', tokens });
  f.event('session.execution.succeeded');
  const roots = f.spans.filter(s => s.kind === 'root');
  assert.equal(roots.length, 2); assert.ok(roots.every(s => s.end.output === 'answer to both'));
  assert.ok(roots.every(s => s.events.length === 1));
  assert.equal(f.spans.filter(s => s.kind === 'llm').length, 2);
});
