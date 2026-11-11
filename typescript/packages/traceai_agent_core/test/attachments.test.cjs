const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const https = require('node:https');
const { execFileSync, spawn } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { AttachmentManager, AttachmentTransportError } = require('@traceai/fi-core/attachments');
const { captureAgentImages, MAX_AGENT_IMAGE_BYTES, createTurnTracer, agentAttachmentStatus } = require('../dist/src/index.js');
const { agentAttachmentRuntime } = require('../dist/src/attachment-runtime.js');
const { identity, fake } = require('../../fi-core/test/attachments-fixtures.cjs');
const image = () => ({ type: 'image', mimeType: 'image/png', data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==' });
const input = { direction: 'input', purpose: 'model_input', role: 'user' };
const tool = { direction: 'output', purpose: 'tool_result', role: 'tool', toolCallId: 'call_1', capture: { modelInput: true } };
async function fixture(t, overrides = {}, transportOverrides = {}) {
  const f = fake(transportOverrides);
  const manager = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', transport: f.transport, ...overrides });
  t.after(() => manager.shutdown());
  await manager.ready();
  return { ...f, manager };
}

test('default-OFF reads no typed blocks, decodes nothing and makes no request', async t => {
  const f = await fixture(t, { enabled: false });
  const parts = [{ get type() { throw Error('must not inspect denied blocks'); } }];
  assert.deepEqual(captureAgentImages(f.manager, identity, parts, input), []);
  assert.deepEqual(f.calls, []);
});
for (const [key, options] of [
  ['hideInputs', input], ['hideInputMessages', input], ['hideInputText', input],
  ['hideOutputs', tool], ['hideOutputMessages', tool], ['hideOutputText', tool],
  ['hideInputImages', tool], ['hideInputs', tool], ['piiRedaction', input],
  ['hideErrorDetails', { ...tool, capture: { modelInput: true, errorDetail: true } }],
]) test(`${key} denies before base64 access and upload`, async t => {
  const f = await fixture(t, { traceConfig: { [key]: true } });
  let reads = 0;
  const part = { type: 'image', mimeType: 'image/png', get data() { reads++; throw Error('synthetic-secret'); } };
  const result = captureAgentImages(f.manager, identity, [part], options);
  assert.equal(result.length, 1);
  assert.equal(result[0].admission, key === 'piiRedaction' ? 'redaction_unavailable' : 'policy_denied');
  await f.manager.flush();
  assert.equal(reads, 0);
  assert.deepEqual(f.calls, ['bootstrap']);
});
for (const capture of [{ muted: true }, { temporaryChat: true }, { captureContent: false }, { recordInputs: false }])
  test(`integration restriction ${Object.keys(capture)[0]} cannot be overridden`, async t => {
    const f = await fixture(t);
    const part = { ...image(), get data() { throw Error('denied source read'); } };
    assert.equal(captureAgentImages(f.manager, identity, [part], { ...input, capture })[0].admission, 'policy_denied');
    await f.manager.flush(); assert.deepEqual(f.calls, ['bootstrap']);
  });

test('only explicit typed image bytes; arbitrary paths, URLs and nested objects are not fetched', async t => {
  const f = await fixture(t);
  const parts = [
    { type: 'image', mimeType: 'image/svg+xml', data: '<script/>' },
    { type: 'local_image', get path() { throw Error('path-read'); } },
    { type: 'image_url', get url() { throw Error('url-read'); } },
    { type: 'text', nested: image() },
    { type: 'image', mimeType: 'image/png', url: 'https://example.invalid/signed-secret' },
  ];
  const results = captureAgentImages(f.manager, identity, parts, input);
  assert.equal(results.length, 1); assert.equal(results[0].admission, 'source_unavailable');
  await f.manager.flush(); assert.deepEqual(f.calls, ['bootstrap']);
});

test('byte-perfect capture, bounded callback budget and no history replay', async t => {
  const f = await fixture(t);
  const part = image();
  const [first] = captureAgentImages(f.manager, identity, [part], input);
  assert.equal(first.admission, 'queued');
  assert.deepEqual(captureAgentImages(f.manager, identity, [part], input), []);
  await f.manager.flush();
  assert.deepEqual(f.body(), [...Buffer.from(part.data, 'base64')]);
  assert.equal((await first.completion).status, 'validating'); // not playback readiness
  const samePixels = captureAgentImages(f.manager, identity, [image(), image()], input);
  assert.equal(samePixels.length, 2);
  assert.notEqual(samePixels[0].occurrenceId, samePixels[1].occurrenceId);
  const large = { ...image(), data: Buffer.alloc(MAX_AGENT_IMAGE_BYTES + 1).toString('base64') };
  assert.equal(captureAgentImages(f.manager, identity, [large], input)[0].admission, 'source_unavailable');
  const half = () => ({ ...image(), data: Buffer.alloc(MAX_AGENT_IMAGE_BYTES / 2).toString('base64') });
  assert.deepEqual(captureAgentImages(f.manager, identity, [half(), half(), half()], input).map(h => h.admission), ['queued', 'queued', 'source_unavailable']);
});

test('unknown bootstrap and unsupported scope never read media', async t => {
  const f = await fixture(t, {}, { bootstrap: async () => { throw new AttachmentTransportError('credential_scope_unsupported'); } });
  assert.equal(f.manager.status().state, 'credential_scope_unsupported');
  assert.deepEqual(captureAgentImages(f.manager, identity, [image()], input), []);
});

test('tightening policy after capture cancels before any upload', async t => {
  const f = await fixture(t);
  const [h] = captureAgentImages(f.manager, identity, [image()], input);
  f.manager.updatePolicy({ traceConfig: { hideInputs: true } });
  await f.manager.flush();
  assert.equal((await h.completion).status, 'policy_denied');
  assert.deepEqual(f.calls, ['bootstrap']);
});

async function networkFixture(t, configOverrides = {}, runtimeOverrides = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-media-'));
  const requests = [];
  const f = fake();
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const bytes = Buffer.concat(chunks);
    const body = req.url === '/upload' ? bytes : JSON.parse(bytes.toString());
    requests.push({ path: req.url, body });
    const result = req.url === '/bootstrap' ? await f.transport.bootstrap(body) :
      req.url === '/initialize' ? await f.transport.initialize(null, body) : {};
    res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(result));
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const send = async (route, body, signal) => (await fetch(origin + route, {
    method: 'POST', body: body instanceof Uint8Array ? body : JSON.stringify(body), signal,
  })).json();
  const transport = {
    bootstrap: (body, signal) => send('/bootstrap', body, signal),
    initialize: (session, body, signal) => send('/initialize', body, signal),
    upload: (session, upload, bytes, signal) => send('/upload', bytes, signal),
    complete: (session, upload, body, signal) => send('/complete', body, signal),
  };
  const config = { agent: 'pi', enabled: true, captureContent: true, projectName: 'media', endpoint: origin + '/traces',
    credentials: { apiKey: 'synthetic-key', secretKey: 'synthetic-secret' }, tags: [], generation: 0, destinationApproved: true,
    hideInputs: false, hideOutputs: false, hideErrorDetails: false, provenance: {}, issues: [],
    paths: { manifest: path.join(home, 'manifest.json'), stateDir: home }, ...configOverrides };
  const tracer = createTurnTracer({ config, attachments: { enabled: true, transport, ...runtimeOverrides } });
  t.after(async () => { await tracer.discard(); server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  await tracer.attachments.ready();
  return { tracer, requests, config };
}
for (const denial of ['disabled', 'content-off', 'hide-inputs', 'hide-images', 'pii'])
  test(`actual network has no binary egress for ${denial}`, async t => {
    const f = await networkFixture(t,
      denial === 'content-off' ? { captureContent: false } : denial === 'hide-inputs' ? { hideInputs: true } : {},
      denial === 'disabled' ? { enabled: false } : denial === 'hide-images' ? { traceConfig: { hideInputImages: true } } : denial === 'pii' ? { traceConfig: { piiRedaction: true } } : {});
    const turn = f.tracer.startTurn({ sessionId: 's', turnId: 't' });
    turn.images([image()], input); turn.end(); await f.tracer.shutdown();
    assert.equal(f.requests.filter(r => ['/initialize', '/upload', '/complete'].includes(r.path)).length, 0);
    assert.equal(JSON.stringify(f.requests).includes(image().data), false);
  });

test('real provider lifecycle emits exact span correlation and no payload in OTLP', async t => {
  const f = await networkFixture(t);
  const turn = f.tracer.startTurn({ sessionId: 's', turnId: 't' });
  const child = turn.child({ id: 'tool', kind: 'tool', toolCallId: 'call_1' });
  child.images([image()], tool);
  child.end(); turn.end(); await f.tracer.shutdown();
  const upload = f.requests.find(r => r.path === '/initialize').body;
  assert.equal(upload.association.parent.span_id, child.context.spanId);
  assert.equal(upload.association.parent.trace_id.replaceAll('-', ''), child.context.traceId);
  assert.equal(upload.tool_call_id, 'call_1');
  assert.deepEqual(f.requests.find(r => r.path === '/upload').body, Buffer.from(image().data, 'base64'));
  const traces = f.requests.filter(r => r.path === '/traces');
  assert.ok(traces.length);
  const serialized = JSON.stringify(traces);
  assert.ok(serialized.includes('fi.attachment.refs')); assert.ok(serialized.includes(upload.occurrence_id));
  assert.equal(serialized.includes(image().data), false);
  const before = f.requests.length; child.images([image()], tool); await f.tracer.flush();
  assert.equal(f.requests.length, before);
});

test('discard and async route revocation prevent binary delivery', async t => {
  let allowed = true;
  const f = await networkFixture(t, { revalidate: async () => allowed });
  const turn = f.tracer.startTurn({ sessionId: 's', turnId: 't' });
  turn.images([image()], input); allowed = false;
  await f.tracer.flush();
  assert.equal(f.requests.filter(r => r.path === '/upload').length, 0);
  await f.tracer.discard();
  assert.equal(f.tracer.attachments.status().state, 'policy_denied');
});

test('status distinguishes requested capture, unsupported sources and unverified scope', () => {
  const config = { agent: 'pi', enabled: true, captureContent: true, endpoint: 'https://example.invalid/tracer/v1/traces' };
  assert.equal(agentAttachmentStatus(config, {}).state, 'disabled');
  assert.equal(agentAttachmentStatus(config, { FI_ATTACHMENTS_ENABLED: 'true' }).state, 'endpoint_unconfigured');
  const enabled = { FI_ATTACHMENTS_ENABLED: 'true', FI_ATTACHMENTS_ENDPOINT: 'https://example.invalid' };
  assert.equal(agentAttachmentStatus(config, enabled).state, 'scope_unverified');
  // An endpoint on any origin other than the approved OTLP destination is not configured.
  for (const other of ['https://other.invalid', 'https://example.invalid:8443', 'http://example.invalid', 'not a url'])
    assert.equal(agentAttachmentStatus(config, { ...enabled, FI_ATTACHMENTS_ENDPOINT: other }).state, 'endpoint_unconfigured', other);
  assert.equal(agentAttachmentStatus({ ...config, endpoint: undefined }, enabled).state, 'endpoint_unconfigured');
  for (const agent of ['pi', 'oh-my-pi', 'claude']) {
    assert.equal(agentAttachmentStatus({ ...config, agent }, enabled).source, 'typed-image-blocks');
  }
  // Every adapter id without an inline typed source, plus unknown/prototype keys, fails closed.
  for (const agent of ['codex', 'cursor', 'cursor-cli', 'cursor-cloud', 'copilot', 'copilot-vscode', 'goose', 'hermes',
    'kiro-cli', 'kiro-ide', 'opencode', 'opencode-v1', 'openclaw', 'gemini', 'antigravity', 'antigravity-ide', 'augment',
    'augment-ide', 'devin-cascade', 'grok', 'muse', 'unknown-host', 'constructor', '__proto__']) {
    assert.equal(agentAttachmentStatus({ ...config, agent }, enabled).state, 'source_unavailable', agent);
  }
  assert.equal(agentAttachmentStatus({ ...config, hideInputs: true, hideOutputs: true }, enabled).state, 'policy_denied');
  assert.equal(agentAttachmentStatus({ ...config, captureContent: false }, enabled).state, 'policy_denied');
});

test('Anthropic/Claude Code base64 source blocks share the same lazy boundary; URL/file sources are never read', async t => {
  const f = await fixture(t);
  const data = image().data;
  let urlReads = 0;
  const parts = [
    { type: 'image', source: { type: 'base64', media_type: 'image/png', data } },
    { type: 'image', source: { type: 'url', get url() { urlReads++; return 'https://example.invalid/secret'; } } },
    { type: 'image', source: { type: 'file', file_id: 'file_secret' } },
    { type: 'image', source: { type: 'base64', media_type: 'image/svg+xml', data } },
  ];
  const results = captureAgentImages(f.manager, identity, parts, input);
  assert.deepEqual(results.map(r => r.admission), ['queued']);
  await f.manager.flush();
  assert.deepEqual(f.body(), [...Buffer.from(data, 'base64')]);
  assert.equal(urlReads, 0);
  const hidden = await fixture(t, { traceConfig: { hideInputImages: true } });
  let reads = 0;
  const secret = { type: 'image', source: { type: 'base64', media_type: 'image/png', get data() { reads++; return data; } } };
  assert.equal(captureAgentImages(hidden.manager, identity, [secret], input)[0].admission, 'policy_denied');
  assert.equal(reads, 0);
});

test('runtime binds the credential to the approved destination origin', () => {
  const config = { agent: 'pi', enabled: true, captureContent: true, endpoint: 'https://approved.invalid/tracer/v1/traces',
    credentials: { apiKey: 'synthetic-key', secretKey: 'synthetic-secret' }, destinationApproved: true, projectName: 'p',
    hideInputs: false, hideOutputs: false, hideErrorDetails: false };
  const same = agentAttachmentRuntime(config, { endpoint: 'https://approved.invalid' });
  assert.equal(same.endpoint, 'https://approved.invalid');
  assert.equal(same.headers['x-api-key'], 'synthetic-key');
  for (const endpoint of ['https://other.invalid', 'https://approved.invalid:8443', 'http://approved.invalid']) {
    const other = agentAttachmentRuntime(config, { endpoint });
    assert.equal(other.endpoint, undefined, endpoint);
    assert.deepEqual(other.headers, {}, endpoint);
    // A transport is always supplied, so fi-core never builds its own from FI_ATTACHMENTS_ENDPOINT.
    assert.equal(typeof other.transport?.bootstrap, 'function', endpoint);
  }
});

// Real saved profile, real resolveConfig and real Node TLS transport, in a child process so the
// self-signed test CA is trusted through NODE_EXTRA_CA_CERTS (read only at process start).
test('saved-profile credential never reaches an FI_ATTACHMENTS_ENDPOINT origin destination approval did not approve', async t => {
  let tls;
  try {
    tls = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-att-origin-'));
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', path.join(tls, 'key.pem'),
      '-out', path.join(tls, 'cert.pem'), '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], { stdio: 'ignore' });
  } catch { t.skip('openssl unavailable'); return; }
  t.after(() => fs.rm(tls, { recursive: true, force: true }));
  const tlsOptions = { key: await fs.readFile(path.join(tls, 'key.pem')), cert: await fs.readFile(path.join(tls, 'cert.pem')) };
  async function server() {
    const seen = [];
    const s = https.createServer(tlsOptions, (req, res) => {
      req.resume();
      req.on('end', () => {
        seen.push({ url: req.url, apiKey: req.headers['x-api-key'], secretKey: req.headers['x-secret-key'] });
        if (req.url.startsWith('/tracer/v1/traces')) { res.setHeader('content-type', 'application/json'); res.end('{}'); }
        else res.writeHead(503).end();
      });
    });
    await new Promise(r => s.listen(0, '127.0.0.1', r));
    t.after(async () => { s.closeAllConnections(); await new Promise(r => s.close(r)); });
    return { origin: `https://127.0.0.1:${s.address().port}`, seen };
  }
  const approved = await server();
  const other = await server();
  const dist = path.resolve(__dirname, '../dist/src/index.js');
  const script = `
    const c = require(${JSON.stringify(dist)});
    (async () => {
      const options = { agent: 'pi', home: process.env.HOME, env: {} };
      const setup = await c.setup({ ...options, credentials: { apiKey: 'synthetic-saved-api', secretKey: 'synthetic-saved-secret' },
        projectName: 'unit-project', otlpEndpoint: process.env.APPROVED + '/tracer/v1/traces', configureOnly: true });
      const config = await c.resolveConfig(options);
      const redirected = await c.resolveConfig({ ...options, env: { FI_BASE_URL: process.env.OTHER } });
      const tracer = c.createTurnTracer({ config });
      await tracer.attachments.ready(3000);
      const state = tracer.attachments.status().state;
      await tracer.shutdown();
      console.log('RESULT ' + JSON.stringify({ setup: setup.code, approved: config.destinationApproved, enabled: config.enabled,
        redirectedApproved: redirected.destinationApproved, redirectedIssues: redirected.issues, state,
        status: c.agentAttachmentStatus(config).state }));
    })().catch(error => { console.error(error); process.exitCode = 1; });
  `;
  async function child(attachmentsEndpoint) {
    const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-att-home-'));
    t.after(() => fs.rm(home, { recursive: true, force: true }));
    const proc = spawn(process.execPath, ['-e', script], { stdio: ['ignore', 'pipe', 'pipe'], env: {
      PATH: process.env.PATH, HOME: home, NODE_EXTRA_CA_CERTS: path.join(tls, 'cert.pem'),
      APPROVED: approved.origin, OTHER: other.origin + '/',
      FI_ATTACHMENTS_ENABLED: 'true', FI_ATTACHMENTS_ENDPOINT: attachmentsEndpoint } });
    let out = '', err = '';
    proc.stdout.on('data', d => { out += d; }); proc.stderr.on('data', d => { err += d; });
    const [code] = await once(proc, 'exit');
    assert.equal(code, 0, err);
    const line = out.split('\n').find(l => l.startsWith('RESULT '));
    assert.ok(line, out + err);
    return JSON.parse(line.slice(7));
  }
  const attachmentPaths = seen => seen.filter(r => r.url.includes('attachment'));

  // Unapproved origin: the same origin the OTLP route itself refuses (control) is refused here too.
  const denied = await child(other.origin + '/');
  assert.equal(denied.setup, 'CONFIGURED_NOT_REGISTERED');
  assert.equal(denied.approved, true);
  assert.equal(denied.redirectedApproved, false);
  assert.ok(denied.redirectedIssues.includes('DESTINATION_NOT_APPROVED'));
  assert.equal(denied.state, 'endpoint_unconfigured');
  assert.equal(denied.status, 'endpoint_unconfigured');
  assert.deepEqual(other.seen, []);
  assert.deepEqual(attachmentPaths(approved.seen), []);

  // Positive control: the approved origin does receive the bootstrap, with the saved credential.
  const allowed = await child(approved.origin + '/');
  assert.equal(allowed.status, 'scope_unverified');
  const boot = attachmentPaths(approved.seen);
  assert.ok(boot.length >= 1);
  assert.ok(boot.every(r => r.apiKey === 'synthetic-saved-api' && r.secretKey === 'synthetic-saved-secret'));
  assert.deepEqual(other.seen, []);
});
