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
    await a.manager.flush();
    assert.equal(a.uploaded.length, 0); assert.equal(a.requests.length, 0);
    assert.ok(a.calls.every(c => c.path.endsWith('/attachment-sessions')));
    assert.ok(f.spans().every(s => s.attributes['fi.attachment.refs'] === undefined));
  });

  test(`${sdkName}: streams retain cancellation and never masquerade as complete audio`, async t => {
    const a = await attachmentFixture(); t.after(() => a.close());
    const f = await fixture(SDK, { attachments: a.manager }); t.after(() => f.close());
    const stream = await f.client.textToSpeech.stream('voice', { text: 'synthetic' });
    await stream.cancel('synthetic-cancel');
    await bytes(await f.client.textToSpeech.convert('voice', { text: 'synthetic' }));
    for await (const chunk of await f.client.textToSpeech.streamWithTimestamps('voice', { text: 'synthetic' })) {
      assert.equal(typeof chunk.audioBase64, 'string'); break;
    }
    await a.manager.flush(); assert.equal(a.uploaded.length, 0);
    assert.ok(f.spans().some(s => s.attributes['elevenlabs.completion'] === 'cancelled'));
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
