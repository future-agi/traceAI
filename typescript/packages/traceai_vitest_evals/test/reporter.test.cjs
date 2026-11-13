const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
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

// A vitest-evals harness trace: the root span carries the harness name, a child span the model call.
const harnessTrace = (id, harness) => ({ id, name: harness, spans: [
  { id: `${id}:run`, name: harness, kind: 'run', status: 'ok', attributes: { 'gen_ai.workflow.name': harness } },
  { id: `${id}:model`, parentId: `${id}:run`, name: 'chat model-1', kind: 'model', status: 'ok' },
] });
const evalCase = (name, fullName, traces) => ({
  name, fullName, result: () => ({ state: 'passed' }),
  meta: () => ({ eval: { scores: [{ name: 'accuracy', score: 1 }] }, harness: { name: 'capital-app', run: { traces } } }),
});
const exportCases = async (...cases) => {
  let exported = [];
  const reporter = new VitestEvalsReporter({ exporter: { export(spans, cb) { exported = spans; cb({ code: 0 }); }, shutdown: async () => {} } });
  await reporter.onTestRunEnd([{ children: { *allTests() { yield* cases; } } }]);
  return exported;
};
const roots = spans => spans.filter(span => !span.parentSpanContext);

test('names each trace root after its test, so tests sharing one harness can be told apart', async () => {
  const spans = await exportCases(
    evalCase('answers the capital of France', 'capital answers > answers the capital of France', [harnessTrace('trace_1', 'capital-app')]),
    evalCase('does simple arithmetic', 'capital answers > does simple arithmetic', [harnessTrace('trace_2', 'capital-app')]),
  );
  assert.deepEqual(roots(spans).map(span => span.name), ['capital answers > answers the capital of France', 'capital answers > does simple arithmetic']);
  for (const root of roots(spans)) assert.equal(root.attributes['vitest.evals.original_name'], 'capital-app');
  const children = spans.filter(span => span.parentSpanContext);
  assert.deepEqual(children.map(span => span.name), ['chat model-1', 'chat model-1']);
  for (const child of children) assert.equal(child.attributes['vitest.evals.original_name'], undefined);
});

test('numbers the roots of a test with several traces and renames only the first root of a trace', async () => {
  const flat = { id: 'trace_4', spans: [
    { id: 'trace_4:run', name: 'capital-app', kind: 'run' },
    { id: 'trace_4:tool', name: 'lookup tool', kind: 'tool' },
  ] };
  const spans = await exportCases(evalCase('answers the capital of France', 'capital answers > answers the capital of France', [harnessTrace('trace_3', 'capital-app'), flat]));
  const exportedRoots = roots(spans);
  assert.deepEqual(exportedRoots.map(span => span.name), [
    'capital answers > answers the capital of France (1/2)',
    'capital answers > answers the capital of France (2/2)',
    'lookup tool',
  ]);
  assert.equal(exportedRoots[0].attributes['vitest.evals.original_name'], 'capital-app');
  assert.equal(exportedRoots[1].attributes['vitest.evals.original_name'], 'capital-app');
  assert.equal(exportedRoots[2].attributes['vitest.evals.original_name'], undefined);
});

test('a test without a harness trace is still exported under its test name', async () => {
  const scored = () => ({ eval: { scores: [{ name: 'accuracy', score: 1 }] } });
  const spans = await exportCases(
    { name: 'case', fullName: 'suite > case', meta: scored, result: () => ({ state: 'passed' }) },
    { name: 'bare case', meta: scored, result: () => ({ state: 'failed' }) },
  );
  assert.deepEqual(spans.map(span => span.name), ['suite > case', 'bare case']);
  for (const span of spans) assert.equal(span.attributes['vitest.evals.original_name'], undefined);
  assert.equal(spans[1].status.code, 2);
});

// A loopback OTLP/HTTP collector that records the path of every export request.
const withCollector = async (send) => {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    requests.push({ path: req.url, headers: req.headers });
    res.writeHead(200, { 'content-type': 'application/json', connection: 'close' }); res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await send(`http://127.0.0.1:${server.address().port}`); } finally { await new Promise(resolve => server.close(resolve)); }
  return requests;
};
const exportWithBaseUrl = async (options, baseUrl) => {
  const saved = process.env.FI_BASE_URL;
  if (baseUrl === undefined) delete process.env.FI_BASE_URL; else process.env.FI_BASE_URL = baseUrl;
  try {
    const reporter = new VitestEvalsReporter({ apiKey: 'synthetic-key', secretKey: 'synthetic-secret', ...options });
    try {
      await reporter.onTestRunEnd([{ children: { *allTests() { yield { name: 'case', meta: () => ({ eval: { scores: [{ name: 'accuracy', score: 1 }] } }), result: () => ({ state: 'passed' }) }; } } }]);
    } finally { await reporter.onClose(); }
  } finally {
    if (saved === undefined) delete process.env.FI_BASE_URL; else process.env.FI_BASE_URL = saved;
  }
};

test('without an endpoint option, FI_BASE_URL sends traces to its collector path, as in fi-core', async () => {
  for (const [suffix, expected] of [['/', '/tracer/v1/traces'], ['/self-hosted', '/self-hosted/tracer/v1/traces']]) {
    const requests = await withCollector(origin => exportWithBaseUrl({}, `${origin}${suffix}`));
    assert.deepEqual(requests.map(request => request.path), [expected]);
    assert.equal(requests[0].headers['x-api-key'], 'synthetic-key');
  }
});

test('an endpoint option takes precedence over FI_BASE_URL', async () => {
  const requests = await withCollector(origin => exportWithBaseUrl({ endpoint: `${origin}/custom/v1/traces` }, `${origin}/from-env`));
  assert.deepEqual(requests.map(request => request.path), ['/custom/v1/traces']);
});
