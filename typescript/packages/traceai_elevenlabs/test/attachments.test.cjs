const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { fixture, bytes } = require('./fixture.cjs');
const { attachmentFixture, wav } = require('./attachments-fixture.cjs');
const { SpeechAttachments } = require('../dist/src/attachments.js');
const { generateTraceConfig } = require('@traceai/fi-core');
const tone = readFileSync(require('node:path').join(__dirname, 'fixtures/tone.mp3'));

for (const sdkName of ['@elevenlabs/sdk-floor', '@elevenlabs/elevenlabs-js']) {
  const SDK = require(sdkName);
  test(`${sdkName}: timestamp response uploads once; cached awaits retain data and parent`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }, { base64: tone.toString('base64') }); t.after(() => f.close());
    const response = f.client.textToSpeech.convertWithTimestamps('synthetic-voice', { text: 'synthetic', outputFormat: 'mp3_44100_128' });
    const first = await response, second = await response.withRawResponse();
    assert.equal(first, second.data);
    assert.deepEqual(Buffer.from(first.audioBase64, 'base64'), tone);
    assert.equal(first.alignment.characterStartTimesSeconds[0], 0);
    await a.manager.flush();
    assert.equal(a.uploaded.length, 1); assert.deepEqual(a.uploaded[0], tone);
    const span = f.spans()[0];
    const refs = JSON.parse(span.attributes['fi.attachment.refs']);
    assert.equal(refs.length, 1); assert.equal(refs[0].occurrence_id, a.requests[0].occurrence_id);
    assert.equal(a.requests[0].association.parent.span_id, span.spanContext().spanId);
    assert.equal(a.requests[0].association.parent.trace_id.replaceAll('-', ''), span.spanContext().traceId);
    assert.equal(a.requests[0].mime_type, 'audio/mpeg');
    const wire = JSON.stringify(await f.flush());
    assert.ok(!wire.includes(tone.toString('base64')));
    assert.ok(!wire.includes('synthetic-write-capability'));
  });

  test(`${sdkName}: immutable typed STT Blob uploads independently without filename`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager, attachmentInputMimeType: 'audio/wav' }); t.after(() => f.close());
    const audio = wav(); const blob = new File([audio], 'synthetic-private-name.wav', { type: 'audio/wav' });
    const result = await f.client.speechToText.convert({ file: blob, modelId: 'scribe_v1' });
    assert.equal(result.text, 'synthetic-transcript-elevenlabs');
    await a.manager.flush(); assert.equal(a.uploaded.length, 1); assert.deepEqual(a.uploaded[0], audio);
    assert.deepEqual(Buffer.from(await blob.arrayBuffer()), audio);
    assert.equal(a.requests[0].direction, 'input'); assert.equal(a.requests[0].mime_type, 'audio/wav');
    assert.ok(!JSON.stringify(a.requests).includes('synthetic-private-name'));
    assert.ok(!JSON.stringify(await f.flush()).includes('synthetic-private-name'));
  });

  for (const [name, managerConfig, localConfig, policy] of [
    ['disabled', { enabled: false }, {}],
    ['core hidden input/output', { traceConfig: { hideInputs: true, hideOutputs: true } }, {}],
    ['local hidden messages', {}, { hideInputMessages: true, hideOutputMessages: true }],
    ['local hidden text', {}, { hideInputText: true, hideOutputText: true }],
    ['local PII', {}, { piiRedaction: true }],
    ['core PII', { traceConfig: { piiRedaction: true } }, {}],
    ['server redaction', {}, {}, { require_media_redaction: true }],
    ['environment denial', { environment: () => ({ FI_ATTACHMENTS_ENABLED: 'false' }) }, {}],
  ]) test(`${sdkName}: ${name} prevents all attachment upload traffic`, async t => {
    const a = await attachmentFixture(managerConfig, policy); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager, attachmentInputMimeType: 'audio/wav', traceConfig: localConfig },
      { base64: tone.toString('base64') }); t.after(() => f.close());
    await f.client.textToSpeech.convertWithTimestamps('voice', { text: 'synthetic' });
    await f.client.speechToText.convert({ file: new Blob([wav()], { type: 'audio/wav' }), modelId: 'scribe_v1' });
    await bytes(await f.client.textToSpeech.convert('voice', { text: 'synthetic' }));
    await bytes(await f.client.textToSpeech.stream('voice', { text: 'synthetic' }));
    for await (const chunk of await f.client.textToSpeech.streamWithTimestamps('voice', { text: 'synthetic' })) assert.ok(chunk);
    await a.manager.flush();
    assert.equal(a.uploaded.length, 0); assert.equal(a.requests.length, 0);
    assert.ok(a.calls.every(c => c.path.endsWith('/attachment-sessions')));
    assert.ok(f.spans().every(s => s.attributes['fi.attachment.refs'] === undefined));
  });

  for (const method of ['convert', 'stream']) {
    test(`${sdkName}: fully consumed ${method} uploads the exact MP3 once, before the span ends`, async t => {
      const a = await attachmentFixture(); t.after(() => a.close());
      const f = await fixture(SDK, { attachments: a.manager }, { audio: tone }); t.after(() => f.close());
      const received = await bytes(await f.client.textToSpeech[method]('voice', { text: 'synthetic' }));
      assert.deepEqual(Buffer.from(received), tone);
      await a.manager.flush();
      assert.equal(a.uploaded.length, 1); assert.deepEqual(a.uploaded[0], tone);
      const span = f.spans()[0];
      assert.equal(span.attributes['elevenlabs.completion'], 'complete');
      assert.equal(span.attributes['elevenlabs.attachment.output'], 'queued');
      const refs = JSON.parse(span.attributes['fi.attachment.refs']);
      assert.equal(refs.length, 1); assert.equal(refs[0].occurrence_id, a.requests[0].occurrence_id);
      assert.equal(a.requests[0].mime_type, 'audio/mpeg'); assert.equal(a.requests[0].direction, 'output');
      assert.equal(a.requests[0].declared_bytes, tone.length);
      assert.equal(a.requests[0].association.parent.span_id, span.spanContext().spanId);
    });
  }

  test(`${sdkName}: BYOB reads keep consumer buffers and upload identical bytes`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }, { audio: tone }); t.after(() => f.close());
    const stream = await f.client.textToSpeech.stream('voice', { text: 'synthetic' });
    let reader;
    try { reader = stream.getReader({ mode: 'byob' }); } catch { t.skip('upstream stream is not a byte stream'); return; }
    const out = []; let buffer = new ArrayBuffer(1024);
    for (;;) {
      const { done, value } = await reader.read(new Uint8Array(buffer, 0, 1024));
      if (done) break;
      out.push(Buffer.from(value)); buffer = value.buffer;
    }
    assert.deepEqual(Buffer.concat(out), tone);
    await a.manager.flush(); assert.equal(a.uploaded.length, 1); assert.deepEqual(a.uploaded[0], tone);
  });

  test(`${sdkName}: cancelled or broken streams upload nothing and keep cancellation`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }, { audio: tone }); t.after(() => f.close());
    const stream = await f.client.textToSpeech.stream('voice', { text: 'synthetic' });
    const reader = stream.getReader();
    assert.equal((await reader.read()).done, false);
    await reader.cancel('synthetic-cancel');
    const broken = await f.client.textToSpeech.convert('voice', { text: 'synthetic' }, { headers: { 'x-case': 'broken' } });
    await assert.rejects(bytes(broken));
    for await (const chunk of await f.client.textToSpeech.streamWithTimestamps('voice', { text: 'synthetic' })) {
      assert.equal(typeof chunk.audioBase64, 'string'); break;
    }
    await a.manager.flush(); assert.equal(a.uploaded.length, 0); assert.equal(a.requests.length, 0);
    const spans = f.spans();
    assert.ok(spans.some(s => s.attributes['elevenlabs.completion'] === 'cancelled'));
    assert.ok(spans.every(s => s.attributes['fi.attachment.refs'] === undefined));
    assert.equal(a.manager.status().openStreams, 0);
  });

  test(`${sdkName}: fully iterated timestamp stream uploads the decoded MP3 chunks as one object`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }, { base64: tone.toString('base64') }); t.after(() => f.close());
    let chunks = 0;
    for await (const chunk of await f.client.textToSpeech.streamWithTimestamps('voice', { text: 'synthetic' })) {
      assert.equal(chunk.audioBase64, tone.toString('base64')); chunks++;
    }
    assert.equal(chunks, 2);
    await a.manager.flush();
    assert.equal(a.uploaded.length, 1); assert.deepEqual(a.uploaded[0], Buffer.concat([tone, tone]));
    const span = f.spans()[0];
    assert.equal(JSON.parse(span.attributes['fi.attachment.refs']).length, 1);
    assert.ok(!JSON.stringify(await f.flush()).includes(tone.toString('base64')));
  });

  test(`${sdkName}: raw PCM streams are never labelled MP3`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }, { audio: tone }); t.after(() => f.close());
    await bytes(await f.client.textToSpeech.stream('voice', { text: 'synthetic', outputFormat: 'pcm_24000' }));
    await a.manager.flush(); assert.equal(a.uploaded.length, 0);
    assert.equal(f.spans()[0].attributes['elevenlabs.attachment.output'], 'source_unavailable');
  });

  test(`${sdkName}: the bound adapter marks its call context as the media owner`, async t => {
    const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
    const { context } = require('@opentelemetry/api');
    const manager = new AsyncLocalStorageContextManager().enable();
    context.setGlobalContextManager(manager);
    t.after(() => { context.disable(); });
    const key = Symbol.for('traceai.auto.attachment_owner.v1');
    const seen = [];
    const original = globalThis.fetch;
    globalThis.fetch = (...args) => { seen.push(context.active().getValue(key)); return original(...args); };
    t.after(() => { globalThis.fetch = original; });
    const a = await attachmentFixture(); t.after(() => a.close());
    const bound = await fixture(SDK, { attachments: a.manager }, { audio: tone }); t.after(() => bound.close());
    const plain = await fixture(SDK, {}, { audio: tone }); t.after(() => plain.close());
    await bytes(await bound.client.textToSpeech.convert('voice', { text: 'synthetic' }));
    await bytes(await plain.client.textToSpeech.convert('voice', { text: 'synthetic' }));
    assert.deepEqual(seen, [true, undefined]);
  });

  for (const [name, base64, format] of [
    ['oversized complete response', Buffer.alloc(256 * 1024 + 1).toString('base64'), 'mp3_44100_128'],
    ['malformed base64', 'a!==', 'mp3_44100_128'],
    ['raw PCM is not MP3', tone.toString('base64'), 'pcm_24000'],
    ['unrecognized format cannot become the default', tone.toString('base64'), 'bad/format'],
  ]) test(`${sdkName}: ${name} fails closed without changing SDK response`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }, { base64 }); t.after(() => f.close());
    const value = await f.client.textToSpeech.convertWithTimestamps('voice', { text: 'synthetic', outputFormat: 'mp3_44100_128' },
      { queryParams: { output_format: format } });
    assert.equal(value.audioBase64, base64); await a.manager.flush(); assert.equal(a.uploaded.length, 0);
  });
}

for (const config of [{ enabled: false }, { traceConfig: { piiRedaction: true } }, { traceConfig: { hideInputs: true, hideOutputs: true } }]) {
  test(`denied sources are never inspected: ${JSON.stringify(config)}`, async t => {
    const a = await attachmentFixture(config); t.after(() => a.close());
    let reads = 0;
    const span = { spanContext: () => ({ traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 }),
      isRecording: () => true, setAttribute() {} };
    const capture = new SpeechAttachments(a.manager, span, generateTraceConfig(), undefined);
    capture.timestamped({ get audioBase64() { reads++; throw Error('synthetic-private'); } });
    capture.input({ get file() { reads++; throw Error('synthetic-private'); } }, 'audio/wav');
    await a.manager.flush(); assert.equal(reads, 0); assert.equal(a.uploaded.length, 0);
  });
}

test('a stream above the cap uploads nothing (too_large) and returns its reservation', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const attrs = {};
  const span = { spanContext: () => ({ traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 }),
    isRecording: () => true, setAttribute(k, v) { attrs[k] = v; } };
  const capture = new SpeechAttachments(a.manager, span, generateTraceConfig(), undefined);
  const { ELEVENLABS_STREAM_MAX_BYTES } = require('../dist/src/attachments.js');
  capture.chunk(new Uint8Array(ELEVENLABS_STREAM_MAX_BYTES));
  capture.chunk(new Uint8Array(1));
  capture.close('complete');
  await a.manager.flush();
  assert.equal(a.uploaded.length, 0); assert.equal(attrs['elevenlabs.attachment.output'], 'too_large');
  assert.equal(a.manager.status().ownedBytes, 0);
});
