const { test } = require('node:test');
const assert = require('node:assert/strict');
const { wrapQuery } = require('../dist/src');
const { harness, PNG, PDF } = require('../../traceai_mcp/test/attachment-harness.cjs');
const image = () => ({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG.toString('base64') } });
function messages(content, isError = false) {
  return [
    { type: 'assistant', message: { id: 'msg_1', content: [{ type: 'tool_use', id: 'tool_1', name: 'screenshot', input: {} }] } },
    { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'tool_1', content, is_error: isError }] } },
    { type: 'result', subtype: 'success', result: 'done' },
  ];
}
async function run(h, frames, config = {}) {
  const params = { prompt: 'synthetic', options: {} };
  const original = function (actual) { assert.equal(actual, params); return (async function* () { yield* frames; })(); };
  const received = [];
  for await (const frame of wrapQuery(original, { tracerProvider: h.provider, ...config })(params)) received.push(frame);
  assert.equal(received.length, frames.length);
  received.forEach((frame, i) => assert.equal(frame, frames[i]));
  await h.manager.flush();
}

test('original nested image/PDF attach to tool before truncation; results remain identical', async () => {
  const h = await harness();
  try {
    const frames = messages([{ type: 'text', text: 'x'.repeat(6000) }, image(), { type: 'document', title: 'secret-filename', source: { type: 'base64', media_type: 'application/pdf', data: PDF.toString('base64') } }]);
    await run(h, frames);
    assert.equal(h.uploads.length, 2);
    const tool = h.exporter.getFinishedSpans().find(s => s.name === 'tool.screenshot');
    assert.ok(tool); assert.equal(JSON.parse(tool.attributes['fi.attachment.refs']).length, 2);
    for (const record of h.records) { assert.equal(record.association.parent.span_id, tool.spanContext().spanId); assert.equal(record.tool_call_id, 'tool_1'); }
    const attributes = JSON.stringify(h.exporter.getFinishedSpans().map(s => s.attributes));
    assert.ok(!attributes.includes(PNG.toString('base64'))); assert.ok(!attributes.includes(PDF.toString('base64'))); assert.ok(!attributes.includes('secret-filename'));
  } finally { await h.close(); }
});

for (const flag of ['hideInputs', 'hideOutputs', 'hideInputMessages', 'hideOutputMessages', 'hideInputText', 'hideOutputText', 'hideInputImages', 'piiRedaction']) {
  test(`agent privacy ${flag}: original bytes never read/uploaded`, async () => {
    const h = await harness({ manager: { traceConfig: { [flag]: true } } });
    try {
      let reads = 0;
      const part = { type: 'image', source: { type: 'base64', media_type: 'image/png', get data() { reads++; throw Error('secret'); } } };
      await run(h, messages([part]));
      assert.equal(reads, 0); assert.equal(h.records.length, 0); assert.equal(h.uploads.length, 0);
      for (const span of h.exporter.getFinishedSpans()) assert.equal(span.attributes['fi.attachment.refs'], undefined);
    } finally { await h.close(); }
  });
}

test('default disabled, integration hide flags, capture restrictions and error policy fail closed', async () => {
  for (const setting of ['disabled', 'integration', 'muted', 'error']) {
    const h = await harness({ manager: setting === 'disabled' ? { enabled: false } : setting === 'error' ? { traceConfig: { hideErrorDetails: true } } : {} });
    try {
      const config = setting === 'integration' ? { traceConfig: { hideInputImages: true } } : setting === 'muted' ? { attachmentCapture: { muted: true } } : {};
      await run(h, messages([image()], setting === 'error'), config);
      assert.equal(h.records.length, 0); assert.equal(h.uploads.length, 0);
    } finally { await h.close(); }
  }
});

test('input images use conversation identity; paths, URLs, file IDs and opaque streams are not read', async () => {
  const h = await harness();
  try {
    const frames = [{ type: 'user', message: { content: [image(), { type: 'image', source: { type: 'url', get url() { throw Error('source read'); } } }, { type: 'document', source: { type: 'file', get file_id() { throw Error('file read'); } } }] } }];
    await run(h, frames);
    assert.equal(h.uploads.length, 1);
    const conversation = h.exporter.getFinishedSpans().find(s => s.name === 'claude_agent.conversation');
    assert.equal(h.records[0].association.parent.span_id, conversation.spanContext().spanId);
    assert.equal(h.records[0].direction, 'input');
  } finally { await h.close(); }
});

test('replayed tool_result cannot upload a second occurrence after the tool ended', async () => {
  const h = await harness();
  try {
    const frames = messages([image()]); frames.splice(2, 0, frames[1]);
    await run(h, frames); assert.equal(h.uploads.length, 1);
  } finally { await h.close(); }
});

test('iterator cancellation and control method receivers are unchanged', async () => {
  const h = await harness();
  try {
    const frames = messages([image()]); let closed = 0;
    const original = (async function* () { yield* frames; })();
    original.close = function () { assert.equal(this, original); closed++; };
    const traced = wrapQuery(() => original, { tracerProvider: h.provider })({ prompt: 'synthetic' });
    assert.equal((await traced.next()).value, frames[0]);
    traced.close();
    assert.equal((await traced.next()).value, frames[1]);
    await traced.return(); await h.manager.flush();
    assert.equal(closed, 1); assert.equal(h.uploads.length, 0);
  } finally { await h.close(); }
});

test('the same original block re-yielded in user frames is one occurrence per query', async () => {
  const h = await harness();
  try {
    const block = image();
    const frame = { type: 'user', message: { content: [block] } };
    await run(h, [frame, frame, { type: 'user', message: { content: [block] } }]);
    assert.equal(h.uploads.length, 1); assert.equal(h.records.length, 1);
  } finally { await h.close(); }
});
