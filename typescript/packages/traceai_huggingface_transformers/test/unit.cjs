const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { context, trace, SpanKind, SpanStatusCode } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const { TransformersInstrumentation } = require('@traceai/huggingface-transformers');
const telemetry = require('./telemetry.cjs');
let t;
before(async () => { t = await telemetry.start(); });
after(async () => { await t.close(); });
function pipeline(fn, task = 'text-generation') {
  Object.assign(fn, { task, model: { config: { _name_or_path: 'synthetic/model' } }, tokenizer: {}, dispose() { return 'disposed'; } });
  return fn;
}
const last = () => t.memory.getFinishedSpans().at(-1);

test('callable identity, this, synchronous values, properties, disabled and suppressed', () => {
  const i = new TransformersInstrumentation();
  const tensor = { dims: [1, 3], data: new Float32Array([1, 2, 3]) };
  const p = pipeline(function () { assert.equal(this, receiver); return tensor; }, 'feature-extraction');
  const receiver = {};
  const w = i.wrapPipeline(p);
  const count = t.memory.getFinishedSpans().length;
  assert.equal(w.call(receiver), tensor);
  assert.equal(w.model, p.model); assert.equal(w.tokenizer, p.tokenizer);
  assert.equal(w.dispose(), 'disposed');
  assert.equal(i.wrapPipeline(p), w); assert.equal(i.wrapPipeline(w), w);
  assert.equal(new TransformersInstrumentation().wrapPipeline(w), w);
  assert.equal(t.memory.getFinishedSpans().length, count + 1);
  assert.equal(last().attributes['fi.span.kind'], 'EMBEDDING');
  assert.equal(last().kind, SpanKind.INTERNAL);
  assert.deepEqual(last().attributes['transformers.output.shape'], [1, 3]);
  assert.ok(!JSON.stringify(last().attributes).includes('data'));
  i.disable(); w.call(receiver); i.enable();
  context.with(suppressTracing(context.active()), () => w.call(receiver));
  assert.equal(t.memory.getFinishedSpans().length, count + 1);
});

test('immutable module facade, factory this, no factory/disposal spans, unsupported passthrough', async () => {
  const i = new TransformersInstrumentation();
  const p = pipeline(async () => [{ generated_text: 'ok' }]);
  const original = Object.freeze({ pipeline() { assert.equal(this, original); return Promise.resolve(p); }, sentinel: {} });
  const w = i.wrapModule(original);
  assert.equal(w.sentinel, original.sentinel); assert.equal(i.wrapModule(original), w); assert.equal(i.wrapModule(w), w);
  const before = t.memory.getFinishedSpans().length;
  const loaded = await w.pipeline();
  loaded.dispose(); assert.equal(t.memory.getFinishedSpans().length, before);
  await loaded('hello'); assert.equal(t.memory.getFinishedSpans().length, before + 1);
  for (const invalid of [null, undefined, 3, {}, pipeline(() => 1, 'image-classification')]) {
    assert.equal(i.wrapPipeline(invalid), invalid);
    if (typeof invalid !== 'function') assert.equal(i.wrapModule(invalid), invalid);
  }
  const error = new Error('factory failed');
  await assert.rejects(i.wrapModule({ pipeline: async () => { throw error; } }).pipeline(), e => e === error);
  assert.equal(t.memory.getFinishedSpans().length, before + 1);
  // Unsupported/synchronous factory results retain their original call shape.
  const sync = i.wrapModule({ pipeline: () => p });
  assert.equal(sync.pipeline(), i.wrapPipeline(p));
  const invalidModule = { get pipeline() { throw error; } };
  assert.equal(i.wrapModule(invalidModule), invalidModule);
});

test('correct active parent and concurrent async call isolation', async () => {
  const i = new TransformersInstrumentation();
  const observed = [];
  const p = i.wrapPipeline(pipeline(async input => {
    const id = trace.getActiveSpan().spanContext().spanId;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(trace.getActiveSpan().spanContext().spanId, id);
    observed.push(id); return [{ generated_text: input }];
  }));
  const parents = [];
  await Promise.all(['one', 'two'].map(label => t.provider.getTracer('test').startActiveSpan(label, async parent => {
    parents.push(parent); await p(label); parent.end();
  })));
  assert.equal(new Set(observed).size, 2);
  await t.flush();
  for (const id of observed) {
    const wire = t.wire().find(s => s.spanId === id);
    assert.ok(parents.some(p => p.spanContext().spanId === wire.parentSpanId && p.spanContext().traceId === wire.traceId));
  }
});

test('same error rethrow, one ended ERROR span, sync throws and promise rejections', async () => {
  const i = new TransformersInstrumentation();
  for (const asyncFailure of [false, true]) {
    const error = new TypeError('synthetic error');
    const count = t.memory.getFinishedSpans().length;
    const p = i.wrapPipeline(pipeline(() => { if (asyncFailure) return Promise.reject(error); throw error; }));
    if (asyncFailure) await assert.rejects(p('test'), e => e === error);
    else assert.throws(() => p('test'), e => e === error);
    assert.equal(t.memory.getFinishedSpans().length, count + 1);
    assert.equal(last().status.code, SpanStatusCode.ERROR);
    assert.equal(last().events[0].attributes['exception.message'], error.message);
  }
});

test('telemetry start, extraction and end failures never replace application result or retry', async () => {
  let calls = 0;
  const result = [{ generated_text: 'ok' }];
  const p = pipeline(() => { calls++; return result; });
  const i = new TransformersInstrumentation({ tracer: { startSpan() { throw new Error('export failed'); } } });
  assert.equal(i.wrapPipeline(p)('hello'), result); assert.equal(calls, 1);
  const j = new TransformersInstrumentation();
  const hostile = new Proxy({}, { getOwnPropertyDescriptor() { throw new Error('extraction failed'); } });
  const output = pipeline(() => { calls++; return hostile; });
  assert.equal(j.wrapPipeline(output)(hostile), hostile); assert.equal(calls, 2);
  const span = new Proxy({ spanContext: () => ({ traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1 }) }, {
    get(target, key) { return target[key] ?? (() => { throw new Error('telemetry failed'); }); },
  });
  const k = new TransformersInstrumentation({ tracer: { startSpan: () => span } });
  assert.equal(k.wrapPipeline(p)('hello'), result); assert.equal(calls, 3);
  const error = new Error('application error');
  assert.throws(() => k.wrapPipeline(pipeline(() => { calls++; throw error; }))('hello'), e => e === error);
  assert.equal(calls, 4);
});

test('wire privacy: individual content flags, nested chats, QA, vectors, errors and credential options', async () => {
  for (const flag of ['hideInputs', 'hideOutputs', 'hideInputText', 'hideInputMessages', 'hideOutputText', 'hideOutputMessages', 'hideErrorDetails', 'hideEmbeddingVectors']) {
    const sentinel = `PRIVATE_${flag}`;
    const i = new TransformersInstrumentation({ traceConfig: { [flag]: true } });
    await t.flush(); const count = t.wire().length;
    const inputHidden = flag.startsWith('hideInput');
    const outputHidden = flag.startsWith('hideOutput');
    const p = i.wrapPipeline(pipeline(async () => [{ generated_text: [{ role: 'assistant', content: outputHidden || inputHidden ? sentinel : 'safe output' }] }]));
    await p([[{ role: 'user', content: inputHidden ? sentinel : 'safe input' }]], { apiKey: sentinel, headers: { authorization: sentinel }, max_new_tokens: 2 });
    const error = new Error(sentinel);
    await assert.rejects(i.wrapPipeline(pipeline(async () => { throw error; }))('safe input'), e => e === error);
    await i.wrapPipeline(pipeline(async () => ({ dims: [1, 2], data: new Float32Array([1234567, 7654321]), secret: sentinel }), 'feature-extraction'))('safe input');
    if (inputHidden) await i.wrapPipeline(pipeline(async () => ({ answer: sentinel }), 'question-answering'))(sentinel, sentinel);
    await t.flush();
    const serialized = JSON.stringify(t.wire().slice(count));
    // Vector hiding alone does not imply hiding exception messages. All content flags do.
    if (flag !== 'hideEmbeddingVectors') assert.ok(!serialized.includes(sentinel), flag);
    assert.ok(!serialized.includes('1234567')); assert.ok(!serialized.includes('7654321'));
  }
});

test('FI environment privacy is resolved by shared config', async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  process.env.FI_HIDE_OUTPUTS = 'true';
  const i = new TransformersInstrumentation();
  delete process.env.FI_HIDE_INPUTS; delete process.env.FI_HIDE_OUTPUTS;
  await t.flush(); const count = t.wire().length;
  await i.wrapPipeline(pipeline(async () => [{ generated_text: 'ENV_SECRET' }]))('ENV_SECRET');
  await t.flush(); assert.ok(!JSON.stringify(t.wire().slice(count)).includes('ENV_SECRET'));
});

test('oversized/cyclic contents and hostile getters stay bounded without altering calls', async () => {
  const i = new TransformersInstrumentation();
  let getterCalls = 0;
  const object = { get content() { getterCalls++; throw new Error('do not inspect'); } };
  const circular = []; circular.push(circular);
  const p = i.wrapPipeline(pipeline(async v => [{ generated_text: v }]));
  for (const input of ['😀'.repeat(100000), circular, object]) {
    const result = await p(input, { secret: 'DO_NOT_CAPTURE' });
    assert.equal(result[0].generated_text, input);
    assert.ok(Buffer.byteLength(JSON.stringify(last().attributes)) < 17000);
  }
  assert.equal(getterCalls, 0);
  assert.ok(!JSON.stringify(last().attributes).includes('DO_NOT_CAPTURE'));
});

test('fail-open diagnostics omit private errors and tolerate a broken diagnostic logger', () => {
  const { diag, DiagLogLevel } = require('@opentelemetry/api');
  const messages = [];
  const logger = { error() {}, warn() {}, info() {}, verbose() {}, debug(...args) { messages.push(args); } };
  diag.setLogger(logger, DiagLogLevel.DEBUG);
  try {
    const instrumentor = new TransformersInstrumentation({ tracer: { startSpan() { throw new Error('PRIVATE_TELEMETRY_FAILURE'); } } });
    const wrapped = instrumentor.wrapPipeline(pipeline(() => 'unchanged'));
    assert.equal(wrapped(), 'unchanged');
    assert.ok(messages.some(args => args.join(' ').includes('telemetry operation skipped')));
    assert.ok(!JSON.stringify(messages).includes('PRIVATE_TELEMETRY_FAILURE'));
    logger.debug = () => { throw new Error('logger failed'); };
    assert.equal(wrapped(), 'unchanged');
  } finally { diag.disable(); }
});
