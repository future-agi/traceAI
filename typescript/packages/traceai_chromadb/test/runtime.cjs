// Real npm SDKs and real OTLP/HTTP export; only the Chroma server is simulated.
// CHROMADB_TEST_ROOT selects an isolated npm install for the compatibility matrix.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { OTLPTraceExporter } = coreRequire('@opentelemetry/exporter-trace-otlp-http');
const { ChromaDBInstrumentation, isPatched } = require('../dist/src');
const sdkRequire = process.env.CHROMADB_TEST_ROOT
  ? createRequire(path.join(process.env.CHROMADB_TEST_ROOT, 'package.json')) : require;
const format = process.env.CHROMADB_TEST_FORMAT || 'cjs';
const mode = process.env.CHROMADB_TEST_MODE || 'manual';

// Locate the package without importing it before automatic instrumentation.
let sdkDir = path.dirname(sdkRequire.resolve('chromadb'));
while (!fs.existsSync(path.join(sdkDir, 'package.json'))) sdkDir = path.dirname(sdkDir);
const manifest = JSON.parse(fs.readFileSync(path.join(sdkDir, 'package.json'), 'utf8'));
const version = manifest.version;
const major = Number(version.split('.')[0]);
const early = version === '1.1.0';

function attributes(span) {
  return Object.fromEntries(span.attributes.map(({ key, value }) =>
    [key, value.stringValue ?? value.intValue ?? value.boolValue]));
}

function ownMethods(prototype) {
  const saved = [];
  for (let owner = prototype; owner && owner !== Object.prototype; owner = Object.getPrototypeOf(owner)) {
    for (const name of Object.getOwnPropertyNames(owner)) {
      const descriptor = Object.getOwnPropertyDescriptor(owner, name);
      if (typeof descriptor.value === 'function') saved.push({ owner, name, descriptor });
    }
  }
  return saved;
}

function restored(saved) {
  for (const { owner, name, descriptor } of saved) {
    assert.deepEqual(Object.getOwnPropertyDescriptor(owner, name), descriptor, `restore ${name}`);
  }
}

test(`Chroma ${version} ${format} ${mode}: factories, spans, lifecycle and OTLP`, async (t) => {
  const wire = [];
  const requests = [];
  let failCount = false;
  const model = { id: '00000000-0000-0000-0000-000000000001', name: 'test_collection',
    metadata: {}, configuration_json: { embedding_function: { type: 'known', name: 'offline', config: {} } },
    tenant: 'default_tenant', database: 'default_database' };
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/v1/traces') {
      wire.push(body);
      res.end('{}');
      return;
    }
    requests.push({ url: req.url, body });
    const route = req.url.split('?')[0];
    let data;
    if (route.endsWith('/count')) {
      if (failCount) { res.statusCode = 400; data = { error: 'InvalidArgumentError', message: 'synthetic failure' }; }
      else data = 2;
    } else if (route.endsWith('/query') || route.endsWith('/search')) {
      const size = body.searches?.length || body.query_embeddings?.length || 1;
      data = { ids: Array.from({ length: size }, () => ['doc1', 'doc2']),
        documents: [['PRIVATE_DOCUMENT', 'PRIVATE_DOCUMENT']], metadatas: [[{}, {}]],
        distances: [[0.1, 0.2]], scores: [[0.1, 0.2]], include: ['documents'], select: [] };
    } else if (route.endsWith('/get')) {
      data = { ids: ['doc1', 'doc2'], documents: ['PRIVATE_DOCUMENT', 'PRIVATE_DOCUMENT'],
        metadatas: [{}, {}], include: ['documents'] };
    } else if (/\/(add|update|upsert|delete)$/.test(route)) {
      if (route.endsWith('/add')) res.statusCode = 201;
      data = true;
    }
    else if (route.endsWith('/collections') && req.method === 'GET') data = [model];
    else if (route.includes('/collections/')) data = model;
    else if (route.endsWith('/collections')) data = model;
    else if (route.endsWith('/pre-flight-checks')) data = { max_batch_size: 1000 };
    else if (route.endsWith('/identity')) data = { tenant: 'default_tenant', databases: ['default_database'] };
    else if (route.endsWith('/default_tenant')) data = { name: 'default_tenant' };
    else if (route.endsWith('/default_database')) data = { id: model.id, name: 'default_database', tenant: 'default_tenant' };
    else { res.statusCode = 404; data = { message: `Unexpected fixture path ${route}` }; }
    res.end(JSON.stringify(data));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const port = server.address().port;
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` })),
  ] });
  provider.register();
  const inst = new ChromaDBInstrumentation({
    instrumentationConfig: { enabled: false }, traceConfig: { hideErrorDetails: true },
  });
  inst.setTracerProvider(provider);
  t.after(async () => { inst.disable(); await provider.shutdown(); trace.disable(); context.disable(); });
  let sdk;
  if (mode === 'auto') inst.enable();
  if (format === 'esm') {
    const loader = path.join(process.env.CHROMADB_TEST_ROOT || __dirname, 'load-chromadb.mjs');
    if (process.env.CHROMADB_TEST_ROOT) fs.copyFileSync(path.join(__dirname, 'load-chromadb.mjs'), loader);
    sdk = await import(pathToFileURL(loader));
  } else sdk = sdkRequire('chromadb');
  const before = [...ownMethods(sdk.Collection?.prototype), ...ownMethods(sdk.CollectionHandle?.prototype),
    ...ownMethods(sdk.ChromaClient?.prototype)];
  if (mode === 'manual') {
    inst.enable();
    inst.manuallyInstrument(sdk);
    inst.manuallyInstrument(sdk); // Idempotent, including both import formats.
  }
  assert.equal(isPatched(), true);

  const once = async (name, fn, expected = {}) => {
    exporter.reset();
    const result = await fn();
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 1, `${name}: ${spans.map(s => s.name)}`);
    const span = spans[0];
    assert.equal(span.name, `chroma ${name}`);
    assert.equal(span.kind, 2);
    assert.equal(span.status.code, SpanStatusCode.OK);
    assert.equal(span.attributes['db.system'], 'chromadb');
    assert.equal(span.attributes['fi.span.kind'], 'VECTOR_DB');
    for (const [key, value] of Object.entries(expected)) assert.equal(span.attributes[key], value);
    return result;
  };

  let collection;
  if (early) {
    // v1.1's API is positional. Exercise the real class and preserve all args.
    const calls = [];
    const api = {
      count: async () => ({ data: 2 }),
      getNearestNeighbors: async (params) => { calls.push(params); return { data: { ids: [['doc1']] } }; },
    };
    collection = new sdk.Collection('test_collection', api);
    await t.test('legacy positional writes, reads and envelopes preserve SDK values', async () => {
      const returned = { data: { ids: ['doc1', 'doc2'] } };
      api.add = async params => { calls.push(params); return returned; };
      api.get = async params => { calls.push(params); return returned; };
      api._delete = async params => { calls.push(params); return returned; };
      const ids = ['doc1', 'doc2'];
      const vectors = [[0.1, 0.2], [0.3, 0.4]];
      const docs = ['first', 'second'];
      assert.equal(await once('add', () => collection.add(ids, vectors, undefined, docs, false), {
        'db.vector.upsert.count': 2, 'db.vector.upsert.dimensions': 2, 'db.vector.documents.count': 2,
      }), returned);
      assert.equal(calls.at(-1).addEmbedding.ids, ids);
      assert.equal(calls.at(-1).addEmbedding.embeddings, vectors);
      assert.equal(calls.at(-1).addEmbedding.documents, docs);
      assert.equal(calls.at(-1).addEmbedding.increment_index, false);
      for (const limit of [2, 0]) {
        assert.equal(await once('get', () => collection.get(ids, undefined, limit, 0), {
          'db.vector.get.ids_count': 2, 'db.vector.query.top_k': limit, 'db.vector.results.count': 2,
        }), returned);
        assert.equal(calls.at(-1).getEmbedding.ids, ids);
        assert.equal(calls.at(-1).getEmbedding.limit, limit);
        assert.equal(calls.at(-1).getEmbedding.offset, 0);
      }
      await once('get', () => collection.get(undefined, undefined, 3), {
        'db.vector.query.top_k': 3, 'db.vector.results.count': 2,
      });
      for (const limit of [3, 0, undefined]) {
        assert.equal(await once('peek', () => collection.peek(limit), {
          'db.vector.query.top_k': limit ?? 10, 'db.vector.results.count': 2,
        }), returned);
        assert.equal(calls.at(-1).getEmbedding.limit, limit ?? 10);
      }
      assert.equal(await once('delete', () => collection.delete(ids), {
        'db.vector.delete.count': 2,
      }), returned);
      assert.equal(calls.at(-1).deleteEmbedding.ids, ids);
    });
    const embeddings = [[0.1, 0.2]];
    const where = { field: 'value' };
    for (const nResults of [3, undefined, 0]) {
      await once('query', () => collection.query(embeddings, nResults, where), {
        'db.vector.query.top_k': nResults ?? 10,
        'db.vector.query.filter': JSON.stringify(where),
        'db.vector.results.count': 1,
      });
      assert.equal(calls.at(-1).queryEmbedding.n_results, nResults ?? 10);
      assert.equal(calls.at(-1).queryEmbedding.query_embeddings, embeddings);
      assert.equal(calls.at(-1).queryEmbedding.where, where);
    }
    await once('query', () => collection.query(embeddings), {
      'db.vector.query.top_k': 10,
      'db.vector.query.filter': undefined,
    });
  } else {
    const client = new sdk.ChromaClient(major >= 3
      ? { host: '127.0.0.1', port, ssl: false, tenant: 'default_tenant', database: 'default_database' }
      : { path: `http://127.0.0.1:${port}` });
    class OfflineEmbedding {
      name = 'offline';
      async generate() { throw new Error('Embedding API must not run'); }
      getConfig() { return {}; }
      static buildFromConfig() { return new OfflineEmbedding(); }
      buildFromConfig() { return new OfflineEmbedding(); }
    }
    sdk.registerEmbeddingFunction?.('offline', OfflineEmbedding);
    const embeddingFunction = new OfflineEmbedding();
    for (const factory of ['createCollection', 'getCollection', 'getOrCreateCollection',
      'getCollectionById', 'getCollectionByCrn', 'getCollections', 'listCollections', 'collection']) {
      if (typeof client[factory] !== 'function') continue;
      let arg = { name: 'test_collection', embeddingFunction };
      if (factory === 'getCollectionById' || factory === 'collection') arg = model.id;
      if (factory === 'getCollectionByCrn') arg = 'crn:default_tenant:default_database:test_collection';
      if (factory === 'getCollections') arg = [{ name: 'test_collection', embeddingFunction }];
      if (factory === 'listCollections') arg = {};
      const pending = client[factory](arg);
      if (factory === 'collection') assert.equal(typeof pending.then, 'undefined');
      const result = await pending;
      const value = Array.isArray(result) ? result[0] : result;
      // Recent listCollections returns names rather than Collection instances.
      if (!value || typeof value.count !== 'function') continue;
      await once('count', () => value.count(), { 'db.vector.results.count': 2 });
      await once('query', () => value.query({ queryEmbeddings: [[0.1, 0.2]], nResults: 2,
        where: { field: 'value' }, include: ['metadatas', 'embeddings'] }), {
        'db.vector.query.top_k': 2, 'db.vector.results.count': 2,
        'db.vector.query.filter': JSON.stringify({ field: 'value' }),
        'db.vector.query.include_metadata': true, 'db.vector.query.include_vectors': true,
      });
      assert.equal(requests.at(-1).body.n_results, 2);
      assert.deepEqual(requests.at(-1).body.where, { field: 'value' });
      assert.deepEqual(requests.at(-1).body.include, ['metadatas', 'embeddings']);
      if (typeof value.search === 'function') {
        await once('search', () => value.search([{}, {}], { readLevel: 'index_and_wal' }),
          { 'db.vector.search.count': 2, 'db.vector.results.count': 4 });
        assert.equal(requests.at(-1).body.read_level, 'index_and_wal', 'preserve search second argument');
      }
      if (factory === 'getCollection') collection = value;
    }
    assert.ok(collection, 'a real factory-created collection is required');
    for (const op of ['add', 'update', 'upsert']) {
      await once(op, () => collection[op]({ ids: ['doc1'], embeddings: [[0.1, 0.2]], documents: ['PRIVATE_DOCUMENT'] }), {
        'db.vector.upsert.count': 1,
        ...(op !== 'update' ? { 'db.vector.upsert.dimensions': 2 } : {}),
        ...(op === 'add' ? { 'db.vector.documents.count': 1 } : {}),
      });
    }
    await once('get', () => collection.get({ ids: ['doc1'] }), { 'db.vector.get.ids_count': 1 });
    await once('delete', () => collection.delete({ ids: ['doc1'] }), { 'db.vector.delete.count': 1 });
    await once('peek', () => collection.peek({ limit: 2 }));
    if (major < 3) {
      // v1/v2 accept scalar records; v3 requires arrays. Use the real SDK's
      // normalizer and compare its outgoing request to the span metadata.
      for (const op of ['add', 'update', 'upsert', 'get', 'delete']) {
        await t.test(`scalar ${op} metadata matches a single SDK record`, async () => {
          const write = ['add', 'update', 'upsert'].includes(op);
          const params = write
            ? { ids: 'single-id', embeddings: [0.1, 0.2, 0.3], documents: 'single document' }
            : { ids: 'single-id' };
          const snapshot = structuredClone(params);
          exporter.reset();
          await collection[op](params);
          assert.deepEqual(params, snapshot, 'instrumentation must not mutate SDK arguments');
          const request = requests.at(-1);
          assert.ok(request.url.endsWith(`/${op}`));
          assert.deepEqual(request.body.ids, ['single-id']);
          if (write) {
            assert.deepEqual(request.body.embeddings, [[0.1, 0.2, 0.3]]);
            assert.deepEqual(request.body.documents, ['single document']);
          }
          const spans = exporter.getFinishedSpans();
          assert.equal(spans.length, 1);
          assert.equal(spans[0].name, `chroma ${op}`);
          assert.equal(spans[0].status.code, SpanStatusCode.OK);
          const attrs = spans[0].attributes;
          const countKey = write ? 'db.vector.upsert.count'
            : op === 'get' ? 'db.vector.get.ids_count' : 'db.vector.delete.count';
          assert.equal(attrs[countKey], request.body.ids.length);
          if (op === 'add' || op === 'upsert') assert.equal(attrs['db.vector.upsert.dimensions'], 3);
          if (op === 'add') assert.equal(attrs['db.vector.documents.count'], 1);
        });
      }
    }
    await t.test('object peek defaults the limit and preserves explicit zero', async () => {
      for (const params of [{}, { limit: undefined }, { limit: 0 }, { limit: 3 }]) {
        const snapshot = { ...params };
        await once('peek', () => collection.peek(params), {
          'db.vector.query.top_k': params.limit ?? 10, 'db.vector.results.count': 2,
        });
        assert.deepEqual(params, snapshot, 'instrumentation must not mutate SDK arguments');
        assert.equal(requests.at(-1).body.limit, params.limit ?? 10);
      }
    });
    if (typeof collection.fork === 'function') {
      const fork = await collection.fork({ name: 'fork_collection' });
      await once('count', () => fork.count());
    }
    await t.test('batched query counts every returned row', async () => {
      const result = await once('query', () => collection.query({
        queryEmbeddings: [[0.1, 0.2], [0.3, 0.4]], nResults: 2,
      }), { 'db.vector.results.count': 4 });
      assert.equal(result.ids.flat().length, 4);
    });
    await t.test('embedding rejections preserve null and mark the span as error', async () => {
      const generate = embeddingFunction.generate;
      try {
        embeddingFunction.generate = async () => { throw null; };
        exporter.reset();
        let caught = Symbol('not thrown');
        try { await collection.query({ queryTexts: ['synthetic-input'] }); } catch (e) { caught = e; }
        assert.equal(caught, null);
        assert.equal(exporter.getFinishedSpans().length, 1);
        assert.equal(exporter.getFinishedSpans()[0].status.code, SpanStatusCode.ERROR);
      } finally { embeddingFunction.generate = generate; }
    });
    await t.test('embedding database operations retain their own child spans', async () => {
      const generate = embeddingFunction.generate;
      try {
        embeddingFunction.generate = async () => { await collection.count(); return [[0.1, 0.2]]; };
        exporter.reset();
        const begin = requests.length;
        await collection.query({ queryTexts: ['synthetic-input'] });
        assert.equal(requests.slice(begin).filter(r => r.url.split('?')[0].endsWith('/count')).length, 1);
        assert.equal(requests.slice(begin).filter(r => r.url.split('?')[0].endsWith('/query')).length, 1);
        const spans = exporter.getFinishedSpans();
        assert.deepEqual(spans.map(s => s.name).sort(), ['chroma count', 'chroma query']);
        const count = spans.find(s => s.name === 'chroma count');
        const query = spans.find(s => s.name === 'chroma query');
        assert.equal(count.parentSpanContext.spanId, query.spanContext().spanId);
      } finally { embeddingFunction.generate = generate; }
    });
    await t.test('independent nested queries are not mistaken for superclass delegation', async () => {
      const generate = embeddingFunction.generate;
      try {
        embeddingFunction.generate = async () => {
          await collection.query({ queryEmbeddings: [[0.3, 0.4]] });
          return [[0.1, 0.2]];
        };
        exporter.reset();
        await collection.query({ queryTexts: ['synthetic-input'] });
        const spans = exporter.getFinishedSpans();
        assert.deepEqual(spans.map(s => s.name), ['chroma query', 'chroma query']);
        assert.equal(spans[0].parentSpanContext.spanId, spans[1].spanContext().spanId);
      } finally { embeddingFunction.generate = generate; }
    });
    // A thrown SDK error is propagated and recorded once.
    failCount = true;
    exporter.reset();
    await assert.rejects(() => collection.count());
    assert.equal(exporter.getFinishedSpans().length, 1);
    assert.equal(exporter.getFinishedSpans()[0].status.code, SpanStatusCode.ERROR);
    failCount = false;
  }

  for (const config of ['environment', 'explicit-true', 'explicit-false']) {
    await t.test(`${config}: input hiding applies to collection names and filters on the wire`, async () => {
      const previous = process.env.FI_HIDE_INPUTS;
      let privacyInst;
      const hideInputs = config !== 'explicit-false';
      try {
        // Explicit options take precedence over the opposite environment value.
        process.env.FI_HIDE_INPUTS = String(config !== 'explicit-true');
        inst.disable();
        privacyInst = config === 'environment' ? inst : new ChromaDBInstrumentation({
          traceConfig: { hideInputs, hideErrorDetails: true },
        });
        privacyInst.setTracerProvider(provider);
        privacyInst.enable();
        privacyInst.manuallyInstrument(sdk);
        // v3 factories can return an unexported class; rediscover this real instance.
        privacyInst.manuallyInstrument({ Collection: collection.constructor });
        const where = { private_note: 'SYNTHETIC_PRIVATE_FILTER' };
        const name = collection.name;
        assert.ok(name, 'exercise a named collection, not an anonymous handle');
        await provider.forceFlush();
        const wireStart = wire.length;
        await once('query', () => early
          ? collection.query([[0.1, 0.2]], 2, where)
          : collection.query({ queryEmbeddings: [[0.1, 0.2]], nResults: 2, where }), {
          'db.vector.query.filter': hideInputs ? undefined : JSON.stringify(where),
          'db.namespace': hideInputs ? undefined : name,
          'db.vector.collection.name': hideInputs ? undefined : name,
          'db.vector.query.top_k': 2,
          'db.vector.results.count': early ? 1 : 2,
        });
        if (!early) assert.deepEqual(requests.at(-1).body.where, where);
        assert.equal(collection.name, name, 'privacy must not change the collection');
        await provider.forceFlush();
        const exported = wire.slice(wireStart).flatMap(p => p.resourceSpans)
          .flatMap(r => r.scopeSpans).flatMap(s => s.spans);
        assert.equal(exported.length, 1);
        const attrs = attributes(exported[0]);
        assert.equal(attrs['db.namespace'], hideInputs ? undefined : name);
        assert.equal(attrs['db.vector.collection.name'], hideInputs ? undefined : name);
        assert.equal(attrs['db.system'], 'chromadb');
        assert.equal(attrs['db.operation.name'], 'query');
        assert.equal(JSON.stringify(exported).includes('SYNTHETIC_PRIVATE_FILTER'), !hideInputs);
      } finally {
        privacyInst?.disable();
        if (previous === undefined) delete process.env.FI_HIDE_INPUTS;
        else process.env.FI_HIDE_INPUTS = previous;
        inst.enable();
      }
    });
  }
  // Existing objects, not just future factory results, follow enable/disable.
  inst.disable();
  assert.equal(isPatched(), false);
  if (mode === 'manual') restored(before);
  const disabledMethods = ownMethods(Object.getPrototypeOf(collection));
  exporter.reset();
  await collection.count();
  assert.equal(exporter.getFinishedSpans().length, 0);
  inst.enable();
  await once('count', () => collection.count());
  inst.disable();
  restored(disabledMethods);
  inst.enable();
  const parent = provider.getTracer('test').startSpan('parent');
  exporter.reset();
  await context.with(trace.setSpan(context.active(), parent), () => collection.count());
  const child = exporter.getFinishedSpans()[0];
  assert.equal(child.parentSpanContext.spanId, parent.spanContext().spanId);
  parent.end();
  if (!early) {
    context.disable();
    await once('peek', () => collection.peek({ limit: 2 }));
    exporter.reset();
    await Promise.all([collection.count(), collection.count()]);
    assert.equal(exporter.getFinishedSpans().length, 2, 'concurrent operations are not suppressed');
  }
  await provider.forceFlush();
  const wireSpans = wire.flatMap(p => p.resourceSpans).flatMap(r => r.scopeSpans).flatMap(s => s.spans);
  const countSpan = wireSpans.find(s => s.name === 'chroma count');
  assert.ok(countSpan, 'real OTLP/HTTP span must reach loopback collector');
  assert.equal(countSpan.kind, 3, 'OTLP CLIENT kind');
  assert.match(countSpan.traceId, /^[0-9a-f]{32}$/);
  assert.match(countSpan.spanId, /^[0-9a-f]{16}$/);
  const wireChild = wireSpans.find(s => s.spanId === child.spanContext().spanId);
  assert.equal(wireChild.parentSpanId, parent.spanContext().spanId);
  assert.equal(attributes(countSpan)['db.system'], 'chromadb');
  assert.equal(attributes(countSpan)['fi.span.kind'], 'VECTOR_DB');
  assert.equal(JSON.stringify(wire).includes('PRIVATE_DOCUMENT'), false, 'do not capture document content');
  assert.equal(JSON.stringify(wire).includes('synthetic failure'), false, 'respect hideErrorDetails on the wire');
});

test('empty modules, independent modules, owner isolation and pending factories', async () => {
  class ManualOnlyInstrumentation extends ChromaDBInstrumentation { init() { return []; } }
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const inst = new ManualOnlyInstrumentation({ traceConfig: { hideErrorDetails: true } });
  inst.setTracerProvider(provider);
  const other = new ManualOnlyInstrumentation();
  try {
    inst.manuallyInstrument({});
    assert.equal(isPatched(), false, 'do not claim a no-op patch succeeded');
    class First { async count() { return 1; } }
    class Second { async count() { return 2; } }
    const originals = [First.prototype.count, Second.prototype.count];
    inst.manuallyInstrument({ Collection: First });
    inst.manuallyInstrument({ Collection: Second });
    assert.notEqual(First.prototype.count, originals[0]);
    assert.notEqual(Second.prototype.count, originals[1]);
    const once = First.prototype.count;
    other.manuallyInstrument({ Collection: First });
    other.disable();
    assert.equal(First.prototype.count, once, 'another instance cannot unpatch our wrapper');
    let failure = new Error('PRIVATE_ERROR');
    class SearchCollection { async search() { throw failure; } }
    inst.manuallyInstrument({ Collection: SearchCollection });
    const collection = new SearchCollection();
    for (const rejection of [failure, null]) {
      failure = rejection;
      exporter.reset();
      let caught = Symbol('not thrown');
      try { await collection.search({}); } catch (error) { caught = error; }
      assert.equal(caught, rejection, 'preserve the exact rejected value');
      const spans = exporter.getFinishedSpans();
      assert.equal(spans.length, 1);
      assert.equal(spans[0].status.code, SpanStatusCode.ERROR);
      assert.equal(JSON.stringify([spans[0].status, spans[0].events]).includes('PRIVATE_ERROR'), false);
    }
    let resolve;
    class Hidden { async count() { return 3; } }
    class Client { getCollection() { return new Promise(r => { resolve = r; }); } }
    const count = Hidden.prototype.count;
    inst.manuallyInstrument({ ChromaClient: Client });
    const pending = new Client().getCollection();
    inst.disable();
    resolve(new Hidden());
    await pending;
    assert.equal(Hidden.prototype.count, count, 'factory completion after disable must not patch');
    assert.equal(First.prototype.count, originals[0]);
    assert.equal(Second.prototype.count, originals[1]);
    assert.equal(isPatched(), false);
  } finally { inst.disable(); other.disable(); await provider.shutdown(); }
});


test('every operation preserves arbitrary rejection identity and error status', async (t) => {
  class ManualOnlyInstrumentation extends ChromaDBInstrumentation { init() { return []; } }
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const inst = new ManualOnlyInstrumentation({ traceConfig: { hideErrorDetails: true } });
  inst.setTracerProvider(provider);
  try {
    for (const op of ['add', 'query', 'get', 'update', 'upsert', 'delete', 'count', 'peek', 'search']) {
      await t.test(op, async () => {
        let failure;
        class Collection { async [op]() { throw failure; } }
        inst.manuallyInstrument({ Collection });
        for (const value of [null, undefined, 'PRIVATE_ERROR', new Error('PRIVATE_ERROR'), { message: 'PRIVATE_ERROR' }, 42]) {
          failure = value;
          exporter.reset();
          let caught = Symbol('not thrown');
          try { await new Collection()[op]({}); } catch (e) { caught = e; }
          assert.equal(caught, value);
          const spans = exporter.getFinishedSpans();
          assert.equal(spans.length, 1);
          assert.equal(spans[0].status.code, SpanStatusCode.ERROR);
          assert.equal(JSON.stringify([spans[0].status, spans[0].events]).includes('PRIVATE_ERROR'), false);
        }
      });
    }
  } finally { inst.disable(); await provider.shutdown(); }
});

test('metadata normalization preserves argument and response identity', async () => {
  class ManualOnlyInstrumentation extends ChromaDBInstrumentation { init() { return []; } }
  const provider = new NodeTracerProvider();
  const inst = new ManualOnlyInstrumentation();
  inst.setTracerProvider(provider);
  try {
    for (const op of ['add', 'update', 'upsert', 'get', 'delete', 'peek']) {
      const params = Object.freeze(op === 'peek' ? {} : {
        ids: 'single-id', embeddings: Object.freeze([0.1, 0.2, 0.3]), documents: 'single document',
      });
      const extra = Object.freeze({ marker: 'unchanged' });
      const result = { ids: ['single-id'] };
      class Collection {
        async [op](...args) {
          assert.equal(args.length, 2);
          assert.equal(args[0], params);
          assert.equal(args[1], extra);
          return result;
        }
      }
      inst.manuallyInstrument({ Collection });
      assert.equal(await new Collection()[op](params, extra), result);
    }
  } finally { inst.disable(); await provider.shutdown(); }
});

test('explicit hideInputs overrides environment without serializing hidden filters', async () => {
  class ManualOnlyInstrumentation extends ChromaDBInstrumentation { init() { return []; } }
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const previous = process.env.FI_HIDE_INPUTS;
  try {
    for (const hideInputs of [true, false]) {
      process.env.FI_HIDE_INPUTS = String(!hideInputs);
      const inst = new ManualOnlyInstrumentation({ traceConfig: { hideInputs } });
      inst.setTracerProvider(provider);
      try {
        let received;
        class Collection { async query(params) { received = params; return { ids: [] }; } }
        inst.manuallyInstrument({ Collection });
        const where = hideInputs ? { toJSON() { throw new Error('must not serialize hidden data'); } }
          : { field: 'public' };
        const params = { where };
        exporter.reset();
        await new Collection().query(params);
        assert.equal(received, params);
        assert.equal(exporter.getFinishedSpans()[0].attributes['db.vector.query.filter'],
          hideInputs ? undefined : JSON.stringify(where));
      } finally { inst.disable(); }
    }
  } finally {
    if (previous === undefined) delete process.env.FI_HIDE_INPUTS;
    else process.env.FI_HIDE_INPUTS = previous;
    await provider.shutdown();
  }
});
