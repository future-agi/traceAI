const assert = require('node:assert/strict');
const { test, before, after, afterEach } = require('node:test');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const http = require('node:http');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { RedisInstrumentation, isPatched } = require('../dist/src');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { NodeTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = coreRequire('@opentelemetry/sdk-trace-node');
const { OTLPTraceExporter } = coreRequire('@opentelemetry/exporter-trace-otlp-http');
const redisRequire = process.env.REDIS_TEST_ROOT
  ? createRequire(`${process.env.REDIS_TEST_ROOT}/package.json`) : require;
// Load before constructing instrumentation, as normal static ESM imports do.
const redis = redisRequire('redis');
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const instances = [];
function instrument(options) {
  const instance = new RedisInstrumentation(options);
  instance.setTracerProvider(provider);
  instances.push(instance);
  return instance;
}
function spans() { return exporter.getFinishedSpans(); }
before(() => provider.register());
afterEach(() => { for (const instance of instances.splice(0)) instance.disable(); exporter.reset(); });
after(() => provider.shutdown());

test('automatic CJS loading and manual setup coexist in a fresh process', () => {
  execFileSync(process.execPath, [path.join(__dirname, 'auto.cjs')], { timeout: 15000, stdio: 'pipe' });
});

test('built package loads through native ESM named imports', async () => {
  const namespace = await import(pathToFileURL(require.resolve('../dist/src')).href);
  assert.equal(namespace.RedisInstrumentation, RedisInstrumentation);
});

test('mutable CJS factories and existing clients are restored on public disable', async () => {
  const instance = instrument();
  const original = async function (key, field) { assert.equal(this, client); return `${key}:${field}`; };
  const client = { hGet: original };
  const createClient = () => client;
  const module = { createClient };
  assert.equal(instance.manuallyInstrument(module), module);
  assert.equal(module.createClient(), client);
  const retainedWrapper = client.hGet;
  assert.equal(await client.hGet('key', 'field'), 'key:field');
  assert.equal(spans().length, 1);
  instance.disable();
  assert.equal(module.createClient, createClient);
  assert.equal(client.hGet, original);
  await retainedWrapper.call(client, 'key', 'field');
  await client.hGet('key', 'field');
  assert.equal(spans().length, 1);
  assert.equal(isPatched(), false);
  for (let cycle = 0; cycle < 3; cycle++) {
    instance.enable(); instance.enable();
    instance.manuallyInstrument(module); module.createClient(); module.createClient();
    await client.hGet('key', 'field');
    assert.equal(spans().length, cycle + 2);
    instance.disable();
    assert.equal(client.hGet, original);
  }
});

test('native ESM namespace is unchanged and its returned facade instruments real clients', async () => {
  const namespace = await import(pathToFileURL(redisRequire.resolve('redis')).href);
  const original = namespace.createClient;
  const instance = instrument();
  const facade = instance.manuallyInstrument(namespace);
  assert.notEqual(facade, namespace);
  assert.equal(namespace.createClient, original);
  assert.equal(instance.manuallyInstrument(namespace), facade);
  const client = facade.createClient();
  await assert.rejects(client.hGet('synthetic-key', 'field'));
  assert.equal(spans().length, 1);
  instance.disable();
  await assert.rejects(client.hGet('synthetic-key', 'field'));
  assert.equal(spans().length, 1);
  instance.enable();
  await assert.rejects(client.hGet('synthetic-key', 'field'));
  assert.equal(spans().length, 2);
});

test('all available upstream factories and uppercase aliases emit one span', async () => {
  const instance = instrument();
  const facade = instance.manuallyInstrument({ ...redis });
  const cases = [
    ['createClient', []],
    ['createCluster', [{ rootNodes: [] }]],
    ['createClientPool', []],
    ['createSentinel', [{ name: 'synthetic', sentinelRootNodes: [{ host: '127.0.0.1', port: 26379 }] }]],
  ];
  let count = 0;
  for (const [name, args] of cases) {
    if (typeof facade[name] !== 'function') continue; // Pool/sentinel were added after Redis 4.
    const reference = redis[name](...args);
    const client = facade[name](...args);
    const wrapped = client.hGet;
    instance.instrumentClient(client);
    assert.equal(client.hGet, wrapped, `${name} is idempotent`);
    for (const [receiver, command, commandArgs] of [
      [client, 'hGet', ['key', 'field']],
      [client, 'HGET', ['key', 'field']],
      [client.ft, 'search', ['index', '*']],
      [client.ft, 'SEARCH', ['index', '*']],
      [client.json, 'get', ['key']],
    ]) {
      assert.equal(typeof receiver?.[command], 'function', `${name}.${command}`);
      const referenceReceiver = receiver === client ? reference : receiver === client.ft ? reference.ft : reference.json;
      let originalError;
      await assert.rejects(async () => referenceReceiver[command](...commandArgs), (error) => {
        originalError = error;
        return true;
      });
      await assert.rejects(async () => receiver[command](...commandArgs), (error) => {
        assert.equal(error.constructor, originalError.constructor);
        assert.equal(error.message, originalError.message);
        return true;
      });
      assert.equal(spans().length, ++count, `${name}.${command} emits one span`);
    }
  }
  assert.ok(count >= 10); // Standalone and cluster even at the supported floor.
});

test('two instances share one wrapper without disabling each other', async () => {
  const first = instrument(); const second = instrument();
  const client = { hGet: async () => 'value' };
  const original = client.hGet;
  const module = { createClient: () => client };
  first.manuallyInstrument(module); second.manuallyInstrument(module);
  module.createClient();
  await client.hGet(); assert.equal(spans().length, 1);
  first.disable();
  await client.hGet(); assert.equal(spans().length, 2);
  first.enable();
  await client.hGet(); assert.equal(spans().length, 3);
  second.disable();
  await client.hGet(); assert.equal(spans().length, 4);
  first.disable();
  assert.equal(client.hGet, original);
});

test('disabled construction, late provider registration, and direct clients work', async () => {
  const instance = instrument({ instrumentationConfig: { enabled: false } });
  const client = { hGet: async () => 'value' };
  const original = client.hGet;
  instance.instrumentClient(client);
  assert.equal(client.hGet, original);
  await client.hGet(); assert.equal(spans().length, 0);
  instance.enable();
  await client.hGet(); assert.equal(spans().length, 1);
  const lateExporter = new InMemorySpanExporter();
  const lateProvider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(lateExporter)] });
  instance.setTracerProvider(lateProvider);
  await client.hGet();
  assert.equal(lateExporter.getFinishedSpans().length, 1);
  assert.equal(spans().length, 1);
  await lateProvider.shutdown();
});

test('restores inherited methods and does not overwrite a later application wrapper', async () => {
  const instance = instrument();
  const original = async () => 'value';
  const client = Object.create({ hGet: original });
  instance.instrumentClient(client);
  assert.ok(Object.hasOwn(client, 'hGet'));
  instance.disable();
  assert.equal(Object.hasOwn(client, 'hGet'), false);
  instance.enable();
  const ours = client.hGet;
  const external = function (...args) { return ours.apply(this, args); };
  client.hGet = external;
  instance.instrumentClient(client);
  await client.hGet();
  assert.equal(spans().length, 1);
  exporter.reset();
  instance.disable();
  assert.equal(client.hGet, external);
  await client.hGet(); assert.equal(spans().length, 0);
  instance.enable();
  await client.hGet(); assert.equal(spans().length, 1);
});

test('multiple module objects, immutable exports and command receiver identity', async () => {
  const instance = instrument();
  const client = {
    ft: { search: async function () { assert.equal(this, client.ft); return { total: 0, documents: [] }; } },
    json: { get: async function () { assert.equal(this, client.json); return { value: true }; } },
  };
  const frozen = Object.freeze({ createClient: () => client });
  const facade = instance.manuallyInstrument(frozen);
  assert.notEqual(facade, frozen);
  facade.createClient();
  const another = { createClient: () => ({ hGet: async () => 'value' }) };
  instance.manuallyInstrument(another);
  await another.createClient().hGet();
  await client.ft.search('index', '*');
  await client.json.get('key');
  assert.equal(spans().length, 3);
});

test('every documented direct operation preserves results and records its operation', async () => {
  const instance = instrument();
  const result = { total: 1, documents: [], numDocs: 2 };
  const methods = [
    ['ft', 'search', 'ftSearch'], ['ft', 'create', 'ftCreate'],
    ['ft', 'dropIndex', 'ftDropIndex'], ['ft', 'info', 'ftInfo'],
    ['ft', 'aggregate', 'ftAggregate'], [null, 'hSet', 'hSet'],
    [null, 'hGet', 'hGet'], [null, 'hGetAll', 'hGetAll'], [null, 'del', 'del'],
    ['json', 'set', 'json.set'], ['json', 'get', 'json.get'],
  ];
  const client = { ft: {}, json: {} };
  for (const [namespace, name] of methods) (namespace ? client[namespace] : client)[name] = async () => result;
  instance.instrumentClient(client);
  for (const [namespace, name, operation] of methods) {
    assert.equal(await (namespace ? client[namespace] : client)[name]('key', '*'), result);
    assert.equal(spans().at(-1).attributes['db.operation.name'], operation);
  }
  assert.equal(spans().length, methods.length);
});

test('parent context, vector metadata, payload omission and original errors survive OTLP export', async (t) => {
  const bodies = [];
  const server = http.createServer((request, response) => {
    assert.equal(request.url, '/v1/traces');
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => { bodies.push(JSON.parse(Buffer.concat(chunks).toString())); response.end('{}'); });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const wireExporter = new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` });
  const wireProvider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(wireExporter)] });
  t.after(async () => {
    await wireProvider.shutdown();
    await new Promise((resolve) => server.close(resolve));
  });
  const instance = instrument({
    traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
    instrumentationConfig: { captureQueryVectors: true, captureResultVectors: true },
  });
  instance.setTracerProvider(wireProvider);
  const result = { total: 3, documents: [{ id: 'secret-document', value: { vector: [0.123456789] } }] };
  const error = new Error('secret-error-payload');
  const client = {
    ft: { search: async () => result },
    hGet: async () => { throw error; },
  };
  instance.instrumentClient(client);
  const parent = wireProvider.getTracer('test').startSpan('parent');
  try {
    await context.with(trace.setSpan(context.active(), parent), async () => {
      assert.equal(await client.ft.search('index', 'secret-query=>[KNN 4 @embedding $v]', {
        PARAMS: { v: Buffer.from('secret-vector') }, LIMIT: { from: 0, size: 0 },
      }), result);
      await assert.rejects(client.hGet('key', 'field'), (thrown) => thrown === error);
    });
    parent.end();
    await wireProvider.forceFlush();
    const exported = bodies.flatMap((body) => body.resourceSpans.flatMap((resource) => resource.scopeSpans.flatMap((scope) => scope.spans)));
    assert.equal(exported.length, 3);
    const search = exported.find((span) => span.name === 'redis ftSearch');
    const failure = exported.find((span) => span.name === 'redis hGet');
    const attrs = Object.fromEntries(search.attributes.map(({ key, value }) => [key, Object.values(value)[0]]));
    assert.equal(search.parentSpanId, parent.spanContext().spanId);
    assert.equal(search.kind, 3); // OTLP CLIENT (API CLIENT is 2).
    assert.equal(attrs['db.system'], 'redis');
    assert.equal(attrs['fi.span.kind'], 'VECTOR_DB');
    assert.equal(attrs['db.operation.type'], 'vector_search');
    assert.equal(Number(attrs['db.vector.query.top_k']), 0);
    assert.equal(Number(attrs['db.response.total_count']), 3);
    assert.equal(Number(attrs['db.response.returned_rows']), 1);
    assert.equal(failure.status.code, SpanStatusCode.ERROR);
    assert.equal(failure.status.message ?? '', '');
    const serialized = JSON.stringify(bodies);
    for (const secret of ['secret-query', 'secret-vector', 'secret-document', 'secret-error-payload', '0.123456789']) {
      assert.ok(!serialized.includes(secret), `OTLP must not contain ${secret}`);
    }
  } finally {
    instance.disable();
  }
});
