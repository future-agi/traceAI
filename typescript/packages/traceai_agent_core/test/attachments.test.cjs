const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { AttachmentManager, AttachmentTransportError } = require('@traceai/fi-core/attachments');
const { captureAgentImages, MAX_AGENT_IMAGE_BYTES, createTurnTracer, agentAttachmentStatus } = require('../dist/src/index.js');
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
  const config = { agent: 'pi', enabled: true, captureContent: true };
  assert.equal(agentAttachmentStatus(config, {}).state, 'disabled');
  assert.equal(agentAttachmentStatus(config, { FI_ATTACHMENTS_ENABLED: 'true' }).state, 'endpoint_unconfigured');
  const enabled = { FI_ATTACHMENTS_ENABLED: 'true', FI_ATTACHMENTS_ENDPOINT: 'https://example.invalid' };
  assert.equal(agentAttachmentStatus(config, enabled).state, 'scope_unverified');
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
