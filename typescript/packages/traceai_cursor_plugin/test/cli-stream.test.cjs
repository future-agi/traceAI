const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createCliStreamObserver } = require('../dist/cli/stream.js');
function fixture() {
  const spans = [];
  const tracer = { startTurn(value) {
    const root = { ...value, children: [] }; spans.push(root);
    return { child(v) { const child = { ...v }; root.children.push(child); return { end(e) { child.end = e; } }; },
      end(e) { root.end = e; } };
  }};
  const observer = createCliStreamObserver({ tracer, runId: 'local-invocation', ownership: 'exclusive-stream' });
  const send = value => observer.push(Buffer.from(JSON.stringify({ session_id: 'session-a', ...value }) + '\n'));
  return { spans, observer, send };
}
test('print records one honestly reduced run, tools and terminal output; no synthetic model or tokens', () => {
  const f = fixture();
  f.send({ type: 'system', subtype: 'init', apiKeySource: 'env', cwd: '/secret/path', model: 'ignored' });
  f.send({ type: 'user', message: { content: [{ text: 'prompt not part of reduced surface' }] } });
  for (const duplicate of [
    { timestamp_ms: 1, message: { content: [{ text: 'delta' }] } },
    { timestamp_ms: 1, model_call_id: 'call', message: { content: [{ text: 'delta' }] } },
    { message: { content: [{ text: 'delta' }] } },
  ]) f.send({ type: 'assistant', ...duplicate });
  f.send({ type: 'tool_call', subtype: 'started', call_id: 'tool-1', tool_call: { readToolCall: { args: { path: 'README.md' } } } });
  const completed = { type: 'tool_call', subtype: 'completed', call_id: 'tool-1', tool_call: { readToolCall: { result: { success: { content: 'read result' } } } } };
  f.send(completed); f.send(completed);
  f.send({ type: 'result', subtype: 'success', is_error: false, result: 'Final', usage: { input_tokens: 123, output_tokens: 9 }, request_id: 'not-a-model-request' });
  f.observer.finish(0);
  assert.equal(f.spans.length, 1);
  const run = f.spans[0];
  assert.equal(run.unit, 'run'); assert.equal(run.usagePlan, 'unknown');
  assert.equal(run.input, undefined); assert.equal(run.children.length, 1);
  assert.equal(run.children[0].kind, 'tool'); assert.equal(run.children[0].end.outcome, 'success');
  assert.equal(run.end.output, 'Final'); assert.equal(run.end.outcome, 'success');
  assert.equal(run.end.usage, undefined); assert.equal(f.observer.diagnostics.duplicateTools, 1);
  assert.ok(!JSON.stringify(run).includes('/secret/path'));
});
test('unterminated runs, interruption, error after success and malformed data are never successful', () => {
  for (const [code, signal, terminal, expected] of [[0, null, false, 'incomplete'], [2, null, false, 'error'], [null, 'SIGINT', true, 'interrupted'], [4, null, true, 'error']]) {
    const f = fixture(); f.send({ type: 'system' });
    if (terminal) f.send({ type: 'result', subtype: 'success', result: 'done' });
    f.observer.finish(code, signal); f.observer.finish(0);
    assert.equal(f.spans[0].end.outcome, expected);
  }
  const f = fixture(); f.send({ type: 'system' }); f.observer.push(Buffer.from('{broken\n'));
  f.send({ type: 'result', subtype: 'success', result: 'done' }); f.observer.finish(0);
  assert.equal(f.spans[0].end.outcome, 'incomplete');
});
test('split UTF-8, oversize line recovery and complete unterminated NDJSON tails', () => {
  const f = fixture();
  const bytes = Buffer.from(JSON.stringify({ type: 'result', subtype: 'success', result: 'hello 世界', session_id: 'session-a' }));
  for (const byte of bytes) f.observer.push(Buffer.from([byte]));
  f.observer.finish(0); assert.equal(f.spans[0].end.output, 'hello 世界');
  const g = fixture();
  g.observer.push(Buffer.alloc(1024 * 1024 + 1, 65)); g.observer.push(Buffer.from('\n'));
  g.send({ type: 'result', subtype: 'success', result: 'bounded' }); g.observer.finish(0);
  assert.equal(g.observer.diagnostics.oversized, 1);
  assert.equal(g.spans[0].end.outcome, 'incomplete'); assert.equal(g.spans[0].end.output, 'bounded');
});
test('session mismatch, missing call ids and tool limits do not invent spans', () => {
  const f = fixture(); f.send({ type: 'system' });
  f.send({ type: 'tool_call', subtype: 'started', session_id: 'other', call_id: 'other', tool_call: { readToolCall: {} } });
  f.send({ type: 'tool_call', subtype: 'started', tool_call: { readToolCall: {} } });
  for (let i = 0; i < 300; i++) f.send({ type: 'tool_call', subtype: 'completed', call_id: String(i), tool_call: { readToolCall: { result: { success: {} } } } });
  f.send({ type: 'result', subtype: 'success' }); f.observer.finish(0);
  assert.equal(f.spans[0].children.length, 256); assert.equal(f.observer.diagnostics.sessionMismatch, 1);
  assert.equal(f.observer.diagnostics.malformed, 1); assert.equal(f.observer.diagnostics.dropped, 44);
  assert.equal(f.spans[0].end.outcome, 'incomplete');
});
test('tool errors and nonzero shell exits are errors, absent result stays incomplete', () => {
  for (const [result, expected] of [[{ error: 'bad' }, 'error'], [{ failure: {} }, 'error'], [{ success: { exitCode: 1 } }, 'error'], [undefined, 'incomplete']]) {
    const f = fixture();
    f.send({ type: 'tool_call', subtype: 'completed', call_id: 'id', tool_call: { shellToolCall: { result } } });
    f.observer.finish(0);
    assert.equal(f.spans[0].children[0].end.outcome, expected);
  }
});
test('observer requires explicit exclusive ownership', () => {
  assert.throws(() => createCliStreamObserver({ tracer: {}, runId: 'run', ownership: 'hooks-too' }), /OWNERSHIP/);
});
