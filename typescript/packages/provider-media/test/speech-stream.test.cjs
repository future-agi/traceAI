/* SDK design 6.4: a reader or async iterator the application obtained is observed chunk by chunk; nothing is pulled by the adapter. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { ProviderMediaCapture } = require('../dist/src');
const { fixture, wav, identity } = require('./fixtures.cjs');

const audio = (bytes = wav(), chunk = 16) => {
  let offset = 0; let pulls = 0;
  const body = new ReadableStream({ pull(controller) {
    pulls++;
    if (offset >= bytes.length) { controller.close(); return; }
    controller.enqueue(new Uint8Array(bytes.subarray(offset, offset + chunk))); offset += chunk;
  } }, { highWaterMark: 0 });
  return { response: new Response(body, { headers: { 'content-type': 'audio/wav' } }), pulls: () => pulls };
};
const drain = async reader => { const out = []; for (;;) { const { done, value } = await reader.read(); if (done) return Buffer.concat(out); out.push(value); } };
const idle = async f => { const s = f.manager.status(); assert.equal(s.openStreams, 0); assert.equal(s.openTickets, 0); assert.equal(s.ownedBytes, 0); };

test('S-01 a reader read to its end uploads exactly the chunks the application received', async () => {
  const f = await fixture();
  try {
    const { response, pulls } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    assert.equal(pulls(), 0);
    const received = await drain(response.body.getReader());
    assert.deepEqual(received, wav());
    await f.manager.flush(); await idle(f);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, wav());
    assert.equal(f.uploads[0].request.direction, 'output'); assert.equal(f.uploads[0].request.mime_type, 'audio/wav');
  } finally { await f.close(); }
});

for (const stop of ['releaseLock', 'cancel', 'abandon']) test(`S-02 a partial read then ${stop} uploads nothing and returns its budget`, async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const reader = response.body.getReader();
    await reader.read(); await reader.read();
    if (stop === 'releaseLock') reader.releaseLock();
    if (stop === 'cancel') await reader.cancel();
    await f.manager.flush();
    assert.equal(f.uploads.length, 0);
    if (stop !== 'abandon') await idle(f);
    else assert.equal(f.manager.status().openStreams, 1);
  } finally { await f.close(); }
});

test('S-03 a stream over the 4 MiB cap uploads nothing; the application still reads every byte', async () => {
  const f = await fixture();
  try {
    const big = Buffer.alloc(4 * 1024 * 1024 + 64, 3);
    const { response } = audio(big, 256 * 1024);
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const received = await drain(response.body.getReader());
    assert.equal(received.length, big.length);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

for (const mode of ['hideOutputs', 'piiRedaction', 'localHide', 'disabled']) test(`S-04 ${mode}: no stream is opened and nothing uploads`, async () => {
  const f = await fixture(mode === 'disabled' ? { enabled: false } : mode === 'localHide' ? {} : { traceConfig: { [mode]: true } });
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager, traceConfig: mode === 'localHide' ? { hideOutputs: true } : {} }).speechResponse(response);
    const reader = response.body.getReader();
    await reader.read();
    assert.equal(f.manager.status().openStreams, 0);
    assert.ok((await drain(reader)).length > 0);
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-05 a policy tightened mid-stream discards the stream', async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const reader = response.body.getReader();
    await reader.read();
    f.manager.updatePolicy({ traceConfig: { hideOutputs: true } });
    await drain(reader);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-06 a read error reaches the application unchanged and uploads nothing', async () => {
  const f = await fixture();
  try {
    let sent = false;
    const body = new ReadableStream({ pull(controller) { if (!sent) { sent = true; controller.enqueue(new Uint8Array(wav().subarray(0, 8))); } else controller.error(new Error('synthetic-network')); } }, { highWaterMark: 0 });
    const response = new Response(body, { headers: { 'content-type': 'audio/wav' } });
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const reader = response.body.getReader();
    await reader.read();
    await assert.rejects(reader.read(), /synthetic-network/);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-07 the same span is captured once when the application reads through arrayBuffer()', async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), wav());
    await f.manager.flush(); await idle(f);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, wav());
  } finally { await f.close(); }
});

test('S-08 a second reader after releaseLock sees only the rest of the body, so nothing uploads', async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const first = response.body.getReader();
    const head = [(await first.read()).value, (await first.read()).value];
    first.releaseLock();
    const rest = await drain(response.body.getReader());
    assert.deepEqual(Buffer.concat([...head, rest]), wav());
    await f.manager.flush(); await idle(f);
    assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

/* The core reserves min(4 MiB, the negotiated max_upload_bytes) for a consumed stream (ts-core 01b3327a). */
const SERVER_CAP = 32 * 1024;
const pcm = size => { const out = Buffer.alloc(size); wav().copy(out); out.writeUInt32LE(size - 8, 4); out.writeUInt32LE(size - 44, 40); return out; };

test('S-09 a server max_upload_bytes below 4 MiB still admits the reader stream, reserving only the server cap', async () => {
  const f = await fixture({ sessionLimits: { max_upload_bytes: SERVER_CAP } });
  try {
    const body = pcm(20 * 1024);
    const { response } = audio(body, 4096);
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.equal(f.manager.status().openStreams, 1);
    assert.equal(f.manager.status().ownedBytes, SERVER_CAP, 'reservation = min(4 MiB, max_upload_bytes)');
    const rest = await drain(reader);
    assert.deepEqual(Buffer.concat([first.value, rest]), body);
    await f.manager.flush(); await idle(f);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, body);
  } finally { await f.close(); }
});

test('S-10 a body over the server cap (but under 4 MiB) is discarded whole; the application still reads every byte', async () => {
  const f = await fixture({ sessionLimits: { max_upload_bytes: SERVER_CAP } });
  try {
    const body = pcm(SERVER_CAP + 4096);
    const { response } = audio(body, 4096);
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    assert.deepEqual(await drain(response.body.getReader()), body);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

/* `for await` and values() lock the body with an internal reader, never the public getReader(). */
const iterate = async (iterable, stopAfter = Infinity) => {
  const out = []; let n = 0;
  for await (const chunk of iterable) { out.push(chunk); if (++n >= stopAfter) break; }
  return Buffer.concat(out);
};

for (const entry of ['for await', 'values()']) test(`S-11 ${entry} to the end uploads exactly the chunks the application received, pulling nothing itself`, async () => {
  const f = await fixture();
  try {
    const { response, pulls } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const iterator = entry === 'values()' ? response.body.values() : response.body[Symbol.asyncIterator]();
    assert.equal(pulls(), 0, 'obtaining the iterator reads nothing');
    assert.equal(f.manager.status().openStreams, 1);
    const received = await iterate({ [Symbol.asyncIterator]: () => iterator });
    assert.deepEqual(received, wav());
    await f.manager.flush(); await idle(f);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, wav());
    assert.equal(f.uploads[0].request.direction, 'output'); assert.equal(f.uploads[0].request.mime_type, 'audio/wav');
  } finally { await f.close(); }
});

test('S-12 a for-await loop that breaks early uploads nothing and returns its budget', async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    assert.equal((await iterate(response.body, 2)).length, 32);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-13 values({ preventCancel }) then a later reader sees only the rest of the body, so nothing uploads', async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const head = await iterate({ [Symbol.asyncIterator]: () => response.body.values({ preventCancel: true }) }, 2);
    const rest = await drain(response.body.getReader());
    assert.deepEqual(Buffer.concat([head, rest]), wav());
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-14 an iterated stream over the 4 MiB cap uploads nothing; the application still reads every byte', async () => {
  const f = await fixture();
  try {
    const big = Buffer.alloc(4 * 1024 * 1024 + 64, 3);
    const { response } = audio(big, 256 * 1024);
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    assert.equal((await iterate(response.body)).length, big.length);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-15 an error during iteration reaches the application unchanged and uploads nothing', async () => {
  const f = await fixture();
  try {
    let sent = false;
    const body = new ReadableStream({ pull(controller) { if (!sent) { sent = true; controller.enqueue(new Uint8Array(wav().subarray(0, 8))); } else controller.error(new Error('synthetic-network')); } }, { highWaterMark: 0 });
    const response = new Response(body, { headers: { 'content-type': 'audio/wav' } });
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    await assert.rejects(iterate(response.body), /synthetic-network/);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

for (const mode of ['hideOutputs', 'piiRedaction', 'localHide', 'disabled']) test(`S-16 ${mode}: iterating opens no stream and nothing uploads`, async () => {
  const f = await fixture(mode === 'disabled' ? { enabled: false } : mode === 'localHide' ? {} : { traceConfig: { [mode]: true } });
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager, traceConfig: mode === 'localHide' ? { hideOutputs: true } : {} }).speechResponse(response);
    const iterator = response.body[Symbol.asyncIterator]();
    await iterator.next();
    assert.equal(f.manager.status().openStreams, 0);
    assert.ok((await iterate({ [Symbol.asyncIterator]: () => iterator })).length > 0);
    await f.manager.flush(); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});

test('S-17 iterating a body already locked by a reader throws the host error unchanged; the reader still uploads once', async () => {
  const f = await fixture();
  try {
    const { response } = audio();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    const reader = response.body.getReader();
    assert.throws(() => response.body[Symbol.asyncIterator](), TypeError);
    assert.throws(() => response.body.values(), TypeError);
    assert.deepEqual(await drain(reader), wav());
    await f.manager.flush(); await idle(f);
    assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, wav());
  } finally { await f.close(); }
});

/* openai 4.x's default Node transport is node-fetch 2, whose body is a Node PassThrough. */
const nodeStreamResponse = (bytes = wav(), chunk = 16) => {
  const { PassThrough } = require('node:stream');
  const body = new PassThrough();
  for (let offset = 0; offset < bytes.length; offset += chunk) body.write(bytes.subarray(offset, offset + chunk));
  body.end();
  return { headers: new Headers({ 'content-type': 'audio/wav' }), bodyUsed: false, body };
};

for (const stop of ['end', 'break']) test(`S-18 iterating a Node-stream body (node-fetch shim) to its ${stop} installs no hook and uploads nothing`, async () => {
  const f = await fixture();
  try {
    const response = nodeStreamResponse();
    new ProviderMediaCapture(identity, { attachments: f.manager }).speechResponse(response);
    assert.equal(Object.getOwnPropertyDescriptor(response.body, Symbol.asyncIterator), undefined);
    assert.equal(Object.getOwnPropertyDescriptor(response.body, 'values'), undefined);
    const received = await iterate(response.body, stop === 'break' ? 2 : Infinity);
    if (stop === 'end') assert.deepEqual(received, wav());
    assert.equal(f.manager.status().openStreams, 0);
    await f.manager.flush(); await idle(f); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});
