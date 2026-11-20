// Ambient OTLP headers must not reach a Future AGI sink, at every OTel SDK version the CI fixture installs.
// Runs against the built package: FI_CORE_PACKAGE_JSON selects the fixture install, as in provider-lifecycle.
const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
for (const key of Object.keys(process.env)) {
  if (key.startsWith('FI_') && key !== 'FI_CORE_PACKAGE_JSON' || key.startsWith('OTEL_')) delete process.env[key];
}
const grpc = load('@grpc/grpc-js');
const { register, Transport } = load('./dist/src');
const FI = { 'x-api-key': 'synthetic-fi-key', 'x-secret-key': 'synthetic-fi-secret' };
const AUTH = 'Bearer%20SYNTHETIC-OTHER-BACKEND';
const CASES = {
  general: { OTEL_EXPORTER_OTLP_HEADERS: `authorization=${AUTH},x-synthetic-ambient-marker=general` },
  traces: { OTEL_EXPORTER_OTLP_TRACES_HEADERS: `authorization=${AUTH},x-synthetic-ambient-marker=traces` },
  both: { OTEL_EXPORTER_OTLP_HEADERS: `x-synthetic-ambient-general=general,x-synthetic-ambient-marker=general`, OTEL_EXPORTER_OTLP_TRACES_HEADERS: `authorization=${AUTH},x-synthetic-ambient-marker=traces` },
};
const AMBIENT = ['authorization', 'x-synthetic-ambient-marker', 'x-synthetic-ambient-general'];
const cleanup = [];
afterEach(async () => { for (const fn of cleanup.splice(0)) await fn(); });

async function httpSink() {
  const received = [];
  const server = http.createServer((req, res) => { req.resume(); req.on('end', () => { received.push(req.headers); res.setHeader('content-type', 'application/json'); res.end('{}'); }); });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  cleanup.push(() => new Promise(done => { server.closeAllConnections(); server.close(done); }));
  return { received, endpoint: `http://127.0.0.1:${server.address().port}` };
}
async function grpcSink() {
  const received = [];
  const server = new grpc.Server();
  const id = buffer => buffer;
  server.addService({ Export: { path: '/opentelemetry.proto.collector.trace.v1.TraceService/Export', requestStream: false, responseStream: false,
    requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } },
  { Export: (call, callback) => { received.push(call.metadata); callback(null, Buffer.alloc(0)); } });
  const port = await new Promise((ok, bad) => server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
  cleanup.push(() => server.forceShutdown());
  return { received, endpoint: `127.0.0.1:${port}` };
}
async function exportOne(options) {
  const provider = register({ projectName: 'ambient-headers', setGlobalTracerProvider: false, batch: false, verbose: false, ...options });
  provider.getTracer('ambient-headers').startSpan('one').end();
  await provider.forceFlush();
  await provider.shutdown();
}

for (const [label, env] of Object.entries(CASES)) {
  test(`HTTP exporter: ambient ${label} headers are not sent to the FI endpoint`, async () => {
    Object.assign(process.env, env); cleanup.push(() => { for (const key of Object.keys(env)) delete process.env[key]; });
    const sink = await httpSink();
    await exportOne({ endpoint: sink.endpoint, headers: { ...FI, 'x-caller-custom': 'caller' } });
    assert.equal(sink.received.length, 1);
    const [headers] = sink.received;
    assert.equal(headers['x-api-key'], FI['x-api-key']);
    assert.equal(headers['x-secret-key'], FI['x-secret-key']);
    assert.equal(headers['x-caller-custom'], 'caller');
    for (const name of AMBIENT) assert.equal(headers[name], undefined, `${name} reached the sink`);
  });
  test(`gRPC exporter: ambient ${label} headers are not sent to the FI endpoint`, async () => {
    Object.assign(process.env, env); cleanup.push(() => { for (const key of Object.keys(env)) delete process.env[key]; });
    const sink = await grpcSink();
    await exportOne({ endpoint: sink.endpoint, transport: Transport.GRPC, headers: { ...FI, 'x-caller-custom': 'caller' } });
    assert.equal(sink.received.length, 1);
    const [metadata] = sink.received;
    assert.deepEqual(metadata.get('x-api-key'), [FI['x-api-key']]);
    assert.deepEqual(metadata.get('x-secret-key'), [FI['x-secret-key']]);
    assert.deepEqual(metadata.get('x-caller-custom'), ['caller']);
    for (const name of AMBIENT) assert.deepEqual(metadata.get(name), [], `${name} reached the sink`);
  });
}

test('an exporter for another collector sends no FI keys and keeps its own OTLP headers', async () => {
  Object.assign(process.env, { FI_API_KEY: 'synthetic-env-key', FI_SECRET_KEY: 'synthetic-env-secret' }, CASES.general);
  cleanup.push(() => { for (const key of ['FI_API_KEY', 'FI_SECRET_KEY', ...Object.keys(CASES.general)]) delete process.env[key]; });
  const http1 = await httpSink();
  await exportOne({ endpoint: http1.endpoint });
  assert.equal(http1.received.length, 1);
  assert.equal(http1.received[0]['x-api-key'], undefined);
  assert.equal(http1.received[0]['x-secret-key'], undefined);
  assert.equal(http1.received[0].authorization, 'Bearer SYNTHETIC-OTHER-BACKEND');
  const grpc1 = await grpcSink();
  await exportOne({ endpoint: grpc1.endpoint, transport: Transport.GRPC });
  assert.equal(grpc1.received.length, 1);
  assert.deepEqual(grpc1.received[0].get('x-api-key'), []);
  assert.deepEqual(grpc1.received[0].get('x-secret-key'), []);
  assert.deepEqual(grpc1.received[0].get('authorization'), ['Bearer SYNTHETIC-OTHER-BACKEND']);
});
