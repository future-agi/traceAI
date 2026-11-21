// Runs the sealed cross-language plaintext/proxy oracle through the send-level guard. The oracle is plaintext-rule-oracle.json.gz next to this
// file (override the path with FI_ORACLE_ROWS). Both sums, of the .gz bytes and of the JSON after gunzip, are read from the SHA256SUMS file beside
// it; no sum is written in this code. Field names are the file's own: a row is { transport, host, caller, source, opt_in, env, no_proxy, decision,
// error, dialed, tls, key_bytes_allowed, key_bytes_at_sink, proxied_by, fact_keys }; the header carries dimensions.hosts (authority and path with
// the port placeholder {T}) and dimensions.env_values (environment variables with {A}/{B} proxy ports).
//
// What applies to TypeScript (every other row is counted and skipped with its reason):
//   transport  http and eval-config go through the guarded fetch path (http decision); grpc goes through the gRPC decision.
//   caller     none and "*": no dispatcher. client_with_proxy: a caller's proxy dispatcher. uninspectable_transport: a dispatcher the SDK cannot inspect.
//              On gRPC there is no caller-supplied client in this SDK, so client_with_proxy and uninspectable_transport rows are skipped.
//   source     tls: https URL (gRPC: https:// target). http_scheme: http URL (gRPC: http:// target). otel_insecure_env: gRPC only, OTEL_EXPORTER_OTLP_INSECURE=true
//              with a bare target. insecure_argument: gRPC only, caller credentials that are insecure. On HTTP the last two have no TypeScript analog.
//   opt_in     the value of FI_ALLOW_INSECURE_HTTP in the environment ("1", " 1", "true", or unset).
//   env/no_proxy  the proxy variables of the row's env value; NO_PROXY text is not in the file, so none is set (a send the SDK builds disables proxies).
// Compared: decision, error code (through the fixed name table below), dialed target when the row says "target", TLS or plain, keys on the wire.
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
const grpc = load('@grpc/grpc-js');

const CODE_OF = { insecure_futureagi_http: 'FI_PLAINTEXT_FI_HOST', insecure_futureagi_grpc: 'FI_PLAINTEXT_FI_HOST', insecure_self_hosted_target: 'FI_PLAINTEXT_NOT_ALLOWED',
  proxied: 'FI_PLAINTEXT_PROXIED', disputed_authority: 'FI_HOST_MISMATCH', empty_authority: 'FI_EMPTY_AUTHORITY' };
const OPT_IN = { one: '1', space_one: ' 1', true: 'true', unset: undefined };
const EVAL_PORTS = { T: '4318', A: '41001', B: '41002' };

// Stand-ins shaped like undici's dispatchers, as in fixture-rows.test.cjs.
class ProxyAgent {}
class Pool { }
const SCOPED = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy', 'GRPC_PROXY', 'grpc_proxy', 'no_grpc_proxy',
  'NODE_USE_ENV_PROXY', 'FI_API_KEY', 'FI_SECRET_KEY', 'FI_ALLOW_INSECURE_HTTP', 'FI_BASE_URL', 'FI_GRPC_COLLECTOR_ENDPOINT', 'OTEL_EXPORTER_OTLP_HEADERS',
  'OTEL_EXPORTER_OTLP_TRACES_HEADERS', 'OTEL_EXPORTER_OTLP_INSECURE', 'OTEL_EXPORTER_OTLP_TRACES_INSECURE'];
const KEYS = { 'x-api-key': 'synthetic-fi-key-0001', 'x-secret-key': 'synthetic-fi-secret-0001' };

const rowsPath = path.resolve(process.env.FI_ORACLE_ROWS || path.join(__dirname, 'fixtures', 'plaintext-rule-oracle.json.gz'));
const sums = fs.readFileSync(path.join(path.dirname(rowsPath), 'SHA256SUMS'), 'utf8').split('\n').filter(Boolean)
  .map(line => /^([0-9a-f]{64}) [ *](.+)$/.exec(line)).filter(Boolean);
const sumOf = name => { const entry = sums.find(([, , entryName]) => entryName === name); return entry && entry[1]; };
const sha256 = buffer => crypto.createHash('sha256').update(buffer).digest('hex');
const bytes = fs.readFileSync(rowsPath);
const json = zlib.gunzipSync(bytes);

test('the oracle matches its sidecar sums: the .gz bytes and the JSON after gunzip', () => {
  const name = path.basename(rowsPath), jsonName = name.replace(/\.gz$/, '');
  assert.notEqual(jsonName, name);
  assert.ok(sumOf(name), `SHA256SUMS has no entry for ${name}`);
  assert.ok(sumOf(jsonName), `SHA256SUMS has no entry for ${jsonName}`);
  assert.equal(sha256(bytes), sumOf(name));
  assert.equal(sha256(json), sumOf(jsonName));
});

const oracle = JSON.parse(json.toString('utf8'));
const hosts = oracle.dimensions.hosts, envValues = oracle.dimensions.env_values;

/** The row as the SDK sees it, or { skip: reason }. */
function adapt(row, ports = EVAL_PORTS) {
  const http = row.transport === 'http' || row.transport === 'eval-config';
  const host = hosts[row.host];
  if (!host) return { skip: `unknown host ${row.host}` };
  const caller = row.caller === '*' ? 'none' : row.caller;
  if (!http && caller !== 'none') return { skip: 'gRPC has no caller-supplied client in this SDK' };
  if (http && (row.source === 'otel_insecure_env' || row.source === 'insecure_argument')) return { skip: `${row.source} has no HTTP analog in this SDK` };
  const authority = host.authority.replace('{T}', ports.T);
  const source = row.source === '*' ? 'tls' : row.source;
  let target, insecureEnv = false, credentials;
  if (http) target = `${source === 'tls' ? 'https' : 'http'}://${authority}${host.path}`;
  else if (source === 'tls') target = `https://${authority}`;
  else if (source === 'http_scheme') target = `http://${authority}`;
  else { target = authority; if (source === 'otel_insecure_env') insecureEnv = true; else credentials = grpc.credentials.createInsecure(); }
  const proxyEnv = Object.fromEntries(Object.entries(envValues[row.env] || {}).map(([k, v]) => [k, v.replace('{A}', ports.A).replace('{B}', ports.B)]));
  const optIn = row.opt_in === '*' ? undefined : OPT_IN[row.opt_in];
  const dispatcher = !http ? undefined : { none: undefined, client_with_proxy: new ProxyAgent(), uninspectable_transport: new Pool() }[caller];
  return { target, transport: http ? 'http' : 'grpc', proxyEnv, optIn, insecureEnv, credentials, dispatcher, caller };
}

/** One adapted row through the decider and then the guard, in a scoped environment, reported in the oracle's terms. */
function evaluate(input) {
  const saved = {};
  for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
  try {
    Object.assign(process.env, input.proxyEnv);
    if (input.optIn !== undefined) process.env.FI_ALLOW_INSECURE_HTTP = input.optIn;
    if (input.insecureEnv) process.env.OTEL_EXPORTER_OTLP_INSECURE = 'true';
    const decision = policy.decideFiRequest({ target: input.target, transport: input.transport, headers: { ...KEYS }, credentials: input.credentials, dispatcher: input.dispatcher });
    const entries = decision.metadata ? guard.metadataEntries(decision.metadata) : guard.headerEntries(decision.headers);
    const sent = guard.guardSend({ transport: input.transport, target: input.target, entries, intended: guard.intendedFrom(decision), secureChannel: decision.tls, dispatcher: input.dispatcher });
    const parsed = endpoint.endpointTarget(input.target, input.transport);
    const host = parsed && endpoint.comparableHost(parsed.hostname);
    return { decision: 'build', code: null, dialed: parsed ? `${host.includes(':') ? `[${host}]` : host}:${parsed.port}` : '', tls: sent.tls, keys: sent.keys };
  } catch (error) {
    if (error && error.name === 'FiKeyRefusal') return { decision: 'refuse', code: error.code, dialed: '', tls: false, keys: false };
    throw error;
  } finally {
    for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; }
  }
}

// The oracle states the dialed target of a build row as "target" or "proxy". For "target" the host:port is the row's authority as the URL parser
// normalises it (one trailing dot dropped, IPv6 in brackets); a "proxy" row's dial is the proxy's and is not compared (the SDK's own send is direct).
function expectedDialed(row) {
  const parsed = new URL(`http://${hosts[row.host].authority.replace('{T}', EVAL_PORTS.T)}`);
  return `${parsed.hostname.replace(/\.$/, '')}:${parsed.port}`;
}
function compare(row, input, got) {
  const problems = [];
  const wantDecision = row.decision;
  if (got.decision !== wantDecision) problems.push(`decision ${got.decision} want ${wantDecision}`);
  const wantCode = row.error === null ? null : CODE_OF[row.error];
  if (got.code !== wantCode) problems.push(`code ${got.code} want ${wantCode}`);
  if (wantDecision === 'build' && got.decision === 'build') {
    if (got.tls !== row.tls) problems.push(`tls ${got.tls} want ${row.tls}`);
    if (got.keys !== row.key_bytes_allowed) problems.push(`keys ${got.keys} want ${row.key_bytes_allowed}`);
    if (row.dialed === 'target' && got.dialed !== expectedDialed(row)) problems.push(`dialed ${got.dialed} want ${expectedDialed(row)}`);
  }
  return problems;
}

const applied = [], skipped = new Map();
for (const row of oracle.rows) {
  const input = adapt(row);
  if (input.skip) { skipped.set(input.skip, (skipped.get(input.skip) || 0) + 1); continue; }
  applied.push([row, input]);
}
const note = text => console.log(`# ${text}`);
test('rows applied and skipped are reported', () => {
  note(`oracle rows ${oracle.rows.length}; applied to TypeScript ${applied.length}; skipped ${oracle.rows.length - applied.length}`);
  for (const [reason, count] of skipped) note(`  skipped ${count}: ${reason}`);
  assert.equal(applied.length + [...skipped.values()].reduce((a, b) => a + b, 0), oracle.rows.length);
});

// Every row where the guard's answer differs from the oracle's is put in one named class. A class is a finding: it is pinned by its row count, so the
// number changes the day the guard changes, and a row that fits no class fails the suite. No row is loosened: a row in a class still differs.
function classify(row, got) {
  const wantRefuse = row.decision === 'refuse';
  if (wantRefuse && got.decision === 'refuse') return 'refused, under another error code';
  if (row.caller === 'uninspectable_transport' && row.source === 'tls' && got.decision === 'refuse') return 'uninspectable dispatcher over TLS is refused, oracle builds';
  if (row.caller === 'client_with_proxy' && row.source === 'tls' && got.decision === 'refuse') return "a caller's proxy dispatcher over TLS is refused, oracle builds";
  if (row.host === 's1_at_in_path' && !wantRefuse) return '@ in the path is refused, oracle builds';
  return 'UNCLASSIFIED';
}
const classes = new Map();
test('every applied row: decision, error code, dialed target, TLS or plain, keys on the wire', () => {
  let passed = 0;
  for (const [row, input] of applied) {
    const got = evaluate(input);
    const problems = compare(row, input, got);
    if (!problems.length) { passed++; continue; }
    const name = classify(row, got);
    const entry = classes.get(name) || { count: 0, example: undefined };
    entry.count++; entry.example = entry.example || { id: `${row.transport}|${row.host}|${row.caller}|${row.source}|${row.opt_in}|${row.env}|${row.no_proxy}`, target: input.target, problems };
    classes.set(name, entry);
  }
  note(`rows passed ${passed} of ${applied.length}`);
  for (const [name, entry] of [...classes].sort()) note(`  FINDING x${entry.count}: ${name}; e.g. ${entry.example.id} ${entry.example.target} -> ${entry.example.problems.join('; ')}`);
  assert.equal(classes.has('UNCLASSIFIED') ? classes.get('UNCLASSIFIED').count : 0, 0, 'rows that fit no finding class');
  const found = Object.fromEntries([...classes].map(([name, entry]) => [name, entry.count]));
  assert.deepEqual(found, PINNED_FINDINGS);
});
// Every class is one where this SDK is at least as strict as the oracle: none builds where the oracle refuses. A refusal under another code is
// listed by code pair in the report (TypeScript code vs the oracle's), counted below.
const PINNED_FINDINGS = {
  '@ in the path is refused, oracle builds': 32,
  "a caller's proxy dispatcher over TLS is refused, oracle builds": 1202,
  'refused, under another error code': 1012,
  'uninspectable dispatcher over TLS is refused, oracle builds': 1202,
};
const CODE_PAIRS = new Map();
test('the refusals under another code, by code pair: TypeScript code vs the oracle code', () => {
  for (const [row, input] of applied) {
    if (row.decision !== 'refuse') continue;
    const got = evaluate(input);
    if (got.decision === 'refuse' && got.code !== CODE_OF[row.error]) { const key = `${got.code} vs ${row.error} (${CODE_OF[row.error]})`; CODE_PAIRS.set(key, (CODE_PAIRS.get(key) || 0) + 1); }
  }
  for (const [pair, count] of [...CODE_PAIRS].sort()) note(`  CODE PAIR x${count}: TypeScript ${pair}`);
  assert.deepEqual(Object.fromEntries(CODE_PAIRS), PINNED_CODE_PAIRS);
});
const PINNED_CODE_PAIRS = {
  'FI_DISPATCHER_UNKNOWN vs proxied (FI_PLAINTEXT_PROXIED)': 932,
  'FI_HOST_MISMATCH vs insecure_self_hosted_target (FI_PLAINTEXT_NOT_ALLOWED)': 48,
  'FI_HOST_MISMATCH vs proxied (FI_PLAINTEXT_PROXIED)': 32,
};

// ---- Key bytes at the sink are MEASURED --------------------------------------------------------------------------------------------
// A refused row's key_bytes_at_sink of 0 is a value the oracle states. Here each refused HTTP row is sent for real: the decision, then the guarded
// fetch with real undici, a marker key of this test's own, a real proxy listener (the ProxyAgent of a client_with_proxy row dials it) and the row's
// target on a loopback sink. net.Server sockets count the bytes that really arrive at the target sink and at both proxy listeners; the count must be 0.
// A send the guard lets through for a row the oracle refuses is a failure of that row whatever the count says.
const net = require('node:net');
const dns = require('node:dns');
const undici = load('undici');
const MARKER = { 'x-api-key': 'MARKER-KEY-7f3a91c2-api', 'x-secret-key': 'MARKER-KEY-7f3a91c2-secret' };
async function listener(label) {
  const state = { label, bytes: 0, connections: 0, servers: [], port: 0 };
  const sockets = new Set();
  const handler = socket => {
    state.connections++; sockets.add(socket); socket.on('error', () => {}); socket.on('close', () => sockets.delete(socket));
    let seen = '';
    socket.on('data', chunk => {
      state.bytes += chunk.length; seen = (seen + chunk.toString('latin1')).slice(-8192);
      if (/\r\n\r\n/.test(seen) && !socket.destroyed) socket.end('HTTP/1.1 502 Bad Gateway\r\nconnection: close\r\ncontent-length: 0\r\n\r\n');
    });
  };
  const first = net.createServer(handler);
  await new Promise((resolve, reject) => { first.once('error', reject); first.listen(0, '127.0.0.1', resolve); });
  state.port = first.address().port; state.url = `http://127.0.0.1:${state.port}`; state.servers.push(first);
  const second = net.createServer(handler); // the same port on ::1, where the machine has it, so a [::1] target reaches the sink too
  await new Promise(resolve => { second.once('error', resolve); second.listen(state.port, '::1', resolve); });
  if (second.listening) state.servers.push(second);
  state.close = () => new Promise(done => { for (const socket of sockets) socket.destroy(); let open = state.servers.length; for (const server of state.servers) server.close(() => { if (--open === 0) done(); }); });
  return state;
}
function allHostsAreLoopback() {
  const original = dns.lookup;
  dns.lookup = (host, options, callback) => { const done = typeof options === 'function' ? options : callback; const all = options && options.all; process.nextTick(() => all ? done(null, [{ address: '127.0.0.1', family: 4 }]) : done(null, '127.0.0.1', 4)); };
  return () => { dns.lookup = original; };
}
const total = sinks => sinks.reduce((sum, sink) => sum + sink.bytes, 0);
const connections = sinks => sinks.reduce((sum, sink) => sum + sink.connections, 0);
const pause = ms => new Promise(done => setTimeout(done, ms));
/** One row's send for real. Returns 'refused' when the guard refused (nothing was sent) or 'attempted' when the guard let the request go on. */
async function sendForReal(input, ports) {
  const saved = {};
  for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
  let dispatcher;
  try {
    Object.assign(process.env, input.proxyEnv);
    if (input.optIn !== undefined) process.env.FI_ALLOW_INSECURE_HTTP = input.optIn;
    dispatcher = { none: undefined, client_with_proxy: new undici.ProxyAgent(`http://127.0.0.1:${ports.A}`), uninspectable_transport: new undici.Pool(`http://127.0.0.1:${ports.T}`) }[input.caller];
    const decision = policy.decideFiRequest({ target: input.target, transport: 'http', headers: { ...MARKER }, dispatcher });
    await guard.guardedFetch({ url: input.target, init: { method: 'POST', headers: decision.headers, body: '{}', dispatcher }, intended: guard.intendedFrom(decision) });
    return 'attempted';
  } catch (error) {
    if (error && error.name === 'FiKeyRefusal') return 'refused';
    return 'attempted'; // the guard did not refuse; the fetch itself failed (an unparsable url, a dial that nothing answers)
  } finally {
    if (dispatcher && dispatcher.close) await dispatcher.close().catch(() => {});
    for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; }
  }
}

test('RED: a deliberately unguarded send makes the counters non-zero', async () => {
  const target = await listener('target'), proxy = await listener('proxy');
  const restore = allHostsAreLoopback();
  try {
    await fetch(`http://collector.example:${target.port}/x`, { method: 'POST', headers: MARKER, body: '{}', signal: AbortSignal.timeout(2000) }).catch(() => {});
    await pause(50);
    assert.ok(target.bytes > 0, 'a raw fetch puts bytes on the target sink');
    const agent = new undici.ProxyAgent(`http://127.0.0.1:${proxy.port}`);
    await undici.fetch('http://collector.example:8099/x', { method: 'POST', headers: MARKER, body: '{}', dispatcher: agent, signal: AbortSignal.timeout(2000) }).catch(() => {});
    await pause(50);
    await agent.close().catch(() => {});
    assert.ok(proxy.bytes > 0, 'a raw send through a proxy agent puts bytes on the proxy listener');
    note(`RED measured: unguarded fetch -> target sink ${target.bytes} bytes; unguarded send through a ProxyAgent -> proxy listener ${proxy.bytes} bytes`);
  } finally { restore(); await target.close(); await proxy.close(); }
});

const EXPORT_SPAN = { name: 'x', kind: 0, spanContext: () => ({ traceId: '0'.repeat(31) + '1', spanId: '0'.repeat(15) + '1', traceFlags: 1 }), startTime: [0, 0], endTime: [0, 1], status: { code: 0 },
  attributes: {}, links: [], events: [], duration: [0, 1], ended: true, resource: { attributes: {} }, instrumentationLibrary: { name: 't' }, instrumentationScope: { name: 't' },
  droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0 };
const measuredClasses = new Map();
test('every refused HTTP row sends 0 bytes to the target sink and to both proxy listeners', async () => {
  const target = await listener('target'), proxyA = await listener('proxyA'), proxyB = await listener('proxyB');
  const ports = { T: String(target.port), A: String(proxyA.port), B: String(proxyB.port) };
  const sinks = [target, proxyA, proxyB];
  const restore = allHostsAreLoopback();
  let rows = 0, refusedClean = 0, bytesSeen = 0;
  try {
    for (const row of oracle.rows) {
      if (row.decision !== 'refuse' || (row.transport !== 'http' && row.transport !== 'eval-config')) continue;
      const input = adapt(row, ports);
      if (input.skip) continue;
      rows++;
      const bytesBefore = total(sinks), connectionsBefore = connections(sinks);
      const outcome = await sendForReal(input, ports);
      if (outcome === 'attempted') await pause(40);
      const bytes = total(sinks) - bytesBefore;
      if (outcome === 'refused' && bytes === 0 && connections(sinks) === connectionsBefore) { refusedClean++; continue; }
      bytesSeen += bytes;
      const name = `${row.host}: the guard let the send go on for a refused row`;
      const entry = measuredClasses.get(name) || { count: 0, bytes: 0, example: `${row.transport}|${row.caller}|${row.source}|${row.opt_in} ${input.target}` };
      entry.count++; entry.bytes += bytes; measuredClasses.set(name, entry);
    }
  } finally { restore(); await Promise.all(sinks.map(sink => sink.close())); }
  note(`measured refused HTTP rows ${rows}: ${refusedClean} refused with 0 bytes and 0 connections at every sink; ${rows - refusedClean} not`);
  for (const [name, entry] of [...measuredClasses].sort()) note(`  FINDING x${entry.count}: ${name}; ${entry.bytes} bytes at the sinks; e.g. ${entry.example}`);
  const found = Object.fromEntries([...measuredClasses].map(([name, entry]) => [name, { count: entry.count, bytes: entry.bytes }]));
  assert.deepEqual(found, PINNED_MEASURED);
});
const PINNED_MEASURED = {};

// Refused gRPC rows are measured too: the exporter is constructed for the row's target with the row's proxy variables pointing at a real counting
// listener (the lower-case variables grpc reads) and a marker key; a refusal must happen at construction, with 0 bytes and 0 connections at the
// listener standing for the target and at the one standing for the proxy. Every row is run; the counts are kept per class (error code, source).
test('every refused gRPC row is refused at construction, with 0 bytes and 0 connections at the target and the proxy listeners', async () => {
  const { createSpanExporter, Transport } = load('./dist/src/otel');
  const target = await listener('target'), proxy = await listener('proxy');
  const ports = { T: String(target.port), A: String(proxy.port), B: String(proxy.port) };
  const classesSeen = new Map();
  let rows = 0, clean = 0;
  try {
    for (const row of oracle.rows) {
      if (row.decision !== 'refuse' || row.transport !== 'grpc') continue;
      const input = adapt(row, ports);
      if (input.skip) continue;
      rows++;
      const saved = {};
      for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
      const before = [total([target, proxy]), connections([target, proxy])];
      let outcome;
      try {
        Object.assign(process.env, { grpc_proxy: proxy.url, https_proxy: proxy.url, http_proxy: proxy.url }, input.proxyEnv);
        if (input.optIn !== undefined) process.env.FI_ALLOW_INSECURE_HTTP = input.optIn;
        if (input.insecureEnv) process.env.OTEL_EXPORTER_OTLP_INSECURE = 'true';
        const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: input.target, headers: { ...MARKER }, ...(input.credentials ? { credentials: input.credentials } : {}), timeoutMillis: 500 });
        outcome = 'built';
        // A row the guard builds is exported once, so a send the guard should have refused puts bytes on a listener and fails the row.
        await new Promise(done => { exporter.export([EXPORT_SPAN], () => done()); setTimeout(done, 1500); });
        await pause(60);
        await exporter.shutdown().catch(() => {});
      } catch (error) { outcome = error && error.name === 'FiKeyRefusal' ? 'refused' : `error ${error && error.name}`; }
      finally { for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; } }
      const bytes = total([target, proxy]) - before[0], opened = connections([target, proxy]) - before[1];
      const name = `${CODE_OF[row.error]}|${row.source}|${row.host.startsWith('s1') || row.host.startsWith('s3') ? row.host : 'host'}`;
      const entry = classesSeen.get(name) || { count: 0, bytes: 0, connections: 0, outcomes: new Set() };
      entry.count++; entry.bytes += bytes; entry.connections += opened; entry.outcomes.add(outcome); classesSeen.set(name, entry);
      if (outcome === 'refused' && bytes === 0 && opened === 0) clean++;
    }
  } finally { await Promise.all([target.close(), proxy.close()]); }
  note(`measured refused gRPC rows ${rows}: ${clean} refused at construction with 0 bytes and 0 connections at the target and proxy listeners`);
  for (const [name, entry] of [...classesSeen].sort()) note(`  gRPC class ${name}: ${entry.count} rows, ${entry.bytes} bytes, ${entry.connections} connections, outcomes ${[...entry.outcomes]}`);
  assert.equal(clean, rows);
  for (const entry of classesSeen.values()) { assert.equal(entry.bytes, 0); assert.equal(entry.connections, 0); assert.deepEqual([...entry.outcomes], ['refused']); }
  assert.deepEqual(Object.fromEntries([...classesSeen].map(([name, entry]) => [name, entry.count])), PINNED_GRPC_CLASSES);
});
// The gRPC measurement can fail: the proxy variables name a real counting listener, and a row the guard builds is exported once, which puts the
// marker key on a listener. A literal-loopback plaintext target is built, so its export is the positive control for the same steps.
test('CONTROL: a gRPC row the guard builds, exported through the measurement steps, puts bytes on the target listener; the proxy variables name the proxy listener', async () => {
  const { createSpanExporter, Transport } = load('./dist/src/otel');
  const target = await listener('target'), proxy = await listener('proxy');
  assert.match(proxy.url, /^http:\/\/127\.0\.0\.1:\d+$/);
  const saved = {};
  for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
  try {
    Object.assign(process.env, { grpc_proxy: proxy.url, https_proxy: proxy.url, http_proxy: proxy.url });
    const exporter = createSpanExporter({ transport: Transport.GRPC, endpoint: `http://127.0.0.1:${target.port}`, headers: { ...MARKER }, timeoutMillis: 500 });
    await new Promise(done => { exporter.export([EXPORT_SPAN], () => done()); setTimeout(done, 1500); });
    await pause(60);
    await exporter.shutdown().catch(() => {});
    assert.ok(target.connections > 0 && target.bytes > 0, 'the built row reached the target listener');
    assert.equal(proxy.bytes, 0, 'a plaintext channel that carries keys is built with proxies off');
    note(`CONTROL measured: a built gRPC row -> ${target.connections} connections, ${target.bytes} bytes at the target listener, ${proxy.bytes} at the proxy listener`);
  } finally { for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; } await Promise.all([target.close(), proxy.close()]); }
});
// Exact per-class counts of the refused gRPC rows: error code | source | host class (s1_* and s3_* by name, every other host together).
const PINNED_GRPC_CLASSES = {
  'FI_EMPTY_AUTHORITY|*|s3_empty': 5,
  'FI_EMPTY_AUTHORITY|*|s3_port_only': 5,
  'FI_EMPTY_AUTHORITY|http_scheme|s3_empty': 3,
  'FI_EMPTY_AUTHORITY|http_scheme|s3_port_only': 3,
  'FI_EMPTY_AUTHORITY|insecure_argument|s3_empty': 3,
  'FI_EMPTY_AUTHORITY|insecure_argument|s3_port_only': 3,
  'FI_EMPTY_AUTHORITY|otel_insecure_env|s3_empty': 3,
  'FI_EMPTY_AUTHORITY|otel_insecure_env|s3_port_only': 3,
  'FI_EMPTY_AUTHORITY|tls|s3_empty': 3,
  'FI_EMPTY_AUTHORITY|tls|s3_port_only': 3,
  'FI_HOST_MISMATCH|*|s1_backslash_futureagi': 5,
  'FI_HOST_MISMATCH|*|s1_backslash_loopback': 5,
  'FI_HOST_MISMATCH|*|s1_pct5c': 5,
  'FI_HOST_MISMATCH|*|s1_v6_userinfo': 8,
  'FI_HOST_MISMATCH|http_scheme|s1_backslash_futureagi': 3,
  'FI_HOST_MISMATCH|http_scheme|s1_backslash_loopback': 3,
  'FI_HOST_MISMATCH|http_scheme|s1_pct5c': 3,
  'FI_HOST_MISMATCH|insecure_argument|s1_backslash_futureagi': 3,
  'FI_HOST_MISMATCH|insecure_argument|s1_backslash_loopback': 3,
  'FI_HOST_MISMATCH|insecure_argument|s1_pct5c': 3,
  'FI_HOST_MISMATCH|otel_insecure_env|s1_backslash_futureagi': 3,
  'FI_HOST_MISMATCH|otel_insecure_env|s1_backslash_loopback': 3,
  'FI_HOST_MISMATCH|otel_insecure_env|s1_pct5c': 3,
  'FI_HOST_MISMATCH|tls|s1_backslash_futureagi': 3,
  'FI_HOST_MISMATCH|tls|s1_backslash_loopback': 3,
  'FI_HOST_MISMATCH|tls|s1_pct5c': 3,
  'FI_PLAINTEXT_FI_HOST|http_scheme|host': 171,
  'FI_PLAINTEXT_FI_HOST|insecure_argument|host': 171,
  'FI_PLAINTEXT_FI_HOST|otel_insecure_env|host': 171,
  'FI_PLAINTEXT_NOT_ALLOWED|http_scheme|host': 2394,
  'FI_PLAINTEXT_NOT_ALLOWED|http_scheme|s1_at_in_path': 24,
  'FI_PLAINTEXT_NOT_ALLOWED|insecure_argument|host': 2394,
  'FI_PLAINTEXT_NOT_ALLOWED|insecure_argument|s1_at_in_path': 24,
  'FI_PLAINTEXT_NOT_ALLOWED|otel_insecure_env|host': 2394,
  'FI_PLAINTEXT_NOT_ALLOWED|otel_insecure_env|s1_at_in_path': 24,
};

test('RED: a deliberately unguarded gRPC client puts the marker key on the wire at a plaintext listener', async () => {
  const target = await listener('target');
  const metadata = new grpc.Metadata(); metadata.set('x-api-key', MARKER['x-api-key']);
  const identity = value => value;
  const Client = grpc.makeGenericClientConstructor({ export: { path: '/t.T/Export', requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity } }, 'T');
  const client = new Client(`127.0.0.1:${target.port}`, grpc.credentials.createInsecure());
  await new Promise(done => client.export(Buffer.from('x'), metadata, { deadline: Date.now() + 700 }, () => done()));
  client.close(); await pause(60);
  assert.ok(target.connections > 0, 'an unguarded client opens a connection');
  note(`RED measured: unguarded gRPC client -> ${target.connections} connections, ${target.bytes} bytes at the target listener`);
  assert.ok(target.bytes > 0);
  await target.close();
});

// ---- The oracle's undici answers against the installed undici ----------------------------------------------------------------------
// The oracle was measured with undici 6.21.0 (header.versions.undici). Here the installed undici is run for real: a plain fetch and an
// EnvHttpProxyAgent, for each http:// host and proxy variable of the rows whose NO_PROXY is "none" (the NO_PROXY text of the other rows is not in the file),
// and the sink that gets the connection is compared with the oracle's `route` for the same fact. A different answer is a finding of the undici version.
test('the installed undici routes as the oracle recorded for undici', async () => {
  const target = await listener('target'), proxyA = await listener('proxyA'), proxyB = await listener('proxyB');
  const ports = { T: String(target.port), A: String(proxyA.port), B: String(proxyB.port) };
  const restore = allHostsAreLoopback();
  const routeOf = async send => {
    const before = [target.connections, proxyA.connections, proxyB.connections];
    await send().catch(() => {}); await pause(20);
    const after = [target.connections, proxyA.connections, proxyB.connections];
    return after[1] > before[1] ? 'sinkA' : after[2] > before[2] ? 'sinkB' : after[0] > before[0] ? 'target' : 'none';
  };
  const saved = {};
  let compared = 0; const differences = [];
  try {
    for (const [key, fact] of Object.entries(oracle.facts)) {
      const [hostKey, scheme, envKey, noProxy] = key.split('|');
      // lo_v6 is left out: whether a [::1] listener exists is a property of the machine (the oracle's target listener was not reachable on ::1), not of undici.
      if (scheme !== 'http' || noProxy !== 'none' || !fact.undici || !hosts[hostKey] || !envValues[envKey] || hostKey === 'lo_v6') continue;
      const url = `http://${hosts[hostKey].authority.replace('{T}', ports.T)}${hosts[hostKey].path}`;
      try { new URL(url); } catch { continue; }
      for (const name of SCOPED) { saved[name] = process.env[name]; delete process.env[name]; }
      for (const [name, value] of Object.entries(envValues[envKey])) process.env[name] = value.replace('{A}', ports.A).replace('{B}', ports.B);
      try {
        for (const [variant, make] of [['noDispatcher', () => undefined], ['envAgent', () => new undici.EnvHttpProxyAgent()]]) {
          const want = fact.undici[variant];
          if (!want) continue;
          const dispatcher = make();
          const got = await routeOf(() => undici.fetch(url, { signal: AbortSignal.timeout(300), ...(dispatcher ? { dispatcher } : {}) }));
          if (dispatcher) await Promise.race([dispatcher.destroy().catch(() => {}), pause(300)]);
          compared++;
          const expected = want.route === 'sinkA' || want.route === 'sinkB' || want.route === 'target' ? want.route : 'none';
          if (got !== expected) differences.push(`${key} ${variant}: installed ${got}, oracle ${expected}`);
        }
      } finally { for (const name of SCOPED) { if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name]; } }
    }
  } finally { restore(); await Promise.all([target, proxyA, proxyB].map(sink => sink.close())); }
  note(`undici ${require('undici/package.json').version} installed, ${oracle.header.versions.undici} in the oracle: ${compared} routes compared, ${differences.length} differ`);
  for (const line of differences.slice(0, 40)) note(`  FINDING (undici version): ${line}`);
  assert.deepEqual(differences, PINNED_UNDICI_DIFFERENCES);
});
const PINNED_UNDICI_DIFFERENCES = [];
