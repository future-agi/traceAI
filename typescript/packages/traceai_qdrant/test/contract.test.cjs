// Run after build: node --test test/contract.test.cjs
// QDRANT_TEST_ROOT / QDRANT_TEST_SECOND_ROOT select independent npm installs.
// Only localhost is used: real SDK HTTP requests and real OTLP/HTTP export.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createServer } = require('node:http');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const { join } = require('node:path');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { QdrantInstrumentation, isPatched } = require('../dist/src');

const requireClient = root => root ? createRequire(join(root, 'package.json'))('@qdrant/js-client-rest') : require('@qdrant/js-client-rest');
const point = { id: 1, score: 0.8, payload: { text: 'private-result-payload' }, vector: [0.87654321] };
const query = [0.123456789];
const attrs = span => Object.fromEntries(span.attributes.map(({ key, value }) => [key, value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue]));

test('real Qdrant client loader, lifecycle, and OTLP contract', async t => {
  const wire = [];
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    res.setHeader('content-type', 'application/json');
    if (req.url === '/v1/traces') {
      wire.push(JSON.parse(raw));
      res.end('{}');
      return;
    }
    const body = raw ? JSON.parse(raw) : {};
    requests.push({ url: req.url, body });
    if (req.url.includes('/missing/')) {
      res.statusCode = 404;
      res.end(JSON.stringify({ status: { error: 'synthetic missing collection' } }));
      return;
    }
    let result;
    if (req.url.endsWith('/points/query/batch')) result = [{ points: [point] }, { points: [point, point] }];
    else if (req.url.endsWith('/points/query/groups')) result = { groups: [{ id: 'a', hits: [point, point] }, { id: 'b', hits: [point] }] };
    else if (req.url.endsWith('/points/query')) result = { points: [point] };
    else if (req.url.endsWith('/points/search')) result = [point];
    else if (req.url.endsWith('/points/scroll')) result = { points: [point], next_page_offset: null };
    else if (req.url.endsWith('/points/count')) result = { count: 4 };
    else if (req.url.endsWith('/points/delete') || req.method === 'PUT') result = { operation_id: 1, status: 'completed' };
    else if (req.url.endsWith('/points')) result = [point];
    else if (req.url === '/') { res.end(JSON.stringify({ title: 'qdrant', version: '1.20.0' })); return; }
    else result = { vectors_count: 4, config: { params: { vectors: { size: 1 } } } };
    res.end(JSON.stringify({ result, status: 'ok', time: 0.001 }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const url = `http://127.0.0.1:${server.address().port}`;
  const memory = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(memory),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${url}/v1/traces` })),
  ] });
  provider.register();
  const instrumentation = new QdrantInstrumentation();
  instrumentation.setTracerProvider(provider);
  t.after(async () => {
    instrumentation.disable();
    await provider.shutdown();
    context.disable();
    trace.disable();
    await new Promise(resolve => server.close(resolve));
  });

  // Crucially, load the real package AFTER instrumentation. No manual patch is
  // used for the first copy: this exercises require-in-the-middle itself.
  const module = requireClient(process.env.QDRANT_TEST_ROOT);
  const client = new module.QdrantClient({ url, checkCompatibility: false });
  // 1.20 removed legacy search; the oldest supported release has no query.
  const operation = typeof client.query === 'function' ? 'query' : 'search';
  const params = { [operation === 'query' ? 'query' : 'vector']: query, limit: 2, with_payload: true, with_vector: true };
  const search = () => client[operation]('documents', params);

  await t.test('automatic loading creates one parented span and forwards the request', async () => {
    memory.reset();
    const parent = provider.getTracer('contract').startSpan('parent');
    const result = await context.with(trace.setSpan(context.active(), parent), search);
    assert.deepEqual(result, operation === 'query' ? { points: [point] } : [point]);
    const spans = memory.getFinishedSpans();
    assert.equal(spans.length, 1);
    assert.equal(spans[0].name, `qdrant ${operation}`);
    assert.equal(spans[0].parentSpanContext.spanId, parent.spanContext().spanId);
    assert.equal(spans[0].attributes['db.vector.results.count'], 1);
    assert.deepEqual(requests.at(-1).body[operation === 'query' ? 'query' : 'vector'], query);
    parent.end();
  });

  await t.test('disable stops capture; repeated enable does not duplicate spans', async () => {
    for (let cycle = 0; cycle < 3; cycle++) {
      instrumentation.disable();
      assert.equal(isPatched(), false);
      const original = module.QdrantClient.prototype.search;
      memory.reset();
      await search();
      assert.equal(memory.getFinishedSpans().length, 0);
      instrumentation.enable();
      instrumentation.manuallyInstrument(module); // same target remains idempotent
      instrumentation.enable();
      await search();
      assert.equal(memory.getFinishedSpans().length, 1);
      instrumentation.disable();
      assert.equal(module.QdrantClient.prototype.search, original);
      instrumentation.enable();
    }
  });

  await t.test('existing write/read operations each emit exactly one span', async () => {
    memory.reset();
    await client.upsert('documents', { points: [{ id: 1, vector: query }] });
    await client.delete('documents', { points: [1] });
    await client.retrieve('documents', { ids: [1] });
    await client.scroll('documents', { limit: 2 });
    await client.count('documents', { exact: true });
    await client.getCollection('documents');
    assert.deepEqual(memory.getFinishedSpans().map(span => span.name), [
      'qdrant upsert', 'qdrant delete', 'qdrant retrieve', 'qdrant scroll', 'qdrant count', 'qdrant get_collection',
    ]);
  });

  if (typeof client.query === 'function') {
    await t.test('modern query/batch/group calls use point counts, not envelope lengths', async () => {
      memory.reset();
      assert.deepEqual(await client.query('documents', { query, limit: 0 }), { points: [point] });
      assert.deepEqual(await client.queryBatch('documents', { searches: [{ query, limit: 1 }, { query, limit: 2 }] }), [{ points: [point] }, { points: [point, point] }]);
      assert.deepEqual(await client.queryGroups('documents', { query, group_by: 'category', group_size: 2, limit: 2 }), { groups: [{ id: 'a', hits: [point, point] }, { id: 'b', hits: [point] }] });
      const spans = memory.getFinishedSpans();
      assert.deepEqual(spans.map(span => span.name), ['qdrant query', 'qdrant query_batch', 'qdrant query_groups']);
      assert.deepEqual(spans.map(span => span.attributes['db.vector.results.count']), [1, 3, 3]);
      assert.equal(spans[0].attributes['db.vector.query.top_k'], 0);
      assert.equal(spans[1].attributes['db.vector.query.batch_size'], 2);
      assert.equal(spans[1].attributes['db.vector.query.top_k'], undefined);
      assert.equal(spans[2].attributes['db.vector.query.group_limit'], 2);
      assert.equal(spans[2].attributes['db.vector.query.group_size'], 2);
      assert.equal(spans[2].attributes['db.vector.query.top_k'], undefined);
    });
  }

  await t.test('rejections close a single ERROR span without swallowing the SDK error', async () => {
    memory.reset();
    await assert.rejects(client[operation]('missing', params));
    const spans = memory.getFinishedSpans();
    assert.equal(spans.length, 1);
    assert.equal(spans[0].status.code, SpanStatusCode.ERROR);
    assert.equal(spans[0].events[0].name, 'exception');
  });

  if (process.env.QDRANT_TEST_SECOND_ROOT) {
    await t.test('two real module copies survive the same lifecycle', async () => {
      const second = requireClient(process.env.QDRANT_TEST_SECOND_ROOT);
      assert.notEqual(second.QdrantClient, module.QdrantClient);
      const secondClient = new second.QdrantClient({ url, checkCompatibility: false });
      const both = async () => {
        await search();
        if (typeof secondClient.query === 'function') await secondClient.query('documents', { query, limit: 1 });
        else await secondClient.search('documents', { vector: query, limit: 1 });
      };
      memory.reset();
      await both();
      assert.equal(memory.getFinishedSpans().length, 2);
      instrumentation.disable();
      memory.reset();
      await both();
      assert.equal(memory.getFinishedSpans().length, 0);
      instrumentation.enable();
      await both();
      assert.equal(memory.getFinishedSpans().length, 2);
    });
  }

  await t.test('OTLP wire contains semantic spans without raw query/result vectors or payloads', async () => {
    await provider.forceFlush();
    const spans = wire.flatMap(request => request.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    const searchSpan = spans.find(span => span.name === `qdrant ${operation}`);
    assert.ok(searchSpan);
    assert.equal(searchSpan.kind, 3); // OTLP CLIENT enum (API CLIENT is 2)
    assert.equal(attrs(searchSpan)['db.system'], 'qdrant');
    assert.equal(attrs(searchSpan)['db.operation.name'], operation);
    assert.equal(attrs(searchSpan)['db.namespace'], 'documents');
    assert.equal(attrs(searchSpan)['fi.span.kind'], 'VECTOR_DB');
    assert.equal(Number(attrs(searchSpan)['db.vector.results.count']), 1);
    if (typeof client.queryBatch === 'function') {
      for (const name of ['qdrant query_batch', 'qdrant query_groups']) {
        const span = spans.find(span => span.name === name);
        assert.ok(span, `missing ${name} on the wire`);
        assert.equal(Number(attrs(span)['db.vector.results.count']), 3);
      }
    }
    const serialized = JSON.stringify(wire);
    for (const privateValue of ['private-result-payload', '0.123456789', '0.87654321']) assert.ok(!serialized.includes(privateValue));
  });
});
