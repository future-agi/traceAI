/* Cloud component qualification: real SDKs, synthetic media, loopback-only APIs. */
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { randomUUID } = require('node:crypto');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
const { AttachmentManager, createAttachmentHttpTransport } = require('@traceai/fi-core/attachments');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OpenAIInstrumentation } = require('../dist/src/instrumentation.js');
const { AnthropicInstrumentation } = require('../../traceai_anthropic/dist/src/instrumentation.js');
const oa = require('../dist/src/attachments.js');
const an = require('../../traceai_anthropic/dist/src/attachments.js');
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=';
const PDF = Buffer.from('%PDF-1.7\n% synthetic document\n%%EOF').toString('base64');
const image = () => ({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG } });
const document = () => ({ type: 'document', title: 'synthetic-private-filename', source: { type: 'base64', media_type: 'application/pdf', data: PDF } });
const identity = () => ({ traceId: randomUUID().replace(/-/g, ''), spanId: randomUUID().replace(/-/g, '').slice(0, 16), traceFlags: 1 });
const keepalive = setInterval(() => {}, 1000);
after(() => clearInterval(keepalive));
// The core reports a silently degraded fixture (legacy transport, rejected F01 shape) as a process warning.
const coreWarnings = [];
process.on('warning', warning => { if (/^FI_ATTACHMENTS_/.test(warning.code ?? '')) coreWarnings.push(warning.code); });
// Preflight tickets that are not used must be released at once, never left to the TTL sweep.
const assertNoOpenTickets = async manager => {
  assert.equal(manager.status().openTickets, 0, 'open tickets'); await manager.flush(); assert.equal(manager.status().ownedBytes, 0, 'owned bytes');
};

async function fixture(config = {}, serverPolicy = {}, serverLimits = {}) {
  const calls = [], initialized = [], uploaded = [];
  const scope = { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() };
  // Sessions of one hour or more: the core renews within max(60 s, 20% of lifetime) of expiry.
  const expiry = () => new Date(Date.now() + 3600000).toISOString();
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const c of req) chunks.push(c);
    const bytes = Buffer.concat(chunks); calls.push(req.url);
    res.setHeader('content-type', 'application/json');
    if (req.url.endsWith('attachment-sessions')) {
      const body = JSON.parse(bytes);
      res.end(JSON.stringify({ schema_version: 1, scope, capture_session_id: body.capture_session_id,
        policy: { version: 'fixture-v1', enabled: true, allowed_sources: ['Bytes'], allowed_modalities: ['image', 'document', 'file'],
          require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false,
          record_audio: false, record_video: false, ...serverPolicy },
        limits: { max_upload_bytes: 1024 * 1024, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824,
          recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024,
          initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824,
          transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5, ...serverLimits }, expires_at: expiry(), transfer_modes: ['authenticated_stream'] }));
    } else if (req.url.endsWith('/uploads')) {
      const body = JSON.parse(bytes); initialized.push(body);
      res.statusCode = 201;
      res.end(JSON.stringify({ upload_id: body.attachment_id, attachment_id: body.attachment_id, occurrence_id: body.occurrence_id,
        state: 'pending_upload', reserved_bytes: body.declared_bytes, expires_at: expiry(), transfer_mode: 'authenticated_stream', write_capability: 'synthetic-upload-only' }));
    } else if (req.url.endsWith('/content')) {
      assert.equal(req.headers.authorization, 'Bearer synthetic-upload-only'); uploaded.push(bytes);
      res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'pending_upload', reason: 'queued' }));
    } else {
      res.statusCode = 202; res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'validating', reason: 'binding_pending' }));
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const client = req => new Promise((resolve, reject) => {
    const request = http.request(`http://127.0.0.1:${server.address().port}${new URL(req.url).pathname}`, { method: req.method, headers: req.headers, signal: req.signal }, response => {
      const chunks = []; response.on('data', c => chunks.push(c)); response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(chunks) }));
    }); request.on('error', reject); request.end(req.body);
  });
  const manager = new AttachmentManager({ enabled: true, projectName: 'synthetic', projectType: 'observe',
    transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic' }, client }), ...config });
  assert.equal(await manager.ready(), config.enabled !== false);
  return { manager, calls, initialized, uploaded, async close() { await manager.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}
const oaInput = () => ({ input: [
  { role: 'user', content: [{ type: 'input_image', image_url: `data:image/png;base64,${PNG}` }, { type: 'input_file', filename: 'synthetic-private-filename', file_data: `data:application/pdf;base64,${PDF}` }] },
  { type: 'computer_call_output', call_id: 'call_computer', output: { type: 'computer_screenshot', image_url: `data:image/png;base64,${PNG}` } },
] });
const anInput = () => ({ tools: [{ type: 'computer_20250124', name: 'computer', display_width_px: 800, display_height_px: 600 }], messages: [
  { role: 'assistant', content: [{ type: 'tool_use', id: 'tool_computer', name: 'computer', input: { action: 'screenshot' } }] },
  { role: 'user', content: [image(), document(), { type: 'tool_result', tool_use_id: 'tool_computer', content: [image(), image()] }] },
] });

for (const provider of ['openai', 'anthropic']) {
  const capture = (m, s, p, extra = {}) => provider === 'openai'
    ? oa.captureOpenAIAttachments(m, s, p, { operation: 'responses', direction: 'input', ...extra })
    : an.captureAnthropicAttachments(m, s, p, extra);
  const input = provider === 'openai' ? oaInput : anInput;
  test(`${provider}: typed image/document/screenshots preserve bytes, causal positions and distinct repeated frames`, async () => {
    const f = await fixture();
    try {
      const id = identity(), body = input(), saved = JSON.stringify(body);
      capture(f.manager, id, body); capture(f.manager, { ...id }, body);
      await f.manager.flush();
      assert.equal(f.initialized.length, provider === 'openai' ? 3 : 4);
      assert.equal(f.uploaded.length, f.initialized.length);
      assert.deepEqual(f.uploaded.map(b => b.toString('base64')).sort(), (provider === 'openai' ? [PNG, PDF, PNG] : [PNG, PDF, PNG, PNG]).sort());
      assert.equal(new Set(f.initialized.map(r => r.occurrence_id)).size, f.initialized.length);
      assert.ok(f.initialized.every(r => r.direction === 'input' && r.association.parent.span_id === id.spanId));
      assert.equal(f.initialized.filter(r => r.purpose === 'computer_screenshot').length, provider === 'openai' ? 1 : 2);
      assert.ok(!JSON.stringify(f.initialized).includes('synthetic-private-filename'));
      assert.equal(JSON.stringify(body), saved);
    } finally { await f.close(); }
  });
  for (const key of ['disabled', 'hideInputs', 'hideInputMessages', 'hideInputText', 'piiRedaction', 'hideInputImages', 'serverRedaction', 'serverMetadata']) {
    test(`${provider}: ${key} denies protected sources without reading or uploading`, async () => {
      const f = await fixture(key === 'disabled' ? { enabled: false } : { traceConfig: { [key]: true } },
        key === 'serverRedaction' ? { require_media_redaction: true } : key === 'serverMetadata' ? { require_metadata_removal: true } : {});
      try {
        let read = false;
        const source = provider === 'openai' ? { input: [{ type: 'computer_call_output', call_id: 'call_computer', output: { type: 'computer_screenshot', get image_url() { read = true; throw Error('synthetic-secret'); } } }] }
          : { messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_computer', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', get data() { read = true; throw Error('synthetic-secret'); } } }] }] }] };
        const before = f.calls.length; capture(f.manager, identity(), source);
        await f.manager.flush(); assert.equal(read, false); assert.equal(f.calls.length, before);
        if (key === 'disabled') assert.equal(f.calls.length, 0);
      } finally { await f.close(); }
    });
  }
  for (const capturePolicy of [{ captureContent: false }, { recordInputs: false }, { muted: true }, { temporaryChat: true }]) {
    test(`${provider}: integration restriction ${Object.keys(capturePolicy)[0]} wins`, async () => {
      const f = await fixture(); try { capture(f.manager, identity(), input(), { capture: capturePolicy }); await f.manager.flush(); assert.equal(f.initialized.length, 0); } finally { await f.close(); }
    });
  }
  test(`${provider}: malformed and oversized inline data is bounded; foreign URLs/files are not retrieved`, async () => {
    const f = await fixture();
    try {
      for (const data of ['!', 'a===', 'AB==', 'AAAA'.repeat(100000), 'https://127.0.0.1:1/?synthetic-secret']) {
        const body = provider === 'openai' ? { input: [{ role: 'user', content: [{ type: 'input_image', image_url: data }] }] }
          : { messages: [{ role: 'user', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data } }] }] };
        capture(f.manager, identity(), body);
      }
      capture(f.manager, identity(), provider === 'openai' ? { input: [{ role: 'user', content: [{ type: 'input_file', file_id: 'file_secret' }] }] }
        : { messages: [{ role: 'user', content: [{ type: 'document', source: { type: 'file', file_id: 'file_secret' } }, null, { type: 'tool_result', tool_use_id: {}, content: [image()] }] }] });
      await f.manager.flush(); assert.equal(f.initialized.length, 0);
      await assertNoOpenTickets(f.manager);
    } finally { await f.close(); }
  });
  test(`${provider}: a negotiated upload limit below the inline cap still captures small media exactly`, async () => {
    const f = await fixture({}, {}, { max_upload_bytes: 100 * 1024 });
    try {
      const id = identity(), captured = capture(f.manager, id, input());
      await f.manager.flush();
      assert.equal(captured.size, provider === 'openai' ? 3 : 4);
      assert.equal(f.uploaded.length, captured.size);
      assert.ok(f.initialized.every(r => r.declared_bytes <= 100 * 1024));
      // Above the negotiated limit nothing is decoded or uploaded, and the reservation is returned.
      const big = Buffer.alloc(150 * 1024, 7).toString('base64');
      capture(f.manager, identity(), provider === 'openai' ? { input: [{ role: 'user', content: [{ type: 'input_image', image_url: `data:image/png;base64,${big}` }] }] }
        : { messages: [{ role: 'user', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: big } }] }] });
      await f.manager.flush(); assert.equal(f.uploaded.length, captured.size);
      await assertNoOpenTickets(f.manager);
    } finally { await f.close(); }
  });
  test(`${provider}: a nearly used owned-byte budget still admits small media at its exact size`, async () => {
    const f = await fixture({ maxOwnedBytes: 300 * 1024 });
    try {
      const big = Buffer.alloc(200 * 1024, 7).toString('base64');
      const one = data => provider === 'openai' ? { input: [{ role: 'user', content: [{ type: 'input_image', image_url: `data:image/png;base64,${data}` }] }] }
        : { messages: [{ role: 'user', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data } }] }] };
      // Same tick: the 200 KiB part still owns its bytes, so a cap-sized reservation would not fit.
      const first = capture(f.manager, identity(), one(big)), second = capture(f.manager, identity(), one(PNG));
      assert.equal(first.size, 1); assert.equal(second.size, 1);
      await f.manager.flush();
      assert.deepEqual(f.uploaded.map(b => b.length).sort((a, b) => a - b), [Buffer.from(PNG, 'base64').length, 200 * 1024]);
      await assertNoOpenTickets(f.manager);
    } finally { await f.close(); }
  });
  test(`${provider}: unsampled identity and queued policy tightening cannot upload`, async () => {
    const f = await fixture(); try {
      capture(f.manager, { ...identity(), traceFlags: 0 }, input()); assert.equal(f.manager.status().ownedBytes, 0);
      capture(f.manager, identity(), input()); f.manager.updatePolicy({ traceConfig: { hideInputs: true } }); await f.manager.flush(); assert.equal(f.initialized.length, 0);
    } finally { await f.close(); }
  });
}

test('Inline parts are capped at exactly 256 KiB of decoded bytes for both providers', async () => {
  const f = await fixture(); try {
    const at = Buffer.alloc(256 * 1024, 7).toString('base64'), over = Buffer.alloc(256 * 1024 + 3, 7).toString('base64');
    for (const data of [at, over]) {
      oa.captureOpenAIAttachments(f.manager, identity(), { input: [{ role: 'user', content: [{ type: 'input_image', image_url: `data:image/png;base64,${data}` }] }] }, { operation: 'responses', direction: 'input' });
      oa.captureOpenAIAttachments(f.manager, identity(), { data: [{ b64_json: data }] }, { operation: 'images', direction: 'output' });
      an.captureAnthropicAttachments(f.manager, identity(), { messages: [{ role: 'user', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data } }] }] });
    }
    await f.manager.flush();
    assert.deepEqual(f.uploaded.map(b => b.length), [262144, 262144, 262144]);
  } finally { await f.close(); }
});

test('OpenAI generated images obey output and PII restrictions', async () => {
  for (const traceConfig of [{}, { hideOutputs: true }, { hideOutputMessages: true }, { hideOutputText: true }, { piiRedaction: true }]) {
    const f = await fixture({ traceConfig }); try {
      oa.captureOpenAIAttachments(f.manager, identity(), { data: [{ b64_json: PNG }] }, { operation: 'images', direction: 'output' });
      await f.manager.flush(); assert.equal(f.uploaded.length, Object.keys(traceConfig).length ? 0 : 1);
    } finally { await f.close(); }
  }
});

test('Telemetry projections replace only admitted media with occurrence URIs and never mutate requests', async () => {
  const f = await fixture(); try {
    const cases = [
      [oaInput(), (m, s, x) => oa.captureOpenAIAttachments(m, s, x, { operation: 'responses', direction: 'input' }), (x, c) => oa.projectOpenAIMedia(x, c, 'responses', 'input'), 3],
      [anInput(), (m, s, x) => an.captureAnthropicAttachments(m, s, x), (x, c) => an.projectAnthropicMedia(x, c), 4],
    ];
    for (const [body, capture, project, count] of cases) {
      const before = JSON.stringify(body);
      // Nothing admitted: the exact same payload object is returned, so existing telemetry is unchanged.
      assert.equal(project(body, new Map()), body);
      const captured = capture(f.manager, identity(), body);
      assert.equal(captured.size, count);
      const projected = JSON.stringify(project(body, captured));
      assert.equal(JSON.stringify(body), before);
      for (const secret of [PNG, PDF]) assert.ok(!projected.includes(secret));
      for (const { occurrenceId } of captured.values()) assert.ok(projected.includes(`fi-attachment:${occurrenceId}`));
    }
    await f.manager.flush();
    assert.equal(f.uploaded.length, 7);
  } finally { await f.close(); }
});

test('Denied media keeps the existing telemetry payload untouched', async () => {
  const f = await fixture({ traceConfig: { hideInputImages: true } }); try {
    const body = oaInput(), captured = oa.captureOpenAIAttachments(f.manager, identity(), body, { operation: 'responses', direction: 'input' });
    // Only the PDF is admitted; both images stay exactly as the application sent them.
    assert.deepEqual([...captured.values()].map(v => v.mimeType), ['application/pdf']);
    const projected = oa.projectOpenAIMedia(body, captured, 'responses', 'input');
    assert.equal(projected.input[0].content[0], body.input[0].content[0]);
    assert.equal(projected.input[1], body.input[1]);
    assert.ok(projected.input[0].content[1].file_data.startsWith('fi-attachment:'));
  } finally { await f.close(); }
});

test('Hostile payloads fail capture without throwing into the application', async () => {
  const f = await fixture(); try {
    const hostile = new Proxy({}, { get() { throw Error('synthetic-secret'); }, has() { throw Error('synthetic-secret'); } });
    for (const payload of [hostile, { input: [hostile] }, { messages: [hostile] }, { messages: [{ role: 'user', content: [hostile] }] }, null, 7, 'x']) {
      assert.equal(oa.captureOpenAIAttachments(f.manager, identity(), payload, { operation: 'responses', direction: 'input' }).size, 0);
      assert.equal(oa.captureOpenAIAttachments(f.manager, identity(), payload, { operation: 'chat', direction: 'input' }).size, 0);
      assert.equal(an.captureAnthropicAttachments(f.manager, identity(), payload).size, 0);
      assert.doesNotThrow(() => an.projectAnthropicMedia(payload, new Map([['input/0/0', { occurrenceId: 'x', mimeType: 'image/png' }]])));
      assert.doesNotThrow(() => oa.projectOpenAIMedia(payload, new Map([['responses/input/0/0', { occurrenceId: 'x', mimeType: 'image/png' }]]), 'responses', 'input'));
    }
    await f.manager.flush(); assert.equal(f.initialized.length, 0);
  } finally { await f.close(); }
});

test('Capability descriptors name every unsupported source explicitly', () => {
  for (const name of ['remote_url', 'provider_file', 'partial_image', 'images.generate.stream', 'images.edit', 'speech']) assert.ok(oa.openAIAttachmentCapabilities.unsupported.includes(name));
  // Streamed Responses output is captured from the consumed terminal event; partial images never are.
  assert.ok(oa.openAIAttachmentCapabilities.operations.includes('responses.stream.output'));
  assert.ok(!oa.openAIAttachmentCapabilities.unsupported.includes('streamed_output'));
  for (const name of ['remote_url', 'provider_file', 'native_span_hookup', 'generated_image', 'streamed_output']) assert.ok(an.anthropicAttachmentCapabilities.unsupported.includes(name));
  assert.ok(Object.isFrozen(oa.openAIAttachmentCapabilities) && Object.isFrozen(an.anthropicAttachmentCapabilities));
});

test('CJS/ESM observations share deduplication without creating a second capture', async () => {
  const f = await fixture(); try {
    const esm = await import(pathToFileURL(path.resolve(__dirname, '../dist/esm/attachments.js')));
    const s = identity(), first = oa.captureOpenAIAttachments(f.manager, s, oaInput(), { operation: 'responses', direction: 'input' });
    const second = esm.captureOpenAIAttachments(f.manager, { ...s }, oaInput(), { operation: 'responses', direction: 'input' });
    // The second observer reuses the first occurrence, so both projections point at one upload.
    assert.deepEqual([...second.entries()], [...first.entries()]);
    const anthropicESM = await import(pathToFileURL(path.resolve(__dirname, '../../traceai_anthropic/dist/esm/attachments.js')));
    const a = identity(), cjs = an.captureAnthropicAttachments(f.manager, a, anInput()), mjs = anthropicESM.captureAnthropicAttachments(f.manager, { ...a }, anInput());
    assert.deepEqual([...mjs.entries()], [...cjs.entries()]);
    await f.manager.flush(); assert.equal(f.uploaded.length, 7);
  } finally { await f.close(); }
});

test('Anthropic tool-result images are not labelled computer screenshots without a typed computer tool', async () => {
  const f = await fixture(); try {
    an.captureAnthropicAttachments(f.manager, identity(), { messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_photo', content: [image()] }] }] });
    await f.manager.flush(); assert.equal(f.initialized[0].purpose, 'tool_result'); assert.equal(f.initialized[0].part_index, 0);
  } finally { await f.close(); }
});

test('Anthropic error-tool media respects hideErrorDetails before source access', async () => {
  const f = await fixture({ traceConfig: { hideErrorDetails: true } }); try {
    let read = false;
    an.captureAnthropicAttachments(f.manager, identity(), { messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_error', is_error: true,
      content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', get data() { read = true; return PNG; } } }] }] }] });
    await f.manager.flush(); assert.equal(read, false); assert.equal(f.initialized.length, 0);
  } finally { await f.close(); }
});

test('OpenAI image error bodies are not treated as generated media', async () => {
  const f = await fixture(); try {
    oa.captureOpenAIAttachments(f.manager, identity(), { error: { message: 'synthetic' }, data: [{ b64_json: PNG }] }, { operation: 'images', direction: 'output' });
    await f.manager.flush(); assert.equal(f.initialized.length, 0);
  } finally { await f.close(); }
});

test('Local instrumentor privacy restrictions cannot be loosened by a permissive shared manager', async () => {
  for (const traceConfig of [{ hideInputs: true }, { hideInputMessages: true }, { hideInputText: true }, { piiRedaction: true }]) {
    const f = await fixture(); try {
      assert.equal(oa.captureOpenAIAttachments(f.manager, identity(), oaInput(), { operation: 'responses', direction: 'input', traceConfig }).size, 0);
      assert.equal(an.captureAnthropicAttachments(f.manager, identity(), anInput(), { traceConfig }).size, 0);
      await f.manager.flush(); assert.equal(f.initialized.length, 0, JSON.stringify(traceConfig));
    } finally { await f.close(); }
  }
  for (const traceConfig of [{ hideOutputs: true }, { piiRedaction: true }]) {
    const f = await fixture(); try {
      oa.captureOpenAIAttachments(f.manager, identity(), { data: [{ b64_json: PNG }] }, { operation: 'images', direction: 'output', traceConfig });
      await f.manager.flush(); assert.equal(f.initialized.length, 0, JSON.stringify(traceConfig));
    } finally { await f.close(); }
  }
  // Image-only restriction: the PDF still uploads, no image (including screenshots) does.
  const f = await fixture(); try {
    oa.captureOpenAIAttachments(f.manager, identity(), oaInput(), { operation: 'responses', direction: 'input', traceConfig: { hideInputImages: true } });
    an.captureAnthropicAttachments(f.manager, identity(), anInput(), { traceConfig: { hideInputImages: true } });
    await f.manager.flush(); assert.deepEqual(f.initialized.map(r => r.mime_type), ['application/pdf', 'application/pdf']);
  } finally { await f.close(); }
});

test('Local hideErrorDetails denies Anthropic error tool media even when the manager allows it', async () => {
  const f = await fixture(); try {
    an.captureAnthropicAttachments(f.manager, identity(), { messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_error', is_error: true, content: [image()] }] }] },
      { traceConfig: { hideErrorDetails: true } });
    await f.manager.flush(); assert.equal(f.initialized.length, 0);
  } finally { await f.close(); }
});

async function providerServer() {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const c of req) chunks.push(c);
    const body = JSON.parse(Buffer.concat(chunks)); requests.push({ path: req.url, body });
    if (req.url.endsWith('/responses') && body.stream) {
      // A streamed Responses call whose completed response carries a generated image.
      const response = { id: 'resp_stream', object: 'response', status: 'completed', model: body.model, output: [{ type: 'image_generation_call', id: 'img_stream', status: 'completed', result: PNG }], usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } };
      res.setHeader('content-type', 'text/event-stream');
      res.write(`event: response.created\ndata: ${JSON.stringify({ type: 'response.created', sequence_number: 0, response: { ...response, status: 'in_progress', output: [] } })}\n\n`);
      res.end(`event: response.completed\ndata: ${JSON.stringify({ type: 'response.completed', sequence_number: 1, response })}\n\n`);
      return;
    }
    res.setHeader('content-type', 'application/json');
    if (req.url.endsWith('/chat/completions')) res.end(JSON.stringify({ id: 'chat_fixture', object: 'chat.completion', model: body.model, choices: [{ index: 0, message: { role: 'assistant', content: 'ok' }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
    else if (req.url.endsWith('/responses')) res.end(JSON.stringify({ id: 'resp_fixture', object: 'response', status: 'completed', model: body.model, output: [{ type: 'computer_call', id: 'computer_fixture', call_id: 'call_computer', status: 'completed', action: { type: 'screenshot' } }, { type: 'image_generation_call', id: 'img_fixture', status: 'completed', result: PNG }], usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } }));
    else if (req.url.endsWith('/images/generations')) res.end(JSON.stringify({ created: 1, data: [{ b64_json: PNG }] }));
    else res.end(JSON.stringify({ id: 'msg_fixture', type: 'message', role: 'assistant', model: body.model, content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', stop_sequence: null, usage: { input_tokens: 1, output_tokens: 1 } }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { requests, url: `http://127.0.0.1:${server.address().port}/v1`, async close() { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

test('Real SDKs preserve APIPromise/request/response behavior and upload only through the shared manager', async () => {
  const sdkModule = require(process.env.SDK_OPENAI || 'openai');
  const anthropicModule = require(process.env.SDK_ANTHROPIC || '../../traceai_anthropic/node_modules/@anthropic-ai/sdk');
  const OpenAI = sdkModule.OpenAI || sdkModule.default || sdkModule;
  const Anthropic = anthropicModule.Anthropic || anthropicModule.default || anthropicModule;
  const native = 'openTelemetry' in Anthropic.prototype;
  const f = await fixture(), p = await providerServer();
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const oi = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, attachments: f.manager });
  const ai = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false }, attachments: f.manager });
  oi.setTracerProvider(provider); ai.setTracerProvider(provider);
  oi.manuallyInstrument(sdkModule); ai.manuallyInstrument(anthropicModule);
  try {
    const client = new OpenAI({ apiKey: 'synthetic-provider-key', baseURL: p.url, maxRetries: 0 });
    const body = { model: 'synthetic-model', messages: [{ role: 'user', content: [{ type: 'image_url', image_url: { url: `data:image/png;base64,${PNG}` } }] }] };
    const pending = client.chat.completions.create(body); assert.equal(typeof pending.withResponse, 'function');
    assert.equal((await pending.withResponse()).data.choices[0].message.content, 'ok');
    let expected = 1;
    if (client.responses) {
      const result = await client.responses.create({ model: 'synthetic-model', ...oaInput() });
      assert.equal(result.output[0].call_id, 'call_computer'); expected += 4;
    }
    const generated = await client.images.generate({ prompt: 'synthetic', model: 'synthetic-model', response_format: 'b64_json' });
    assert.equal(generated.data[0].b64_json, PNG); expected++;
    const ac = new Anthropic({ apiKey: 'synthetic-provider-key', baseURL: p.url, maxRetries: 0 });
    const request = ac.messages.create({ model: 'synthetic-model', max_tokens: 10, ...anInput() });
    assert.equal(typeof request.withResponse, 'function'); assert.equal((await request.withResponse()).data.content[0].text, 'ok');
    if (!native) expected += 4;
    await f.manager.flush(); await provider.forceFlush();
    assert.equal(f.uploaded.length, expected);
    assert.ok(p.requests.some(r => JSON.stringify(r.body).includes(PNG)));
    assert.ok(!JSON.stringify(exporter.getFinishedSpans().map(s => s.attributes)).includes(PNG));
    assert.ok(exporter.getFinishedSpans().some(s => s.attributes['fi.attachment.refs']));
    // Each admitted reference appears as an occurrence URI in its own span's telemetry.
    for (const span of exporter.getFinishedSpans()) {
      const refs = JSON.parse(span.attributes['fi.attachment.refs'] ?? '[]'), text = JSON.stringify(span.attributes);
      for (const ref of refs.filter(r => r.direction === 'input')) assert.ok(text.includes(`fi-attachment:${ref.occurrence_id}`), span.name);
    }
    if (client.responses) {
      // Streamed Responses: the stream is delivered unchanged; the generated image in the terminal event the
      // application consumed uploads once and is projected out of that span's output telemetry.
      const stream = await client.responses.create({ model: 'synthetic-model', input: 'synthetic', stream: true });
      const events = []; for await (const event of stream) { events.push(event.type); if (event.type === 'response.completed') assert.equal(event.response.output[0].result, PNG); }
      assert.deepEqual(events, ['response.created', 'response.completed']);
      expected++;
      await f.manager.flush(); await provider.forceFlush(); assert.equal(f.uploaded.length, expected);
      assert.equal(f.uploaded.at(-1).toString('base64'), PNG);
      const streamed = exporter.getFinishedSpans().filter(s => s.name === 'OpenAI Responses').at(-1);
      const refs = JSON.parse(streamed.attributes['fi.attachment.refs'] ?? '[]');
      assert.equal(refs.length, 1); assert.equal(refs[0].direction, 'output'); assert.equal(refs[0].purpose, 'model_output');
      assert.ok(String(streamed.attributes['output.value']).includes(`fi-attachment:${refs[0].occurrence_id}`));
      assert.ok(!JSON.stringify(streamed.attributes).includes(PNG));
      // A consumer that stops early never reaches the terminal event: nothing is captured.
      const early = await client.responses.create({ model: 'synthetic-model', input: 'synthetic', stream: true });
      for await (const event of early) { if (event.type === 'response.created') break; }
      await f.manager.flush(); assert.equal(f.uploaded.length, expected);
    }
    // A raw-response caller owns the body; instrumentation must not parse for media.
    const raw = await client.images.generate({ prompt: 'synthetic', response_format: 'b64_json' }).asResponse();
    assert.equal(raw.bodyUsed, false); await raw.arrayBuffer();
    await f.manager.flush(); assert.equal(f.uploaded.length, expected);
    // Requests still reach the provider unchanged after privacy tightening; media egress stops.
    f.manager.updatePolicy({ traceConfig: { hideInputs: true, piiRedaction: true } });
    await client.chat.completions.create(body);
    await ac.messages.create({ model: 'synthetic-model', max_tokens: 10, ...anInput() });
    await client.images.generate({ prompt: 'synthetic', response_format: 'b64_json' });
    await f.manager.flush(); assert.equal(f.uploaded.length, expected);
    console.log(JSON.stringify({ nativeAnthropic: native, responsesAvailable: !!client.responses, uploads: expected, spans: exporter.getFinishedSpans().length }));
  } finally {
    oi.disable(); ai.disable(); await provider.shutdown(); await f.close(); await p.close();
  }
});

test('A configured but disabled manager leaves existing span telemetry byte-identical', async () => {
  const sdkModule = require(process.env.SDK_OPENAI || 'openai');
  const anthropicModule = require(process.env.SDK_ANTHROPIC || '../../traceai_anthropic/node_modules/@anthropic-ai/sdk');
  const OpenAI = sdkModule.OpenAI || sdkModule.default || sdkModule;
  const Anthropic = anthropicModule.Anthropic || anthropicModule.default || anthropicModule;
  const native = 'openTelemetry' in Anthropic.prototype;
  const p = await providerServer(), disabled = await fixture({ enabled: false });
  const run = async attachments => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const oi = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, attachments });
    const ai = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false }, attachments });
    oi.setTracerProvider(provider); ai.setTracerProvider(provider);
    oi.manuallyInstrument(sdkModule); ai.manuallyInstrument(anthropicModule);
    try {
      const client = new OpenAI({ apiKey: 'synthetic-provider-key', baseURL: p.url, maxRetries: 0 });
      await client.chat.completions.create({ model: 'synthetic-model', messages: [{ role: 'user', content: [{ type: 'image_url', image_url: { url: `data:image/png;base64,${PNG}` } }] }] });
      if (client.responses) await client.responses.create({ model: 'synthetic-model', ...oaInput() });
      if (!native) await new Anthropic({ apiKey: 'synthetic-provider-key', baseURL: p.url, maxRetries: 0 }).messages.create({ model: 'synthetic-model', max_tokens: 10, ...anInput() });
      await provider.forceFlush();
      const strip = ({ name, attributes }) => ({ name, attributes: Object.fromEntries(Object.entries(attributes).filter(([k]) => !/latency|duration|time/i.test(k))) });
      return exporter.getFinishedSpans().map(strip);
    } finally { oi.disable(); ai.disable(); await provider.shutdown(); }
  };
  try {
    const baseline = await run(undefined), off = await run(disabled.manager);
    assert.ok(baseline.length >= 1);
    assert.deepEqual(off, baseline);
    assert.equal(disabled.calls.length, 0);
  } finally { await disabled.close(); await p.close(); }
});

test('OpenAI data URIs of a sibling image type are admitted under their exact MIME', async () => {
  const f = await fixture(); try {
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 0xff, 0xd9]).toString('base64');
    const captured = oa.captureOpenAIAttachments(f.manager, identity(), { messages: [{ role: 'user', content: [
      { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${jpeg}` } },
      { type: 'image_url', image_url: { url: `data:image/gif;base64,${jpeg}` } },
    ] }] }, { operation: 'chat', direction: 'input' });
    await f.manager.flush();
    assert.deepEqual([...captured.values()].map(v => v.mimeType), ['image/jpeg']);
    assert.deepEqual(f.initialized.map(r => r.mime_type), ['image/jpeg']);
    assert.equal(f.uploaded[0].toString('base64'), jpeg);
    await assertNoOpenTickets(f.manager);
  } finally { await f.close(); }
});

test('No core degradation warning was raised by any fixture', () => {
  assert.deepEqual(coreWarnings, []);
});
