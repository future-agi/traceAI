import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { observe } from '../dist/observer.js';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/opencode-1.19.0-session.json', import.meta.url))).steps;
function harness(t, privacy = false) {
  const dir = mkdtempSync(join(tmpdir(), 'opencode-observer-')); t.after(() => rmSync(dir, { recursive: true, force: true }));
  const spans = [];
  const tracer = {
    startTurn(input) {
      const row = { kind: 'root', ...input }; spans.push(row);
      return { end(end) { Object.assign(row, end); }, child(input) { const row = { ...input }; spans.push(row); return { end(end) { Object.assign(row, end); } }; } };
    }, async flush() {}, async shutdown() {},
  };
  const config = { enabled: true, agent: 'opencode-v1', generation: 0, paths: { manifest: join(dir, 'manifest.json') }, credentials: { apiKey: 'dummy-public', secretKey: 'dummy-secret' }, hideInputs: privacy, hideOutputs: privacy, hideErrorDetails: privacy };
  const hooks = observe(config, tracer);
  t.after(() => hooks.dispose());
  return { hooks, spans, config };
}
async function replay(hooks, steps = fixture) {
  for (const step of steps) {
    if (step.event) await hooks.event?.({ event: structuredClone(step.event) });
    else if (step.hook !== 'dispose') await hooks[step.hook]?.(structuredClone(step.input), structuredClone(step.output));
  }
}
test('loader sees exactly one runtime function export and no V2 helpers', async () => {
  const module = await import('../dist/index.js'); assert.deepEqual(Object.keys(module), ['default']); assert.equal(typeof module.default, 'function');
});
test('interleaved sessions never mix generation or tool parents', async t => {
  const { hooks, spans } = harness(t);
  const parent = fixture[0].event.properties.info.id;
  const concurrent = JSON.parse(JSON.stringify(fixture).replaceAll(parent, 'ses_parallel').replaceAll('msg_', 'msg_parallel_').replaceAll('prt_', 'prt_parallel_').replaceAll('call_', 'call_parallel_'));
  for (let i = 0; i < fixture.length; i++) await replay(hooks, [fixture[i], concurrent[i]]);
  const roots = spans.filter(s => s.kind === 'root'); assert.equal(roots.length, 2); assert.notEqual(roots[0].sessionId, roots[1].sessionId);
  for (const tool of spans.filter(s => s.kind === 'tool')) assert.equal(tool.id.includes('parallel'), tool.parentId.includes('parallel'));
});
test('recorded V1 flow emits one immutable prompt root, two real model steps and two tools', async t => {
  const { hooks, spans } = harness(t);
  await replay(hooks);
  assert.equal(spans.filter(s => s.kind === 'root').length, 1);
  assert.equal(spans.filter(s => s.kind === 'llm').length, 2);
  assert.equal(spans.filter(s => s.kind === 'tool').length, 2);
  assert.ok(spans.filter(s => s.kind === 'llm').every(s => s.fidelity === 'native-request'));
  assert.ok(spans.filter(s => s.kind === 'tool').every(s => spans.some(p => p.id === s.parentId && p.kind === 'llm')));
  assert.equal(spans.find(s => s.kind === 'root').outcome, 'success');
  assert.equal(spans.filter(s => s.kind === 'llm')[1].usage.cacheRead, 4608);
  assert.equal(spans.filter(s => s.kind === 'llm')[0].usage.reasoning, 32);
  const n = spans.length;
  await replay(hooks); await hooks.dispose();
  assert.equal(spans.length, n, 'duplicate idle/history/dispose never re-emits a root');
  assert.ok(!JSON.stringify(spans).includes('abc123def456ghi'));
});
test('two user prompts in one conversation create different traces, not session root updates', async t => {
  const { hooks, spans } = harness(t);
  await replay(hooks);
  const next = JSON.parse(JSON.stringify(fixture).replaceAll('msg_', 'msg_next_').replaceAll('prt_', 'prt_next_').replaceAll('call_', 'call_next_'));
  await replay(hooks, next);
  const roots = spans.filter(s => s.kind === 'root');
  assert.equal(roots.length, 2); assert.equal(roots[0].sessionId, roots[1].sessionId); assert.notEqual(roots[0].turnId, roots[1].turnId);
});
test('privacy-off mode keeps counters but omits prompt, tool and assistant bodies', async t => {
  const { hooks, spans } = harness(t, true); await replay(hooks); await hooks.dispose();
  assert.ok(!JSON.stringify(spans).includes('SECRET-MARKER'));
  assert.equal(spans.filter(s => s.kind === 'llm').length, 2);
});
test('persistent disable fences already captured turns and never changes host output', async t => {
  const { hooks, spans, config } = harness(t);
  await replay(hooks, fixture.slice(0, -3));
  writeFileSync(config.paths.manifest, JSON.stringify({ schemaVersion: 1, disabled: { 'opencode-v1': true } }), { mode: 0o600 });
  await replay(hooks, fixture.slice(-3)); await hooks.dispose();
  assert.equal(spans.length, 0);
});
test('no prompt hook means historical message updates cannot become new turns after reload', async t => {
  const { hooks, spans } = harness(t);
  await replay(hooks, fixture.filter(s => s.hook !== 'chat.message')); await hooks.dispose(); assert.equal(spans.length, 0);
});
test('interruption and unfinished work are not success', async t => {
  const { hooks, spans } = harness(t); await replay(hooks, fixture.slice(0, 8));
  await hooks.event({ event: { type: 'session.error', properties: { sessionID: fixture[0].event.properties.info.id, error: { name: 'MessageAbortedError' } } } });
  await hooks.dispose(); assert.equal(spans.find(s => s.kind === 'root').outcome, 'interrupted');
  assert.equal(spans.find(s => s.kind === 'llm').outcome, 'incomplete');
});
test('compaction is activity under the user turn and synthetic messages are not new prompts', async t => {
  const { hooks, spans } = harness(t);
  await replay(hooks, fixture.slice(0, -3));
  const sessionID = fixture[0].event.properties.info.id;
  await hooks['experimental.session.compacting']({ sessionID }, { context: [] });
  await hooks.event({ event: { type: 'session.compacted', properties: { sessionID } } });
  await hooks.event({ event: { type: 'message.updated', properties: { info: { id: 'synthetic', role: 'user', sessionID } } } });
  await replay(hooks, fixture.slice(-3));
  assert.equal(spans.filter(s => s.kind === 'root').length, 1); assert.equal(spans.filter(s => s.kind === 'compaction').length, 1);
});
async function user(hooks, sessionID, id, parts = [{ type: 'text', text: 'prompt' }]) {
  await hooks['chat.message']({}, { message: { id, sessionID, time: { created: Date.now() } }, parts });
}
async function event(hooks, type, properties) { await hooks.event({ event: { type, properties } }); }
async function generation(hooks, sessionID, parentID, id, finish = true) {
  await event(hooks, 'message.updated', { info: { id, parentID, sessionID, role: 'assistant', time: { created: Date.now(), ...(finish ? { completed: Date.now() } : {}) }, tokens: { input: 12, output: 8 }, modelID: 'test', providerID: 'mock' } });
  await event(hooks, 'message.part.updated', { part: { type: 'step-start', messageID: id, sessionID } });
}
async function task(hooks, callID, background = false) {
  await event(hooks, 'message.part.updated', { part: { type: 'tool', tool: 'task', callID, messageID: 'parent-gen', sessionID: 'parent', state: { status: 'completed', input: {}, output: 'started', time: { start: Date.now(), end: Date.now() }, metadata: { sessionId: 'child', background } } } });
}
test('background child survives parent idle and synthetic result continues the original trace', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 'parent', 'prompt'); await generation(hooks, 'parent', 'prompt', 'parent-gen');
  await event(hooks, 'session.created', { info: { id: 'child', parentID: 'parent' } });
  await task(hooks, 'task-one', true);
  await user(hooks, 'child', 'child-prompt'); await generation(hooks, 'child', 'child-prompt', 'child-gen', false);
  await event(hooks, 'session.idle', { sessionID: 'parent' }); assert.equal(spans.length, 0);
  await generation(hooks, 'child', 'child-prompt', 'child-gen');
  await event(hooks, 'message.part.updated', { part: { type: 'text', id: 'child-text', messageID: 'child-gen', sessionID: 'child', text: 'CHILD_FINISHED' } });
  await event(hooks, 'session.idle', { sessionID: 'child' }); assert.equal(spans.length, 0);
  await user(hooks, 'parent', 'notification', [{ type: 'text', synthetic: true, text: '<task id="child" state="completed">result</task>' }]);
  await generation(hooks, 'parent', 'notification', 'notification-gen');
  await event(hooks, 'session.idle', { sessionID: 'parent' });
  assert.equal(spans.filter(s => s.kind === 'root').length, 1);
  assert.equal(spans.filter(s => s.kind === 'llm').length, 3);
  assert.equal(spans.find(s => s.id === 'child-gen').output, 'CHILD_FINISHED');
  assert.equal(spans.find(s => s.id === 'child-gen').usage.output, 8);
  assert.ok(spans.every(s => s.outcome === 'success'));
});
test('resuming the same child session binds each turn to its own task invocation', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 'parent', 'prompt'); await generation(hooks, 'parent', 'prompt', 'parent-gen');
  await event(hooks, 'session.created', { info: { id: 'child', parentID: 'parent' } });
  for (const n of ['one', 'two']) {
    await task(hooks, `task-${n}`); await user(hooks, 'child', `child-${n}`);
    await generation(hooks, 'child', `child-${n}`, `generation-${n}`);
    await event(hooks, 'session.idle', { sessionID: 'child' });
  }
  await event(hooks, 'session.idle', { sessionID: 'parent' });
  for (const n of ['one', 'two']) assert.equal(spans.find(s => s.id === `session-child-${n}`).parentId, `parent-gen-task-${n}`);
});
test('attachment-only prompts remain genuine user turns; unmatched synthetic messages do not', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 'parent', 'synthetic', [{ type: 'text', synthetic: true, text: '<task id="unknown" state="completed">result</task>' }]);
  await event(hooks, 'session.idle', { sessionID: 'parent' }); assert.equal(spans.length, 0);
  await user(hooks, 'parent', 'attachment', [{ type: 'file', url: 'file:///tmp/sample.png', mime: 'image/png' }]);
  await generation(hooks, 'parent', 'attachment', 'attachment-gen');
  await event(hooks, 'session.idle', { sessionID: 'parent' });
  assert.equal(spans.filter(s => s.kind === 'root').length, 1);
});
test('shutdown of pending background work preserves ancestry and reports incomplete', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 'parent', 'prompt'); await generation(hooks, 'parent', 'prompt', 'parent-gen');
  await task(hooks, 'task-one', true); await user(hooks, 'child', 'child-prompt');
  await generation(hooks, 'child', 'child-prompt', 'child-gen', false);
  await event(hooks, 'session.idle', { sessionID: 'parent' }); await hooks.dispose();
  assert.equal(spans.filter(s => s.kind === 'root').length, 1);
  assert.equal(spans.find(s => s.kind === 'root').outcome, 'incomplete');
  assert.equal(spans.find(s => s.id === 'child-gen').outcome, 'incomplete');
});
test('retention expires unresolved background work without waiting in a hook', async t => {
  const { hooks, spans } = harness(t);
  t.mock.timers.enable({ apis: ['Date', 'setInterval'], now: Date.now() });
  // Create a new observer after enabling fake timers so its expiry timer is controlled.
  await hooks.dispose();
  const f = harness(t);
  await user(f.hooks, 'parent', 'prompt'); await generation(f.hooks, 'parent', 'prompt', 'parent-gen');
  await task(f.hooks, 'task-one', true);
  await event(f.hooks, 'session.idle', { sessionID: 'parent' });
  t.mock.timers.tick(15 * 60 * 1000);
  assert.equal(f.spans.find(s => s.kind === 'root').outcome, 'incomplete');
});
test('task metadata proves child ancestry; child idle does not export a separate root', async t => {
  const { hooks, spans } = harness(t);
  const parentSteps = structuredClone(fixture.slice(0, -3));
  // A real call's tool name is stable from pending to completed.
  for (const step of parentSteps) if (step.event?.properties?.part?.type === 'tool') step.event.properties.part.tool = 'task';
  await replay(hooks, parentSteps);
  const parent = fixture[0].event.properties.info.id;
  const childSteps = JSON.parse(JSON.stringify(fixture).replaceAll(parent, 'ses_child').replaceAll('msg_', 'msg_child_').replaceAll('prt_', 'prt_child_').replaceAll('call_', 'call_child_'));
  childSteps[0].event.properties.info.parentID = parent;
  await replay(hooks, childSteps);
  assert.equal(spans.length, 0);
  const toolEvent = structuredClone(fixture.find(s => s.event?.properties?.part?.type === 'tool' && s.event.properties.part.state.status === 'completed'));
  toolEvent.event.properties.part.state.metadata = { sessionId: 'ses_child' };
  toolEvent.event.properties.part.tool = 'task';
  await replay(hooks, [toolEvent, ...fixture.slice(-3)]);
  assert.equal(spans.filter(s => s.kind === 'root').length, 1);
  const child = spans.find(s => s.kind === 'agent'); assert.ok(child); assert.ok(spans.some(s => s.id === child.parentId && s.kind === 'tool'));
  assert.equal(spans.filter(s => s.kind === 'llm').length, 4);
});

for (const [name, expected] of [['ContextOverflowError', 'error'], ['APIError', 'error'], ['MessageAbortedError', 'interrupted']]) {
  test(`idle before terminal ${name} preserves the failed model activity`, async t => {
    const { hooks, spans } = harness(t);
    await user(hooks, 's', 'prompt');
    await event(hooks, 'message.updated', { info: { id: 'failed', parentID: 'prompt', sessionID: 's', role: 'assistant', modelID: 'test', providerID: 'mock', time: { created: Date.now() } } });
    await event(hooks, 'session.error', { sessionID: 's', error: { name } });
    await event(hooks, 'session.idle', { sessionID: 's' });
    assert.equal(spans.length, 0, 'idle is not completion');
    await new Promise(r => setTimeout(r, 40));
    await event(hooks, 'message.updated', { info: { id: 'failed', parentID: 'prompt', sessionID: 's', role: 'assistant', modelID: 'test', providerID: 'mock', time: { created: Date.now() - 100, completed: Date.now() }, error: { name } } });
    assert.equal(spans.find(s => s.kind === 'root').outcome, expected);
    const generation = spans.find(s => s.kind === 'generation');
    assert.equal(generation.outcome, expected); assert.equal(generation.fidelity, 'reconstructed-message');
    assert.equal(generation.input, 'prompt');
    await hooks.dispose(); assert.equal(spans.filter(s => s.kind === 'root').length, 1);
  });
}
test('a recovered context overflow is not a failed turn', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 's', 'prompt');
  await event(hooks, 'session.error', { sessionID: 's', error: { name: 'ContextOverflowError' } });
  await event(hooks, 'message.updated', { info: { id: 'overflow', parentID: 'prompt', sessionID: 's', role: 'assistant', time: { created: Date.now(), completed: Date.now() }, error: { name: 'ContextOverflowError' } } });
  await generation(hooks, 's', 'prompt', 'recovered');
  await event(hooks, 'session.idle', { sessionID: 's' });
  assert.equal(spans.find(s => s.kind === 'root').outcome, 'success');
  assert.equal(spans.find(s => s.id === 'overflow').outcome, 'error');
});
test('idle retains terminal tool updates and tool-call-only generations have output', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 's', 'prompt'); await generation(hooks, 's', 'prompt', 'model');
  const part = { type: 'tool', messageID: 'model', sessionID: 's', callID: 'call', tool: 'read', state: { status: 'running', time: { start: Date.now() }, input: { file: 'README.md' } } };
  await event(hooks, 'message.part.updated', { part }); await event(hooks, 'session.idle', { sessionID: 's' });
  assert.equal(spans.length, 0);
  await event(hooks, 'message.part.updated', { part: { ...part, state: { ...part.state, status: 'completed', time: { ...part.state.time, end: Date.now() }, output: 'file' } } });
  assert.equal(spans.find(s => s.kind === 'tool').output, 'file');
  assert.ok(spans.find(s => s.kind === 'llm').output.includes('tool_calls'));
});
test('unknown tool schema yields an incomplete trace and an explicit gap, not silent success', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 's', 'prompt'); await generation(hooks, 's', 'prompt', 'model');
  await event(hooks, 'message.part.updated', { part: { type: 'tool', messageID: 'model', sessionID: 's', callID: 'call', tool: 'read', state: { status: 'future-status' } } });
  await hooks.dispose();
  assert.equal(spans.find(s => s.kind === 'root').outcome, 'incomplete');
  assert.equal(spans.find(s => s.kind === 'root').gap, 'UNKNOWN_TOOL_SHAPE');
});
test('late updates from a completed prompt cannot attach to a subsequent prompt', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 's', 'first'); await generation(hooks, 's', 'first', 'first-gen');
  await event(hooks, 'session.idle', { sessionID: 's' });
  await user(hooks, 's', 'second'); await generation(hooks, 's', 'first', 'late-old-gen');
  await generation(hooks, 's', 'second', 'second-gen');
  await event(hooks, 'session.idle', { sessionID: 's' });
  assert.equal(spans.filter(s => s.kind === 'root').length, 2);
  assert.ok(!spans.some(s => s.id === 'late-old-gen'));
});
test('a successful step before context overflow is not evidence that the overflow recovered', async t => {
  const { hooks, spans } = harness(t);
  await user(hooks, 's', 'prompt'); await generation(hooks, 's', 'prompt', 'previous-success');
  await event(hooks, 'session.error', { sessionID: 's', error: { name: 'ContextOverflowError' } });
  await event(hooks, 'session.idle', { sessionID: 's' });
  assert.equal(spans.length, 0, 'wait for the error-bearing terminal assistant row');
  await event(hooks, 'message.updated', { info: { id: 'failed-next-step', parentID: 'prompt', sessionID: 's', role: 'assistant', time: { created: Date.now(), completed: Date.now() }, error: { name: 'ContextOverflowError' } } });
  assert.equal(spans.find(s => s.kind === 'root').outcome, 'error');
  assert.equal(spans.find(s => s.id === 'failed-next-step').outcome, 'error');
});
