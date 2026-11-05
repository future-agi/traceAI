'use strict';
// Real SDK constructor/dispatch and native interceptor code; inert Milvus RPCs.
// Usage: node contract/sdk.cjs /absolute/path/to/sdk/package.json
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');
const { createRequire } = require('node:module');
const { context, trace, propagation } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { W3CTraceContextPropagator } = require('@opentelemetry/core');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { MilvusInstrumentation, isPatched } = require('../dist/src');

const sdkPackage = process.argv[2] || require.resolve('@zilliz/milvus2-sdk-node/package.json');
const sdkRequire = createRequire(sdkPackage);
const root = path.dirname(sdkPackage);
const version = sdkRequire(sdkPackage).version;
const memory = new InMemorySpanExporter();
const requests = [];
const server = http.createServer((request, response) => {
  const chunks = [];
  request.on('data', chunk => chunks.push(chunk));
  request.on('end', () => {
    assert.equal(request.url, '/v1/traces');
    requests.push(JSON.parse(Buffer.concat(chunks).toString()));
    response.setHeader('content-type', 'application/json');
    response.end('{}');
  });
});

function loadWithImports(file, replace) {
  const exports = {};
  const localRequire = createRequire(file);
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
    exports,
    require: name => replace(name) ?? localRequire(name),
    __dirname: path.dirname(file),
  }, { filename: file });
  return exports;
}

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const provider = new BasicTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(memory),
    new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `http://127.0.0.1:${server.address().port}/v1/traces`, headers: {},
    })),
  ] });
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  trace.setGlobalTracerProvider(provider);
  propagation.setGlobalPropagator(new W3CTraceContextPropagator());
  const instrumentation = new MilvusInstrumentation();
  instrumentation.setTracerProvider(provider);
  let scenario;
  try {
    const currentData = path.join(root, 'dist/milvus/grpc/Data.js');
    if (fs.existsSync(currentData)) {
      // Preserve the SDK's own constructor alias and search dispatch. Only the
      // superclass (which opens channels) and RPC body are replaced.
      const { Data } = loadWithImports(currentData, name =>
        name === './Collection' ? { Collection: class {} } : undefined);
      class MilvusClient extends Data {}
      MilvusClient.prototype._search = async () => ({ results: [{ id: 1 }] });
      const early = new MilvusClient();
      const original = MilvusClient.prototype.search;
      instrumentation.manuallyInstrument({ MilvusClient });
      const late = new MilvusClient();
      const params = { collection_name: 'synthetic', data: [{ anns_field: 'vector', data: [0.1, 0.2] }], limit: 2 };
      await early.hybridSearch(params);
      assert.equal(memory.getFinishedSpans().length, 0, 'pre-setup alias is outside module patch');
      instrumentation.manuallyInstrumentClient(early);
      for (let cycle = 0; cycle < 3; cycle++) {
        memory.reset();
        await early.hybridSearch(params);
        await late.hybridSearch(params);
        await late.search(params);
        assert.equal(memory.getFinishedSpans().length, 3, 'one operation span per call');
        instrumentation.disable();
        assert.equal(MilvusClient.prototype.search, original);
        memory.reset();
        await early.hybridSearch(params);
        await late.hybridSearch(params);
        await late.search(params);
        assert.equal(memory.getFinishedSpans().length, 0, 'saved aliases stop tracing');
        instrumentation.enable();
      }

      // Execute the actual trace:true interceptor, replacing only grpc's call
      // container; verify injected trace context and parentage without a server.
      const grpcFile = path.join(root, 'dist/milvus/utils/Grpc.js');
      const { getTraceInterceptor } = loadWithImports(grpcFile, name =>
        name === '@grpc/grpc-js' ? {
          InterceptingCall: class { constructor(call, requester) { this.requester = requester; } },
        } : undefined);
      let complete;
      let injected;
      MilvusClient.prototype._search = () => new Promise(resolve => {
        const metadata = { add(key, value) { if (key === 'traceparent') injected = value; } };
        const call = getTraceInterceptor()({}, () => ({}));
        call.requester.start(metadata, {}, () => { complete = resolve; });
      });
      memory.reset();
      const parent = provider.getTracer('application').startSpan('parent');
      const pending = context.with(trace.setSpan(context.active(), parent), () => late.search(params));
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(memory.getFinishedSpans().length, 1, 'only propagation span ends before RPC');
      const native = memory.getFinishedSpans()[0];
      assert.equal(native.name, 'grpc-intercept');
      assert.equal(injected.split('-')[2], native.spanContext().spanId);
      complete({ results: [{ id: 1 }, { id: 2 }] });
      await pending;
      const operation = memory.getFinishedSpans().find(span => span.name === 'milvus search');
      assert.equal(operation.parentSpanContext.spanId, parent.spanContext().spanId);
      assert.equal(native.parentSpanContext.spanId, operation.spanContext().spanId);
      assert.equal(operation.attributes['db.response.returned_rows'], 2);
      parent.end();
      // A distinct HTTP client is never patched by the module entry point.
      class HttpClient { search() { return Promise.resolve({ data: [] }); } }
      const httpSearch = HttpClient.prototype.search;
      instrumentation.manuallyInstrument({ MilvusClient, HttpClient });
      assert.equal(HttpClient.prototype.search, httpSearch);
      scenario = 'real Data constructor/dispatch, lifecycle, aliases, native interceptor parentage';
    } else {
      // SDK 2.0 exposes dataManager rather than MilvusClient.search. Exercise
      // its real Data.search, protobuf serialization and callback dispatch.
      const { Data } = sdkRequire(path.join(root, 'dist/milvus/Data.js'));
      const { DataType } = sdkRequire(path.join(root, 'dist/milvus/types/Common.js'));
      let calls = 0;
      const manager = new Data({ Search(_request, callback) {
        calls++;
        callback(null, { status: { error_code: 'Success' } });
      } }, { describeCollection: async () => ({ schema: { fields: [
        { name: 'vector', data_type: 'FloatVector', type_params: [{ key: 'dim', value: '2' }] },
      ] } }) });
      const params = { collection_name: 'synthetic', vectors: [[0.1, 0.2]], vector_type: DataType.FloatVector,
        search_params: { anns_field: 'vector', metric_type: 'L2', topk: '2', params: '{}' } };
      const original = manager.search;
      instrumentation.manuallyInstrumentClient(manager);
      for (let cycle = 0; cycle < 3; cycle++) {
        memory.reset();
        await manager.search(params);
        assert.equal(memory.getFinishedSpans().length, 1);
        instrumentation.disable();
        assert.equal(manager.search, original);
        memory.reset();
        await manager.search(params);
        assert.equal(memory.getFinishedSpans().length, 0);
        instrumentation.enable();
      }
      assert.equal(calls, 6);
      scenario = 'real SDK 2.0 Data manager search, protobuf serialization and callback dispatch';
    }
    await provider.forceFlush();
    const wire = requests.flatMap(request => request.resourceSpans.flatMap(resource =>
      resource.scopeSpans.flatMap(scope => scope.spans)));
    const operations = wire.filter(span => span.name === 'milvus search');
    assert.ok(operations.length > 0, 'OTLP receiver gets operation spans');
    for (const span of operations) {
      const attributes = Object.fromEntries(span.attributes.map(attribute => [attribute.key, attribute.value]));
      assert.equal(span.kind, 3, 'OTLP CLIENT kind');
      assert.equal(attributes['db.system'].stringValue, 'milvus');
      assert.equal(attributes['db.operation.name'].stringValue, 'search');
      assert.equal(attributes['db.collection.name'].stringValue, 'synthetic');
      const allowed = new Set(['db.system', 'db.operation.name', 'fi.span.kind',
        'db.collection.name', 'db.vector.query.top_k', 'db.vector.query.dimensions',
        'db.response.returned_rows']);
      assert.ok(Object.keys(attributes).every(key => allowed.has(key)),
        'wire attributes contain only operation metadata, never request/result payloads');
    }
    console.log(JSON.stringify({ version, scenario, operationSpansOnWire: operations.length,
      localhostOtlpRequests: requests.length, status: 'PASS' }));
  } finally {
    instrumentation.disable();
    assert.equal(isPatched(), false);
    await provider.shutdown();
    context.disable();
    trace.disable();
    propagation.disable();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
