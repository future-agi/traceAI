/* SDK design section 1: every provider part is preflighted before its source is read, sized or decoded. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { ProviderMediaCapture } = require('../dist/src');
const { fixture, png, wav, identity } = require('./fixtures.cjs');

const warnings = [];
process.on('warning', warning => { if (/^FI_ATTACHMENTS_/.test(String(warning.code))) warnings.push(warning.code); });
const b64 = png.toString('base64');
const settled = async f => { assert.deepEqual(await f.manager.flush(), { unfinished: 0, dropped: 0 }); };
/** Records preflight calls and source reads in one ordered log. */
function spy(manager, log) {
  const preflight = manager.preflight.bind(manager);
  manager.preflight = (target, options) => { const ticket = preflight(target, options); log.push(`preflight:${options.mimeType}:${options.maxBytes}:${ticket.reason}`); return ticket; };
  return manager;
}

test('P-01 the gate preflight runs before the first source read, and an admitted part is read once', async () => {
  const f = await fixture(); const log = [];
  try {
    spy(f.manager, log);
    const capture = new ProviderMediaCapture(identity, { attachments: f.manager });
    capture.geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', get data() { log.push('read'); return b64; } } }] }]);
    await settled(f);
    assert.deepEqual(log, [`preflight:image/png:${1024 * 1024}:queued`, 'read']);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
  } finally { await f.close(); }
});

test('P-02 Bedrock bytes and data URIs are preflighted before their getters run', async () => {
  const f = await fixture(); const log = [];
  try {
    spy(f.manager, log);
    const capture = new ProviderMediaCapture(identity, { attachments: f.manager });
    capture.bedrockConverse({ modelId: 'amazon.nova-lite-v1:0', messages: [{ content: [{ image: { format: 'png', source: { get bytes() { log.push('read'); return png; } } } }] }] });
    capture.compatibleMessages([{ content: [{ type: 'image_url', get image_url() { log.push('read'); return { url: `data:image/png;base64,${b64}` }; } }] }]);
    await settled(f);
    assert.deepEqual(log.map(e => e.split(':')[0]), ['preflight', 'read', 'preflight', 'read']);
    assert.equal(f.uploads.length, 2);
  } finally { await f.close(); }
});

test('P-03 a manager without preflight (an older core) captures nothing and never reads', async () => {
  let attaches = 0; let reads = 0;
  const legacy = { correlation: () => ({ project() {}, snapshot: () => identity }), attach() { attaches++; return { admission: 'queued' }; }, status: () => ({ state: 'ready' }) };
  const capture = new ProviderMediaCapture(identity, { attachments: legacy });
  capture.geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', get data() { reads++; return b64; } } }] }]);
  capture.speechInput(new Blob([wav()], { type: 'audio/wav' }));
  assert.equal(attaches, 0); assert.equal(reads, 0);
});

test('P-04 a server upload cap below the adapter gate still admits a small part at its exact size', async () => {
  const f = await fixture({ sessionLimits: { max_upload_bytes: 64 * 1024 } }); const log = [];
  try {
    spy(f.manager, log);
    new ProviderMediaCapture(identity, { attachments: f.manager }).geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', data: b64 } }] }]);
    await settled(f);
    assert.deepEqual(log, [`preflight:image/png:${1024 * 1024}:too_large`, `preflight:image/png:${png.length}:queued`]);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
  } finally { await f.close(); }
});

test('P-05 an owned budget below the adapter gate still admits a small part at its exact size', async () => {
  const f = await fixture({ maxOwnedBytes: 256 * 1024 });
  try {
    new ProviderMediaCapture(identity, { attachments: f.manager }).compatibleMessages([{ content: [{ type: 'image_url', image_url: { url: `data:image/png;base64,${b64}` } }] }]);
    await settled(f);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
  } finally { await f.close(); }
});

test('P-06 Bedrock bytes above the 1 MiB base64 gate are re-preflighted at their exact length and upload', async () => {
  const f = await fixture();
  try {
    const big = Buffer.alloc(1536 * 1024, 7); png.copy(big);
    new ProviderMediaCapture(identity, { attachments: f.manager }).bedrockConverse({ modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0', messages: [{ content: [{ image: { format: 'png', source: { bytes: big } } }] }] });
    await settled(f);
    assert.equal(f.uploads.length, 1); assert.equal(f.uploads[0].data.length, big.length);
  } finally { await f.close(); }
});

test('P-07 an admitted ticket whose bytes fail strict decode is released, holding no budget or descriptor', async () => {
  const f = await fixture();
  try {
    new ProviderMediaCapture(identity, { attachments: f.manager }).geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', data: '!!!!'.repeat(8) } }] }]);
    await settled(f);
    const status = f.manager.status();
    assert.equal(f.uploads.length, 0); assert.equal(status.openTickets, 0); assert.equal(status.ownedBytes, 0);
  } finally { await f.close(); }
});

test('P-08 speech bodies are preflighted when the application reads them, never when the hook is installed', async () => {
  const f = await fixture(); const log = [];
  try {
    spy(f.manager, log);
    const c = new ProviderMediaCapture(identity, { attachments: f.manager });
    const response = c.speechResponse(new Response(wav(), { headers: { 'content-type': 'audio/wav' } }));
    assert.deepEqual(log, []);
    await response.blob(); c.speechInput(new Blob([wav()], { type: 'audio/wav' }));
    await settled(f);
    assert.ok(log.length >= 2); assert.equal(f.uploads.length, 2);
    for (const upload of f.uploads) assert.deepEqual(upload.data, wav());
  } finally { await f.close(); }
});

test('P-09 the loopback fixture speaks the V1 wire: no legacy-transport or wire-rejection warning fired', async () => {
  const f = await fixture();
  try {
    new ProviderMediaCapture(identity, { attachments: f.manager }).geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', data: b64 } }] }]);
    await settled(f);
    assert.equal(f.uploads.length, 1);
  } finally { await f.close(); }
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(warnings, []);
});
