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
// An exporter is FI-bound by its resolved endpoint, so the sink is made the FI endpoint through the environment.
function fiEndpoint(sink, grpcTransport) {
  const name = grpcTransport ? 'FI_GRPC_COLLECTOR_ENDPOINT' : 'FI_BASE_URL';
  process.env[name] = sink.endpoint; cleanup.push(() => { delete process.env[name]; });
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
    const sink = await httpSink(); fiEndpoint(sink, false);
    await exportOne({ headers: { ...FI, 'x-caller-custom': 'caller' } });
    assert.equal(sink.received.length, 1);
    const [headers] = sink.received;
    assert.equal(headers['x-api-key'], FI['x-api-key']);
    assert.equal(headers['x-secret-key'], FI['x-secret-key']);
    assert.equal(headers['x-caller-custom'], 'caller');
    for (const name of AMBIENT) assert.equal(headers[name], undefined, `${name} reached the sink`);
  });
  test(`gRPC exporter: ambient ${label} headers are not sent to the FI endpoint`, async () => {
    Object.assign(process.env, env); cleanup.push(() => { for (const key of Object.keys(env)) delete process.env[key]; });
    const sink = await grpcSink(); fiEndpoint(sink, true);
    await exportOne({ transport: Transport.GRPC, headers: { ...FI, 'x-caller-custom': 'caller' } });
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

test('an exporter for another collector sends the caller\'s x-api-key as given and no FI env value', async () => {
  Object.assign(process.env, { FI_API_KEY: 'synthetic-env-key', FI_SECRET_KEY: 'synthetic-env-secret' });
  cleanup.push(() => { for (const key of ['FI_API_KEY', 'FI_SECRET_KEY']) delete process.env[key]; });
  const http1 = await httpSink();
  await exportOne({ endpoint: http1.endpoint, headers: { 'x-api-key': 'own-backend-key' } });
  assert.equal(http1.received[0]['x-api-key'], 'own-backend-key');
  assert.equal(http1.received[0]['x-secret-key'], undefined);
  const grpc1 = await grpcSink();
  await exportOne({ endpoint: grpc1.endpoint, transport: Transport.GRPC, headers: { 'x-api-key': 'own-backend-key' } });
  assert.deepEqual(grpc1.received[0].get('x-api-key'), ['own-backend-key']);
  assert.deepEqual(grpc1.received[0].get('x-secret-key'), []);
});

// ---- Caller gRPC metadata with environment FI keys ------------------------------------------------
const { createSpanExporter } = load('./dist/src');
const { BasicTracerProvider, SimpleSpanProcessor } = load('@opentelemetry/sdk-trace-base');
async function exportThrough(exporter) {
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  provider.getTracer('metadata').startSpan('one').end();
  await provider.forceFlush(); await provider.shutdown();
}
function envKeys() {
  Object.assign(process.env, { FI_API_KEY: 'synthetic-env-key', FI_SECRET_KEY: 'synthetic-env-secret' });
  cleanup.push(() => { delete process.env.FI_API_KEY; delete process.env.FI_SECRET_KEY; });
}
test('gRPC FI endpoint, env keys, caller metadata and no headers option: both the FI keys and the metadata arrive', async () => {
  envKeys();
  const sink = await grpcSink(); fiEndpoint(sink, true);
  const metadata = new grpc.Metadata(); metadata.set('x-caller-meta', 'from-metadata');
  await exportThrough(createSpanExporter({ transport: Transport.GRPC, endpoint: sink.endpoint, metadata }));
  assert.equal(sink.received.length, 1);
  assert.deepEqual(sink.received[0].get('x-api-key'), ['synthetic-env-key']);
  assert.deepEqual(sink.received[0].get('x-secret-key'), ['synthetic-env-secret']);
  assert.deepEqual(sink.received[0].get('x-caller-meta'), ['from-metadata']);
});
test('gRPC FI endpoint: an explicit headers pair wins over metadata and env, metadata stays underneath', async () => {
  envKeys();
  const sink = await grpcSink(); fiEndpoint(sink, true);
  const metadata = new grpc.Metadata(); metadata.set('x-caller-meta', 'from-metadata'); metadata.set('x-api-key', 'metadata-key');
  await exportThrough(createSpanExporter({ transport: Transport.GRPC, endpoint: sink.endpoint, metadata, headers: FI }));
  assert.deepEqual(sink.received[0].get('x-api-key'), [FI['x-api-key']]);
  assert.deepEqual(sink.received[0].get('x-secret-key'), [FI['x-secret-key']]);
  assert.deepEqual(sink.received[0].get('x-caller-meta'), ['from-metadata']);
});
test('gRPC FI endpoint: a complete key pair in caller metadata is explicit and env keys do not overwrite it', async () => {
  envKeys();
  const sink = await grpcSink(); fiEndpoint(sink, true);
  const metadata = new grpc.Metadata(); metadata.set('x-api-key', 'metadata-key-synthetic'); metadata.set('x-secret-key', 'metadata-secret-synthetic');
  await exportThrough(createSpanExporter({ transport: Transport.GRPC, endpoint: sink.endpoint, metadata }));
  assert.deepEqual(sink.received[0].get('x-api-key'), ['metadata-key-synthetic']);
  assert.deepEqual(sink.received[0].get('x-secret-key'), ['metadata-secret-synthetic']);
});
test('gRPC FI endpoint: an incomplete key pair in caller metadata is completed by neither half of the env pair', async () => {
  envKeys();
  const sink = await grpcSink(); fiEndpoint(sink, true);
  const metadata = new grpc.Metadata(); metadata.set('x-api-key', 'metadata-key-only-synthetic');
  await exportThrough(createSpanExporter({ transport: Transport.GRPC, endpoint: sink.endpoint, metadata }));
  assert.deepEqual(sink.received[0].get('x-api-key'), ['synthetic-env-key']);
  assert.deepEqual(sink.received[0].get('x-secret-key'), ['synthetic-env-secret']);
});
test('gRPC FI endpoint: caller metadata keeps repeated and binary values', async () => {
  envKeys();
  const sink = await grpcSink(); fiEndpoint(sink, true);
  const metadata = new grpc.Metadata(); metadata.add('x-repeated', 'one'); metadata.add('x-repeated', 'two'); metadata.set('x-blob-bin', Buffer.from([1, 2, 3]));
  await exportThrough(createSpanExporter({ transport: Transport.GRPC, endpoint: sink.endpoint, metadata }));
  // HTTP/2 joins repeated header values with a comma on receipt.
  assert.deepEqual(sink.received[0].get('x-repeated'), ['one, two']);
  assert.deepEqual(sink.received[0].get('x-blob-bin'), [Buffer.from([1, 2, 3])]);
});
// ---- A second physical copy of @opentelemetry/otlp-exporter-base ------------------------------------
// With npm hoisting the exporter packages can carry their own copy of the exporter base class, so the
// base class fi-core imports is not the one the exporters extend. Ambient OTLP headers must still not
// reach the FI endpoint, through the CommonJS entry and the ESM entry alike.
const fs = require('node:fs');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const coreRoot = path.dirname(load.resolve('./package.json'));
function packageDir(name) {
  let dir = path.dirname(load.resolve(name));
  while (!fs.existsSync(path.join(dir, 'package.json')) || JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')).name !== name) {
    const parent = path.dirname(dir); if (parent === dir) throw new Error(`cannot find ${name}`); dir = parent;
  }
  return dir;
}
function copyPackage(from, to) { fs.mkdirSync(path.dirname(to), { recursive: true }); fs.cpSync(from, to, { recursive: true, dereference: true, filter: src => !/[\\/]node_modules$/.test(src) }); }
function duplicateInstall() {
  const root = fs.mkdtempSync(path.join(coreRoot, '.dup-'));
  cleanup.push(() => fs.rmSync(root, { recursive: true, force: true }));
  const nm = path.join(root, 'node_modules');
  const coreCopy = path.join(nm, '@traceai', 'fi-core');
  copyPackage(path.join(coreRoot, 'dist'), path.join(coreCopy, 'dist'));
  fs.copyFileSync(path.join(coreRoot, 'package.json'), path.join(coreCopy, 'package.json'));
  const otel = name => path.join(nm, '@opentelemetry', name);
  const nested = (exporter, name) => path.join(otel(exporter), 'node_modules', '@opentelemetry', name);
  for (const exporter of ['exporter-trace-otlp-http', 'exporter-trace-otlp-grpc']) {
    copyPackage(packageDir(`@opentelemetry/${exporter}`), otel(exporter));
    copyPackage(packageDir('@opentelemetry/otlp-exporter-base'), nested(exporter, 'otlp-exporter-base'));
  }
  copyPackage(packageDir('@opentelemetry/otlp-grpc-exporter-base'), nested('exporter-trace-otlp-grpc', 'otlp-grpc-exporter-base'));
  const base = packageDir('@opentelemetry/otlp-exporter-base');
  assert.notEqual(fs.realpathSync(nested('exporter-trace-otlp-http', 'otlp-exporter-base')), fs.realpathSync(base));
  return path.join(nm, '@traceai', 'fi-core');
}
const ENTRIES = {
  cjs: dir => require(path.join(dir, 'dist', 'src', 'index.js')),
  esm: dir => import(pathToFileURL(path.join(dir, 'dist', 'esm', 'index.js')).href),
};
async function exportWith(core, options) {
  const provider = core.register({ projectName: 'ambient-headers', setGlobalTracerProvider: false, batch: false, verbose: false, ...options });
  provider.getTracer('ambient-headers').startSpan('one').end();
  await provider.forceFlush();
  await provider.shutdown();
}
for (const [entry, loadEntry] of Object.entries(ENTRIES)) {
  for (const [label, env] of Object.entries(CASES)) {
    // Either the headers are isolated correctly, or the exporter is refused: never ambient headers at the FI sink.
    for (const transport of ['http', 'grpc']) {
      test(`${entry} entry, second otlp-exporter-base copy, ${transport}: ambient ${label} headers do not reach the FI endpoint`, async () => {
        Object.assign(process.env, env); cleanup.push(() => { for (const key of Object.keys(env)) delete process.env[key]; });
        const core = await loadEntry(duplicateInstall());
        const grpcTransport = transport === 'grpc';
        const sink = grpcTransport ? await grpcSink() : await httpSink(); fiEndpoint(sink, grpcTransport);
        let refused;
        try { await exportWith(core, { transport: grpcTransport ? core.Transport.GRPC : core.Transport.HTTP, headers: FI }); } catch (error) { refused = error; }
        if (refused) {
          assert.match(refused.message, /^Unsupported OTLP exporter layout: /);
          assert.doesNotMatch(refused.message, /synthetic|x-api-key|x-secret-key|authorization/i);
          assert.equal(sink.received.length, 0);
          return;
        }
        assert.equal(sink.received.length, 1);
        const got = sink.received[0];
        if (grpcTransport) {
          assert.deepEqual(got.get('x-api-key'), [FI['x-api-key']]);
          for (const name of AMBIENT) assert.deepEqual(got.get(name), [], `${name} reached the sink`);
        } else {
          assert.equal(got['x-api-key'], FI['x-api-key']);
          for (const name of AMBIENT) assert.equal(got[name], undefined, `${name} reached the sink`);
        }
      });
    }
  }
}
test('esm entry (no duplicate): ambient headers do not reach the FI endpoint', async () => {
  Object.assign(process.env, CASES.both); cleanup.push(() => { for (const key of Object.keys(CASES.both)) delete process.env[key]; });
  const core = await import(pathToFileURL(path.join(coreRoot, 'dist', 'esm', 'index.js')).href);
  const sink = await httpSink(); fiEndpoint(sink, false);
  await exportWith(core, { headers: FI });
  assert.equal(sink.received.length, 1);
  for (const name of AMBIENT) assert.equal(sink.received[0][name], undefined, `${name} reached the sink`);
  assert.equal(sink.received[0]['x-api-key'], FI['x-api-key']);
});

// ---- Nothing environment-dependent changes the outcome ------------------------------------------------
const { spawnSync } = require('node:child_process');
const coreDist = path.join(coreRoot, 'dist', 'src', 'index.js');
function child(script, env) {
  const r = spawnSync(process.execPath, ['-e', script], { cwd: coreRoot, encoding: 'utf8', timeout: 30000,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env } });
  return { status: r.status, out: (r.stdout || '') + (r.stderr || '') };
}
const { spawn } = require('node:child_process');
const childAsync = (script, env) => new Promise(done => {
  const proc = spawn(process.execPath, ['-e', script], { cwd: coreRoot, timeout: 30000, env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env } });
  proc.on('close', () => done());
});
for (const worker of [undefined, '1']) {
  const env = worker ? { JEST_WORKER_ID: worker, NODE_ENV: 'test' } : {};
  test(`JEST_WORKER_ID=${worker}: a stand-in-shaped exporter on the FI route is refused, with no value in the error`, () => {
    // The HTTP exporter class is replaced by an object with no OTLP delegate before the SDK loads.
    const script = `
      const resolved = require.resolve('@opentelemetry/exporter-trace-otlp-http');
      require.cache[resolved] = { id: resolved, filename: resolved, loaded: true, exports: { OTLPTraceExporter: class { export() {} shutdown() { return Promise.resolve(); } forceFlush() { return Promise.resolve(); } } } };
      const core = require(${JSON.stringify(coreDist)});
      try { core.createSpanExporter({ headers: { 'x-api-key': 'synthetic-fi-key', 'x-secret-key': 'synthetic-fi-secret' } }); console.log('BUILT'); }
      catch (e) { console.log('REFUSED ' + e.message); }`;
    const r = child(script, { ...env, FI_BASE_URL: 'http://127.0.0.1:9' });
    assert.match(r.out, /REFUSED Unsupported OTLP exporter layout: /);
    assert.doesNotMatch(r.out, /BUILT|synthetic/);
  });
  test(`JEST_WORKER_ID=${worker}: a real exporter on the FI route keeps ambient headers off the sink`, async () => {
    const sink = await httpSink();
    const script = `
      const core = require(${JSON.stringify(coreDist)});
      (async () => { const p = core.register({ projectName: 'x', setGlobalTracerProvider: false, batch: false });
        p.getTracer('t').startSpan('one').end(); await p.forceFlush(); await p.shutdown(); })();`;
    await childAsync(script, { ...env, FI_BASE_URL: sink.endpoint, FI_API_KEY: 'synthetic-fi-key', FI_SECRET_KEY: 'synthetic-fi-secret', ...CASES.both });
    assert.equal(sink.received.length, 1);
    for (const name of AMBIENT) assert.equal(sink.received[0][name], undefined);
    assert.equal(sink.received[0]['x-api-key'], 'synthetic-fi-key');
  });
}
test('the published type surface has no fiEndpoint, and no shipped source reads JEST variables', () => {
  const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(path.join(dir, e.name)) : [path.join(dir, e.name)]);
  for (const file of walk(path.join(coreRoot, 'dist')).filter(f => f.endsWith('.d.ts'))) assert.doesNotMatch(fs.readFileSync(file, 'utf8'), /fiEndpoint/, file);
  for (const file of walk(path.join(coreRoot, 'dist', 'src')).filter(f => f.endsWith('.js'))) assert.doesNotMatch(fs.readFileSync(file, 'utf8'), /JEST_WORKER_ID/, file);
});

// ---- Plaintext opt-in gRPC: a channel built with proxies off ----------------------------------------
// A plaintext channel that carries keys under the opt-in goes to a host that is not literal loopback (127.0.0.2 here, which this
// machine routes to the loopback sink). A gRPC proxy in the environment must not get it, with or without gzip; a channel that is
// not plaintext never takes this path; an exporter without the layout it relies on is refused.
const net = require('node:net');
async function grpcSinkAt(address) {
  const received = [];
  const server = new grpc.Server();
  const id = buffer => buffer;
  server.addService({ Export: { path: '/opentelemetry.proto.collector.trace.v1.TraceService/Export', requestStream: false, responseStream: false,
    requestSerialize: id, requestDeserialize: id, responseSerialize: id, responseDeserialize: id } },
  { Export: (call, callback) => { received.push(call.metadata); callback(null, Buffer.alloc(0)); } });
  const port = await new Promise((ok, bad) => server.bindAsync(`${address}:0`, grpc.ServerCredentials.createInsecure(), (e, p) => e ? bad(e) : ok(p)));
  cleanup.push(() => server.forceShutdown());
  return { received, endpoint: `${address}:${port}` };
}
async function proxyTrap() {
  const state = { connections: 0 };
  const server = net.createServer(socket => { state.connections++; socket.destroy(); });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  cleanup.push(() => new Promise(done => server.close(done)));
  state.url = `http://127.0.0.1:${server.address().port}`;
  return state;
}
for (const compression of [undefined, 'gzip']) {
  test(`plaintext opt-in gRPC, compression ${compression}: sent direct with a gRPC proxy in the environment, keys arrive`, async () => {
    const trap = await proxyTrap();
    for (const name of ['grpc_proxy', 'https_proxy', 'http_proxy']) { process.env[name] = trap.url; cleanup.push(() => { delete process.env[name]; }); }
    const sink = await grpcSinkAt('127.0.0.2');
    const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: sink.endpoint, credentials: grpc.credentials.createInsecure(), headers: FI,
      allowInsecureHttp: true, ...(compression ? { compression } : {}) });
    await exportThrough(exporter);
    assert.equal(trap.connections, 0, 'the proxy was contacted');
    assert.equal(sink.received.length, 1);
    assert.deepEqual(sink.received[0].get('x-api-key'), [FI['x-api-key']]);
  });
}
test('a gRPC channel that is not plaintext keeps the exporter\'s own send; the plaintext opt-in one replaces it', async () => {
  const own = exporter => Object.prototype.hasOwnProperty.call(exporter._delegate._transport, 'send');
  const tls = createSpanExporter({ transport: Transport.GRPC, endpoint: 'collector.example:4317', credentials: grpc.credentials.createSsl(), headers: FI });
  assert.equal(own(tls), false);
  const plaintext = createSpanExporter({ transport: Transport.GRPC, endpoint: '127.0.0.2:4317', credentials: grpc.credentials.createInsecure(), headers: FI, allowInsecureHttp: true });
  assert.equal(own(plaintext), true);
  await tls.shutdown(); await plaintext.shutdown();
});
test('an exporter without the layout the direct channel relies on is refused, naming nothing', () => {
  const { connectGrpcDirect } = load('./dist/src/otlp-header-isolation');
  for (const stand of [{}, { _delegate: {} }, { _delegate: { _transport: { send() {}, _parameters: {} } } }]) {
    assert.throws(() => connectGrpcDirect(stand), error => /^Unsupported OTLP exporter layout: /.test(error.message) && !/synthetic|x-api-key/i.test(error.message));
  }
});
