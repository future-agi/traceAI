const { test } = require('node:test');
const assert = require('node:assert/strict');
const { ProviderMediaCapture, compatibleMediaRoute } = require('../dist/src');
const { fixture, png, wav, identity } = require('./fixtures.cjs');
const inline = () => ({ inlineData: { mimeType: 'image/png', data: png.toString('base64') } });
const messages = () => [{ role: 'user', content: [{ type: 'image_url', image_url: { url: `data:image/png;base64,${png.toString('base64')}` } }] }];

for (const kind of ['gemini', 'compatible', 'bedrock']) test(`${kind}: immutable bytes and exact message positions reach loopback attachment API`, async () => {
  const f = await fixture();
  try {
    const capture = new ProviderMediaCapture(identity, { attachments: f.manager });
    if (kind === 'gemini') capture.geminiContents([{ role: 'user', parts: [inline()] }]);
    if (kind === 'compatible') capture.compatibleMessages(messages());
    if (kind === 'bedrock') capture.bedrockConverse({ modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0', messages: [{ role: 'user', content: [{ image: { format: 'png', source: { bytes: png } } }] }] });
    assert.deepEqual(await f.manager.flush(), { unfinished: 0, dropped: 0 });
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
    assert.equal(f.uploads[0].request.message_index, 0); assert.equal(f.uploads[0].request.part_index, 0);
    assert.equal(f.uploads[0].request.association.parent.trace_id, '01234567-89ab-cdef-0123-456789abcdef');
  } finally { await f.close(); }
});

for (const mode of ['disabled', 'hideInputs', 'hideInputMessages', 'hideInputImages', 'hideInputText', 'piiRedaction', 'localHide', 'localPii', 'unsampled']) {
  test(`${mode}: no decoding, source access, upload or occurrence`, async () => {
    const f = await fixture(mode === 'disabled' ? { enabled: false } : { traceConfig: { [mode]: true } });
    let reads = 0;
    try {
      const capture = new ProviderMediaCapture(mode === 'unsampled' ? { ...identity, traceFlags: 0 } : identity, {
        attachments: f.manager, traceConfig: mode === 'localHide' ? { hideInputs: true } : mode === 'localPii' ? { piiRedaction: true } : {},
      });
      capture.geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', get data() { reads++; throw Error('synthetic-secret'); } } }] }]);
      capture.compatibleMessages([{ content: [{ type: 'image_url', get image_url() { reads++; throw Error('synthetic-secret'); } }] }]);
      capture.bedrockConverse({ modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0', messages: [{ content: [
        { image: { format: 'png', source: { get bytes() { reads++; throw Error('synthetic-secret'); } } } },
        { toolResult: { toolUseId: 'tool-1', content: [{ image: { format: 'png', source: { get bytes() { reads++; throw Error('synthetic-secret'); } } } }] } }] }] });
      capture.speechInput(new Blob([Buffer.from('synthetic-secret')], { type: 'audio/wav' }));
      const before = f.requests.length; await f.manager.flush();
      // Hidden images do not hide audio: only the audio Blob may be admitted in that mode.
      assert.equal(reads, 0); assert.equal(f.uploads.length, mode === 'hideInputImages' ? 1 : 0);
      if (mode === 'hideInputImages') assert.equal(f.uploads[0].request.modality, 'audio'); else assert.equal(f.requests.length, before);
    } finally { await f.close(); }
  });
}

test('distinct repeated image positions survive; duplicate observation of the same span does not', async () => {
  const f = await fixture();
  try {
    const input = [{ parts: [inline(), inline()] }];
    new ProviderMediaCapture(identity, { attachments: f.manager }).geminiContents(input);
    new ProviderMediaCapture(identity, { attachments: f.manager }).geminiContents(input);
    await f.manager.flush(); assert.equal(f.uploads.length, 2); assert.notEqual(f.uploads[0].request.occurrence_id, f.uploads[1].request.occurrence_id);
  } finally { await f.close(); }
});

test('unknown models, URLs, provider files and async operation values remain uncaptured', async () => {
  const f = await fixture();
  try {
    const c = new ProviderMediaCapture(identity, { attachments: f.manager });
    c.geminiContents([{ parts: [{ fileData: { mimeType: 'image/png', fileUri: 'https://private.invalid/secret' } }] }]);
    c.geminiResponse({ name: 'operations/submitted', done: false, response: { generatedVideos: [{ video: { uri: 'https://private.invalid' } }] } });
    c.compatibleMessages([{ content: [{ type: 'image_url', image_url: { url: 'http://169.254.169.254/metadata' } }, { type: 'file', file: { file_id: 'private' } }] }]);
    c.bedrockConverse({ modelId: 'unknown.text-only', messages: [{ content: [{ image: { format: 'png', source: { bytes: png } } }] }] });
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
    for (const baseURL of ['https://unknown.invalid', 'https://api.deepseek.com', 'https://api.cerebras.ai', 'http://api.openai.com', 'https://api.openai.com.evil.invalid']) assert.equal(compatibleMediaRoute({ _client: { baseURL } }), false);
  } finally { await f.close(); }
});

test('generated Gemini Part uses output restrictions and stays distinct from input', async () => {
  for (const traceConfig of [{}, { hideOutputs: true }, { hideOutputText: true }, { piiRedaction: true }]) {
    const f = await fixture({ traceConfig });
    try {
      new ProviderMediaCapture(identity, { attachments: f.manager }).geminiResponse({ candidates: [{ content: { parts: [inline()] } }] });
      await f.manager.flush(); assert.equal(f.uploads.length, Object.keys(traceConfig).length ? 0 : 1);
      if (f.uploads.length) assert.equal(f.uploads[0].request.direction, 'output');
    } finally { await f.close(); }
  }
});

for (const method of ['arrayBuffer', 'blob', 'unread', 'cancel', 'stream']) test(`speech ${method}: only application whole-body reads capture`, async () => {
  const f = await fixture();
  try {
    const response = new Response(wav(), { headers: { 'content-type': 'audio/wav' } });
    const c = new ProviderMediaCapture(identity, { attachments: f.manager });
    assert.equal(c.speechResponse(response), response); assert.equal(response.bodyUsed, false);
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
    if (method === 'arrayBuffer') assert.deepEqual(Buffer.from(await response.arrayBuffer()), wav());
    if (method === 'blob') assert.deepEqual(Buffer.from(await (await response.blob()).arrayBuffer()), wav());
    if (method === 'cancel') await response.body.cancel();
    if (method === 'stream') { const reader = response.body.getReader(); while (!(await reader.read()).done) {} }
    await f.manager.flush(); assert.equal(f.uploads.length, ['arrayBuffer', 'blob'].includes(method) ? 1 : 0);
    if (f.uploads.length) assert.deepEqual(f.uploads[0].data, wav());
  } finally { await f.close(); }
});

test('File/Blob input does not read opaque streams, names or paths', async () => {
  const f = await fixture();
  try {
    const c = new ProviderMediaCapture(identity, { attachments: f.manager });
    c.speechInput({ get path() { throw Error('must not read'); }, read() { throw Error('must not read'); } });
    c.speechInput(new Blob([wav()], { type: 'audio/wav' }));
    await f.manager.flush(); assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, wav());
    assert.equal(JSON.stringify(f.uploads[0].request).includes('filename'), false);
  } finally { await f.close(); }
});

test('speech late read rechecks tightened policy and never mutates ended spans', async () => {
  const f = await fixture(); let ended = false; let writes = 0;
  const span = { spanContext: () => identity, isRecording: () => !ended, setAttribute() { if (ended) throw Error('ended mutation'); writes++; } };
  try {
    const c = new ProviderMediaCapture(span, { attachments: f.manager });
    const response = c.speechResponse(new Response(wav(), { headers: { 'content-type': 'audio/wav' } }));
    ended = true; const previous = writes;
    f.manager.updatePolicy({ traceConfig: { hideOutputs: true } });
    await response.arrayBuffer(); await f.manager.flush();
    assert.equal(f.uploads.length, 0); assert.equal(writes, previous);
  } finally { await f.close(); }
});

test('malformed and oversized base64 fail closed without changing provider input', async () => {
  const f = await fixture();
  try {
    for (const data of ['not-base64', 'A'.repeat(2 * 1024 * 1024)]) {
      const part = Object.freeze({ inlineData: Object.freeze({ mimeType: 'image/png', data }) });
      new ProviderMediaCapture({ ...identity }, { attachments: f.manager }).geminiContents([{ parts: [part] }]);
    }
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('data URI MIME descriptors consume one part budget and capture JPEG/WebP once with the exact type', async () => {
  const f = await fixture();
  try {
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, 0xff, 0xd9]);
    const parts = Array.from({ length: 70 }, () => ({ type: 'image_url', image_url: { url: `data:image/jpeg;base64,${jpeg.toString('base64')}` } }));
    new ProviderMediaCapture(identity, { attachments: f.manager }).compatibleMessages([{ content: parts }]);
    await f.manager.flush();
    assert.equal(f.uploads.length, 64);
    assert.equal(new Set(f.uploads.map(u => u.request.mime_type)).size, 1); assert.equal(f.uploads[0].request.mime_type, 'image/jpeg');
    assert.deepEqual(f.uploads[0].data, jpeg);
    // The manager runs two upload workers, so completion order is not part order: check the admitted set.
    assert.deepEqual(f.uploads.map(u => u.request.part_index).sort((a, b) => a - b), Array.from({ length: 64 }, (_, i) => i));
  } finally { await f.close(); }
});

test('non-image data URIs, PDFs in image_url and unknown audio formats are not captured', async () => {
  const f = await fixture();
  try {
    new ProviderMediaCapture(identity, { attachments: f.manager }).compatibleMessages([{ content: [
      { type: 'image_url', image_url: { url: `data:application/pdf;base64,${png.toString('base64')}` } },
      { type: 'image_url', image_url: { url: `data:image/svg+xml;base64,${png.toString('base64')}` } },
      { type: 'input_audio', input_audio: { format: 'flac', data: wav().toString('base64') } },
    ] }]);
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('Bedrock typed tool-result screenshots keep tool identity; S3 locations and unsupported formats stay uncaptured', async () => {
  const f = await fixture();
  try {
    new ProviderMediaCapture(identity, { attachments: f.manager }).bedrockConverse({ modelId: 'us.anthropic.claude-sonnet-4-20250514-v1:0', messages: [
      { role: 'user', content: [{ toolResult: { toolUseId: 'tooluse_screen-1', content: [{ text: 'ok' }, { image: { format: 'png', source: { bytes: png } } }] } }] },
      { role: 'user', content: [{ image: { format: 'png', source: { s3Location: { uri: 's3://private/secret.png' } } } }, { image: { format: 'gif', source: { bytes: png } } },
        { document: { format: 'docx', name: 'private-name', source: { bytes: png } } }, { document: { format: 'pdf', name: 'private-name', source: { bytes: png } } }] },
    ] });
    await f.manager.flush();
    assert.equal(f.uploads.length, 2);
    const tool = f.uploads.find(u => u.request.purpose === 'tool_result');
    assert.equal(tool.request.tool_call_id, 'tooluse_screen-1'); assert.equal(tool.request.message_index, 0); assert.deepEqual(tool.data, png);
    const pdf = f.uploads.find(u => u.request.mime_type === 'application/pdf');
    assert.equal(pdf.request.message_index, 1); assert.equal(pdf.request.part_index, 3);
    assert.equal(JSON.stringify(f.uploads.map(u => u.request)).includes('private'), false);
  } finally { await f.close(); }
});

test('speech routes, manager activity and capability descriptors are explicit', async () => {
  const { speechMediaRoute, attachmentsActive, PROVIDER_MEDIA_CAPABILITIES } = require('../dist/src');
  for (const [baseURL, allowed] of [['https://api.openai.com/v1', true], ['https://api.groq.com/openai/v1', true], ['https://api.fireworks.ai/inference/v1', false], ['http://api.openai.com/v1', false], ['https://api.openai.com.evil.invalid/v1', false]]) assert.equal(speechMediaRoute({ _client: { baseURL } }), allowed, baseURL);
  assert.equal(attachmentsActive(undefined), false);
  const off = await fixture({ enabled: false }); const on = await fixture();
  try { assert.equal(attachmentsActive(off.manager), false); assert.equal(attachmentsActive(on.manager), true); } finally { await off.close(); await on.close(); }
  assert.ok(Object.isFrozen(PROVIDER_MEDIA_CAPABILITIES));
  for (const operations of Object.values(PROVIDER_MEDIA_CAPABILITIES)) for (const entry of operations) {
    assert.equal(entry.sources.some(s => /url|file|stream|provider/i.test(s)), false);
  }
  assert.equal(PROVIDER_MEDIA_CAPABILITIES['@traceai/openai'].some(e => e.operation.startsWith('chat')), false);
});

test('input_audio is captured only for OpenAI-shaped routes that advertise it', async () => {
  const f = await fixture();
  try {
    const audio = [{ content: [{ type: 'input_audio', input_audio: { format: 'wav', data: wav().toString('base64') } }] }];
    new ProviderMediaCapture({ ...identity, spanId: '0000000000000001' }, { attachments: f.manager }).compatibleMessages(audio, 'groq');
    new ProviderMediaCapture({ ...identity, spanId: '0000000000000002' }, { attachments: f.manager }).compatibleMessages(audio, 'mistral');
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
    new ProviderMediaCapture(identity, { attachments: f.manager }).compatibleMessages(audio);
    await f.manager.flush(); assert.equal(f.uploads.length, 1); assert.equal(f.uploads[0].request.mime_type, 'audio/wav'); assert.deepEqual(f.uploads[0].data, wav());
  } finally { await f.close(); }
});
