const { test } = require('node:test');
const assert = require('node:assert/strict');
const { VitestEvalsReporter } = require('../dist/index.js');

test('exports one trace batch with normalized parentage, timing, scores, and safe attributes', async () => {
  const batches = [];
  const exporter = {
    export(spans, callback) { batches.push(spans); callback({ code: 0 }); },
    shutdown: async () => {},
  };
  const reporter = new VitestEvalsReporter({ exporter });
  const module = { children: { *allTests() { yield {
    name: 'synthetic eval', meta: () => ({
      eval: { scores: [{ name: 'accuracy', score: 0.8 }] },
      harness: { run: { usage: { inputTokens: 10, outputTokens: 4, model: 'model-safe' }, traces: [{
        id: 'a'.repeat(32), spans: [
          { id: 'b'.repeat(16), name: 'run', kind: 'run', startedAt: '2026-01-01T00:00:00.000Z', finishedAt: '2026-01-01T00:00:01.000Z', status: 'ok', attributes: { 'gen_ai.request.model': 'safe-model', 'gen_ai.input.messages': 'private input' } },
          { id: 'c'.repeat(16), parentId: 'b'.repeat(16), name: 'model', kind: 'model', startedAt: '2026-01-01T00:00:00.100Z', finishedAt: '2026-01-01T00:00:00.800Z', status: 'error', error: { message: 'private error' } },
        ] }] } }
    }), result: () => ({ state: 'passed' }) }; } } };
  await reporter.onTestRunEnd([module]);
  assert.equal(batches.length, 1);
  assert.equal(batches[0].length, 2);
  const [root, child] = batches[0];
  assert.equal(root.spanContext().traceId, 'a'.repeat(32));
  assert.equal(child.parentSpanContext.spanId, root.spanContext().spanId);
  assert.deepEqual(root.startTime, [1767225600, 0]);
  assert.equal(root.duration[0], 1);
  assert.equal(root.attributes['eval.score.accuracy'], 0.8);
  assert.equal(root.attributes['gen_ai.usage.input_tokens'], 10);
  assert.equal(root.attributes['gen_ai.input.messages'], undefined);
  assert.equal(child.events.length, 0);
  assert.equal(child.status.code, 2);
});

test('uses provider trace ID and derives start from end and duration', async () => {
  let exported;
  const reporter = new VitestEvalsReporter({ exporter: { export(spans, cb) { exported = spans; cb({ code: 0 }); }, shutdown: async () => {} } });
  const traceId = 'e'.repeat(32);
  await reporter.onTestRunEnd([{ children: { *allTests() { yield {
    name: 'case', result: () => ({ state: 'passed' }),
    meta: () => ({ harness: { run: { traces: [{ spans: [{ name: 'timed', traceId, finishedAt: '2026-01-01T00:00:01.000Z', durationMs: 500 }] }] } } }),
  }; } } }]);
  assert.equal(exported[0].spanContext().traceId, traceId);
  assert.deepEqual(exported[0].startTime, [1767225600, 500000000]);
  assert.deepEqual(exported[0].duration, [0, 500000000]);
});

test('every exported span names its Future AGI project, so the backend accepts the batch', async () => {
  const run = async (options, env) => {
    const saved = process.env.FI_PROJECT_NAME;
    if (env === undefined) delete process.env.FI_PROJECT_NAME; else process.env.FI_PROJECT_NAME = env;
    try {
      let exported = [];
      const reporter = new VitestEvalsReporter({ ...options, exporter: { export(spans, cb) { exported = spans; cb({ code: 0 }); }, shutdown: async () => {} } });
      const module = { children: { *allTests() { yield { name: 'synthetic eval', meta: () => ({ eval: { scores: [{ name: 'accuracy', score: 1 }] } }), result: () => ({ state: 'passed' }) }; } } };
      await reporter.onTestRunEnd([module]);
      assert.ok(exported.length > 0);
      return exported.map(span => span.resource.attributes);
    } finally {
      if (saved === undefined) delete process.env.FI_PROJECT_NAME; else process.env.FI_PROJECT_NAME = saved;
    }
  };
  for (const attrs of await run({}, undefined)) {
    assert.equal(attrs.project_name, 'vitest-evals');
    assert.equal(attrs.project_type, 'observe');
  }
  for (const attrs of await run({}, 'synthetic-env-project')) assert.equal(attrs.project_name, 'synthetic-env-project');
  for (const attrs of await run({ projectName: 'synthetic-option-project' }, 'synthetic-env-project')) assert.equal(attrs.project_name, 'synthetic-option-project');
});
