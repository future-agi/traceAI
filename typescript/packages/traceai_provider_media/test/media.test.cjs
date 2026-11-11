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
      const before = f.requests.length; await f.manager.flush();
      assert.equal(reads, 0); assert.equal(f.uploads.length, 0); assert.equal(f.requests.length, before);
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
