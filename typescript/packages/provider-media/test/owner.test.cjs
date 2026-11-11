/* Shared media-owner marker: exactly one adapter uploads a call's media (auto wire layer, ElevenLabs, this helper). */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createRequire } = require('node:module');
const { ProviderMediaCapture, ATTACHMENT_OWNER_KEY, attachmentOwnedElsewhere } = require('../dist/src');
const { fixture, png, wav, identity } = require('./fixtures.cjs');
const api = createRequire(path.resolve(__dirname, '../../fi-core/package.json'))('@opentelemetry/api');

const NAME = 'traceai.auto.attachment_owner.v1';
const owned = () => api.ROOT_CONTEXT.setValue(Symbol.for(NAME), true);
/** Counts every admission call, so an owned call is shown to never reach the manager. */
function counting(manager) {
  const calls = { preflight: 0, stream: 0 };
  const preflight = manager.preflight.bind(manager), stream = manager.startAttachmentStream.bind(manager);
  manager.preflight = (...a) => { calls.preflight++; return preflight(...a); };
  manager.startAttachmentStream = (...a) => { calls.stream++; return stream(...a); };
  return calls;
}
const idle = f => { const s = f.manager.status(); assert.equal(s.openStreams, 0); assert.equal(s.openTickets, 0); assert.equal(s.ownedBytes, 0); };
const speech = () => new Response(wav(), { headers: { 'content-type': 'audio/wav' } });
const drain = async reader => { const out = []; for (;;) { const { done, value } = await reader.read(); if (done) return Buffer.concat(out); out.push(value); } };

test('O-01 the marker is the OpenTelemetry context key the auto module and ElevenLabs use', () => {
  assert.equal(ATTACHMENT_OWNER_KEY, Symbol.for(NAME));
  assert.equal(ATTACHMENT_OWNER_KEY, api.createContextKey(NAME));
  assert.equal(attachmentOwnedElsewhere(owned()), true);
  for (const value of [undefined, false, 0, '']) assert.equal(attachmentOwnedElsewhere(api.ROOT_CONTEXT.setValue(ATTACHMENT_OWNER_KEY, value)), false);
  assert.equal(attachmentOwnedElsewhere(undefined), false);
  assert.equal(attachmentOwnedElsewhere({ getValue() { throw new Error('synthetic'); } }), false);
});

test('O-02 claim marks a new context only while this capture is active; the parent context is never mutated', async () => {
  const f = await fixture();
  try {
    const parent = api.ROOT_CONTEXT;
    const claimed = new ProviderMediaCapture(identity, { attachments: f.manager, parentContext: parent }).claim(parent);
    assert.notEqual(claimed, parent); assert.equal(claimed.getValue(ATTACHMENT_OWNER_KEY), true);
    assert.equal(parent.getValue(ATTACHMENT_OWNER_KEY), undefined);
    // Without a manager (unqualified route, attachments not configured) the call is not claimed.
    assert.equal(new ProviderMediaCapture(identity, {}).claim(parent), parent);
    // Already owned by another adapter: unchanged, and this capture stays inert.
    const other = owned();
    assert.equal(new ProviderMediaCapture(identity, { attachments: f.manager, parentContext: other }).claim(other), other);
    // A context whose setValue throws is returned unchanged; the application call is never affected.
    const broken = { getValue: () => undefined, setValue() { throw new Error('synthetic'); } };
    assert.equal(new ProviderMediaCapture(identity, { attachments: f.manager }).claim(broken), broken);
  } finally { await f.close(); }
});

test('O-03 a call owned by another adapter never reaches the manager and uploads nothing; the application keeps every byte', async () => {
  const f = await fixture();
  try {
    const calls = counting(f.manager);
    const capture = new ProviderMediaCapture(identity, { attachments: f.manager, parentContext: owned() });
    capture.speechInput(new File([wav()], 'synthetic.wav', { type: 'audio/wav' }));
    capture.geminiContents([{ parts: [{ inlineData: { mimeType: 'image/png', data: png.toString('base64') } }] }]);
    capture.bedrockConverse({ modelId: 'amazon.nova-lite-v1:0', messages: [{ content: [{ image: { format: 'png', source: { bytes: png } } }] }] });
    capture.compatibleMessages([{ content: [{ type: 'image_url', image_url: { url: `data:image/png;base64,${png.toString('base64')}` } }] }]);
    const whole = capture.speechResponse(speech());
    assert.deepEqual(Buffer.from(await whole.arrayBuffer()), wav());
    const streamed = capture.speechResponse(speech());
    assert.deepEqual(await drain(streamed.body.getReader()), wav());
    await f.manager.flush(); idle(f);
    assert.deepEqual(calls, { preflight: 0, stream: 0 }); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

for (const value of [undefined, false]) test(`O-04 an unowned parent context (marker ${value}) captures exactly once`, async () => {
  const f = await fixture();
  try {
    const parentContext = api.ROOT_CONTEXT.setValue(ATTACHMENT_OWNER_KEY, value);
    const capture = new ProviderMediaCapture(identity, { attachments: f.manager, parentContext });
    capture.speechInput(new File([wav()], 'synthetic.wav', { type: 'audio/wav' }));
    const response = capture.speechResponse(speech());
    assert.deepEqual(await drain(response.body.getReader()), wav());
    await f.manager.flush(); idle(f);
    assert.deepEqual(f.uploads.map(u => u.request.direction).sort(), ['input', 'output']);
    for (const u of f.uploads) assert.deepEqual(u.data, wav());
  } finally { await f.close(); }
});

test('O-05 a locally hidden capture still claims the call, so no other layer uploads what the integration hides', async () => {
  const f = await fixture();
  try {
    const capture = new ProviderMediaCapture(identity, { attachments: f.manager, traceConfig: { hideOutputs: true, hideInputs: true }, parentContext: api.ROOT_CONTEXT });
    assert.equal(capture.claim(api.ROOT_CONTEXT).getValue(ATTACHMENT_OWNER_KEY), true);
    capture.speechInput(new File([wav()], 'synthetic.wav', { type: 'audio/wav' }));
    assert.deepEqual(Buffer.from(await capture.speechResponse(speech()).arrayBuffer()), wav());
    await f.manager.flush(); idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});
