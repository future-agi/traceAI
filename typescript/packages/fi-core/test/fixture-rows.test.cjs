// Runs rows of a cross-language plaintext-rule fixture through the guard. The rows come from a gzipped JSON file named by FI_FIXTURE_ROWS
// (default: the sample next to this file). The file's sha256 is compared with the entry for its name in the SHA256SUMS file beside it; no
// sum is written in this code. Each row's input goes through the guard in a scoped environment and the outcome must equal the row's expectation:
// decision, error code, dialed target (host:port), TLS or plain, keys on the wire.
//
// File shape: { header: { format: "fi-plaintext-rule-rows", version: 1, ... }, rows: [{ id, input, expect }] }
//   input:  { target, transport: "http"|"grpc", keys: "explicit"|"env"|"none", optIn: "none"|"env"|"option",
//             proxyEnv: { NAME: value }, dispatcher: "none"|"agent"|"agent-connect"|"agent-socketpath"|"agent-factory"|"proxy"|"envproxy"|"pool-same"|"pool-other"|"pool-unknown" }
//   expect: { decision: "allow"|"refuse", code: "FI_..."|null, dialed: "host:port", tls: boolean, keysOnWire: boolean }
//   dialed: the host as the transport receives it (the URL parser's normalised host, one trailing dot dropped, IPv6 in brackets) and the port
//   (written, else the scheme's default; 443 for a bare gRPC target). A refused row has dialed "", tls false, keysOnWire false.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
const guard = load('./dist/src/fi-send-guard');
const policy = load('./dist/src/fi-request-policy');
const endpoint = load('./dist/src/fi-endpoint');

// Stand-ins shaped like undici's dispatchers: the guard reads a dispatcher's class name and its recorded options and factory as data, and an
// origin-bound one's url symbol. An Agent is "plain" when it records no connect, socketPath or factory option and has the default factory.
function defaultFactory(origin, opts) { return opts && opts.connections === 1 ? new Client(origin, opts) : new Pool(origin, opts) }
class Client {}
class Pool { constructor(origin) { if (origin) Object.defineProperty(this, Symbol('url'), { value: new URL(origin), enumerable: false }); } }
function agent(options = {}, factory = defaultFactory) {
  class Agent {}
  Object.defineProperty(Agent, 'name', { value: 'Agent' });
  const value = new Agent();
  Object.defineProperty(value, Symbol('options'), { value: options, enumerable: false });
  Object.defineProperty(value, Symbol('factory'), { value: factory, enumerable: false });
  return value;
}
class ProxyAgent {}
class EnvHttpProxyAgent {}
const SCOPED = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy', 'GRPC_PROXY', 'grpc_proxy', 'no_grpc_proxy',
  'NODE_USE_ENV_PROXY', 'FI_API_KEY', 'FI_SECRET_KEY', 'FI_ALLOW_INSECURE_HTTP', 'FI_BASE_URL', 'FI_GRPC_COLLECTOR_ENDPOINT', 'OTEL_EXPORTER_OTLP_HEADERS', 'OTEL_EXPORTER_OTLP_TRACES_HEADERS'];
const KEYS = { 'x-api-key': 'synthetic-fi-key-0001', 'x-secret-key': 'synthetic-fi-secret-0001' };

/** One row through the decider and then the guard, in a scoped environment, reported in the row shape. */
function evaluateRow(row) {
  const saved = {};
  for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
  try {
    Object.assign(process.env, row.proxyEnv);
    if (row.optIn === 'env') process.env.FI_ALLOW_INSECURE_HTTP = '1';
    if (row.keys === 'env') { process.env.FI_API_KEY = 'env-key-synthetic-0002'; process.env.FI_SECRET_KEY = 'env-secret-synthetic-0002'; }
    const headers = row.keys === 'explicit' ? { ...KEYS } : row.keys === 'none' ? {} : undefined;
    const allowInsecureHttp = row.optIn === 'option' ? true : undefined;
    const origin = row.transport === 'http' ? new URL(row.target).origin : undefined;
    const dispatcher = { none: undefined, agent: agent(), 'agent-connect': agent({ connect() {} }), 'agent-socketpath': agent({ connect: { socketPath: '/tmp/x.sock' } }),
      'agent-factory': agent({}, function customFactory(origin, opts) { return new Pool(origin, opts); }), proxy: new ProxyAgent(), envproxy: new EnvHttpProxyAgent(),
      'pool-same': origin ? new Pool(origin) : undefined, 'pool-other': new Pool('http://elsewhere.example:9'), 'pool-unknown': new Pool() }[row.dispatcher];
    const decision = policy.decideFiRequest({ target: row.target, transport: row.transport, headers, allowInsecureHttp, dispatcher });
    const entries = decision.metadata ? guard.metadataEntries(decision.metadata) : guard.headerEntries(decision.headers);
    const sent = guard.guardSend({ transport: row.transport, target: row.target, entries, intended: guard.intendedFrom(decision), secureChannel: decision.tls, dispatcher, allowInsecureHttp });
    const parsed = endpoint.endpointTarget(row.target, row.transport);
    const host = endpoint.comparableHost(parsed.hostname);
    return { decision: 'allow', code: null, dialed: `${host.includes(':') ? `[${host}]` : host}:${parsed.port}`, tls: sent.tls, keysOnWire: sent.keys };
  } catch (error) {
    if (error && error.name === 'FiKeyRefusal') return { decision: 'refuse', code: error.code, dialed: '', tls: false, keysOnWire: false };
    throw error;
  } finally {
    for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; }
  }
}

const rowsPath = path.resolve(process.env.FI_FIXTURE_ROWS || path.join(__dirname, 'fixtures', 'plaintext-rule-SAMPLE.rows.json.gz'));
const sums = fs.readFileSync(path.join(path.dirname(rowsPath), 'SHA256SUMS'), 'utf8').split('\n').filter(Boolean)
  .map(line => /^([0-9a-f]{64}) [ *](.+)$/.exec(line)).filter(Boolean);
const bytes = fs.readFileSync(rowsPath);
const json = zlib.gunzipSync(bytes);
const sumOf = name => { const entry = sums.find(([, , entryName]) => entryName === name); return entry && entry[1]; };
const sha256 = buffer => crypto.createHash('sha256').update(buffer).digest('hex');

// Both sums come from the sidecar: the .gz bytes under their own name, and the decompressed JSON under the name without .gz.
test('the fixture file matches its sidecar sums: the .gz bytes and the JSON after gunzip', () => {
  const name = path.basename(rowsPath);
  assert.ok(sumOf(name), `SHA256SUMS has no entry for ${name}`);
  assert.equal(sha256(bytes), sumOf(name));
  const jsonName = name.replace(/\.gz$/, '');
  assert.notEqual(jsonName, name, 'the fixture is a .gz file');
  assert.ok(sumOf(jsonName), `SHA256SUMS has no entry for ${jsonName}`);
  assert.equal(sha256(json), sumOf(jsonName));
});

const document = JSON.parse(json.toString('utf8'));
test('the header names the format', () => {
  assert.equal(document.header.format, 'fi-plaintext-rule-rows');
  assert.ok(Array.isArray(document.rows) && document.rows.length > 0);
});
for (const row of document.rows) {
  test(row.id, () => { assert.deepEqual(evaluateRow(row.input), row.expect); });
}

// ---- Key bytes at the sink are MEASURED, not read from a row ---------------------------------------------------------------
// A refused row's expected key_bytes_at_sink of 0 is a value the oracle states. Here the same decision is run for real, with a marker key of
// this test's own, against a loopback sink that counts the marker bytes it receives, and the count must be 0 for a refused row and positive
// for an allowed one. A deliberately unguarded send (a raw fetch) must make the same counter non-zero: that is the check that the counter counts.
const net = require('node:net');
const dns = require('node:dns');
const MARKER = { 'x-api-key': 'MARKER-KEY-7f3a91c2-api', 'x-secret-key': 'MARKER-KEY-7f3a91c2-secret' };
async function markerSink(port = 0) {
  const state = { port: 0, markerBytes: 0, bytes: 0, connections: 0 };
  const sockets = new Set();
  const server = net.createServer(socket => {
    state.connections++; sockets.add(socket); socket.on('error', () => {}); socket.on('close', () => sockets.delete(socket));
    let tail = '';
    socket.on('data', chunk => {
      state.bytes += chunk.length;
      const text = tail + chunk.toString('latin1');
      for (const value of Object.values(MARKER)) { let at = text.indexOf(value); while (at >= 0) { state.markerBytes += value.length; at = text.indexOf(value, at + value.length); } }
      tail = text.slice(-64);
      // answer, so an allowed send completes
      if (/\r\n\r\n/.test(text) && !socket.destroyed) socket.end('HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}');
    });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  state.port = server.address().port;
  state.close = () => new Promise(done => { for (const socket of sockets) socket.destroy(); server.close(() => done()); });
  return state;
}
/** Every host name resolves to this machine for the duration of one measured send. */
function allHostsAreLoopback() {
  const original = dns.lookup;
  dns.lookup = (host, options, callback) => { const done = typeof options === 'function' ? options : callback; const wantsAll = options && options.all; process.nextTick(() => wantsAll ? done(null, [{ address: '127.0.0.1', family: 4 }]) : done(null, '127.0.0.1', 4)); };
  return () => { dns.lookup = original; };
}
function scoped(row, run) {
  const saved = {};
  for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
  try {
    Object.assign(process.env, row.proxyEnv);
    if (row.optIn === 'env') process.env.FI_ALLOW_INSECURE_HTTP = '1';
    if (row.keys === 'env') { process.env.FI_API_KEY = MARKER['x-api-key']; process.env.FI_SECRET_KEY = MARKER['x-secret-key']; }
    return run();
  } finally { for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; } }
}
/** The row's send for real: the decision, then the guarded fetch, to a sink on the row's own port. Returns whether the send was refused. */
async function sendForReal(row) {
  return scoped(row, async () => {
    const headers = row.keys === 'explicit' ? { ...MARKER } : row.keys === 'none' ? {} : undefined;
    try {
      const decision = policy.decideFiRequest({ target: row.target, transport: 'http', headers, allowInsecureHttp: row.optIn === 'option' ? true : undefined });
      await guard.guardedFetch({ url: row.target, init: { method: 'POST', headers: decision.headers, body: '{}' }, intended: guard.intendedFrom(decision), allowInsecureHttp: row.optIn === 'option' ? true : undefined });
      return false;
    } catch (error) { if (error && error.name === 'FiKeyRefusal') return true; return false; }
  });
}
const measurable = row => !row.target.includes('[') && row.transport === 'http' && row.dispatcher === 'none' && row.keys !== 'none' && /^http:\/\/[^/?#\\@]*:(\d{4,5})(?:[/?#]|$)/.test(row.target)
  && Number(/:(\d{4,5})(?:[/?#]|$)/.exec(row.target)[1]) >= 1024;
test('a deliberately unguarded send makes the counter non-zero', async () => {
  const sink = await markerSink();
  try {
    await fetch(`http://127.0.0.1:${sink.port}/x`, { method: 'POST', headers: MARKER, body: '{}' }).catch(() => {});
    assert.ok(sink.markerBytes > 0, 'the counter counts the marker key bytes of a raw fetch');
  } finally { await sink.close(); }
});
for (const row of document.rows.filter(candidate => measurable(candidate.input))) {
  test(`measured at the sink: ${row.id}`, async () => {
    const input = row.input;
    const port = Number(/:(\d{4,5})(?:[/?#]|$)/.exec(input.target)[1]);
    let sink;
    try { sink = await markerSink(port); } catch { return; /* the row's port is taken on this machine */ }
    const restore = allHostsAreLoopback();
    try {
      const refused = await sendForReal(input);
      await new Promise(done => setTimeout(done, 100));
      if (row.expect.decision === 'refuse') { assert.equal(refused, true); assert.equal(sink.markerBytes, 0, 'a refused row put key bytes on the wire'); assert.equal(sink.bytes, 0); }
      else { assert.equal(refused, false); assert.ok(sink.markerBytes > 0, 'an allowed row sent its keys'); }
    } finally { restore(); await sink.close(); }
  });
}
