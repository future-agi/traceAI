// Run after building fi-core, or set FI_CORE_FIXTURE to a packed-package install.
// Native shapes: https://github.com/open-telemetry/semantic-conventions-genai/blob/
// 2e994c6d59a93bb4fc1752c5378eedb9b8e14d6b/model/gen-ai/gen-ai-input-messages.json
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const { createServer } = require('node:http');
const { once } = require('node:events');
const { test } = require('node:test');
const load = createRequire(resolve(process.env.FI_CORE_FIXTURE || __dirname + '/..', 'package.json'));
const { FITracer } = process.env.FI_CORE_FIXTURE ? load('@traceai/fi-core') : load('./dist/src/index.js');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, SamplingDecision } = load('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = load('@opentelemetry/exporter-trace-otlp-http');
const { ROOT_CONTEXT, trace } = load('@opentelemetry/api');
const INPUT = 'gen_ai.input.messages';
const OUTPUT = 'gen_ai.output.messages';
const REDACTED = '__REDACTED__';
const SECRET = 'synthetic-private-payload';
const native = parts => JSON.stringify([{ role: 'user', parts }]);
const text = native([{ type: 'text', content: SECRET }]);

async function record(config, key, value, route = 'creation') {
  const exporter = new InMemorySpanExporter();
  const sampled = [];
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
    sampler: {
      shouldSample(_ctx, _id, _name, _kind, attributes) {
        sampled.push(attributes);
        return { decision: SamplingDecision.RECORD_AND_SAMPLED };
      },
      toString: () => 'privacy-test',
    },
  });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer('genai-test'), traceConfig: config });
    const attributes = { [key]: value, 'gen_ai.request.model': 'synthetic-model' };
    const linked = { traceId: '1'.repeat(32), spanId: '2'.repeat(16), traceFlags: 1 };
    if (route === 'active') {
      tracer.startActiveSpan('synthetic', { attributes }, span => span.end());
    } else {
      const span = tracer.startSpan('synthetic', route === 'creation' ? { attributes } :
        route === 'creation-link' ? { links: [{ context: linked, attributes }] } : {});
      if (route === 'setAttribute') span.setAttribute(key, value);
      if (route === 'setAttributes') span.setAttributes(attributes);
      if (route === 'link') span.addLink({ context: linked, attributes });
      if (route === 'links') span.addLinks([{ context: linked, attributes }]);
      span.end();
    }
    await provider.forceFlush();
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 1);
    return { attributes: spans[0].attributes, links: spans[0].links, sampled };
  } finally {
    await provider.shutdown();
  }
}

for (const [key, flag] of [[INPUT, 'hideInputText'], [OUTPUT, 'hideOutputText']]) {
  for (const route of ['creation', 'active', 'setAttribute', 'setAttributes', 'creation-link', 'link', 'links']) {
    test(`${flag}: native parts masked via ${route}, including sampler`, async () => {
      const result = await record({ [flag]: true }, key, text, route);
      assert.ok(!JSON.stringify(result).includes(SECRET));
      const attributes = route.includes('link') ? result.links[0].attributes : result.attributes;
      assert.deepEqual(JSON.parse(attributes[key]), [{ role: 'user', parts: [{ type: 'text', content: REDACTED }] }]);
    });
  }
  for (const part of [
    { type: 'reasoning', content: SECRET },
    { type: 'compaction', content: SECRET, encrypted_content: SECRET },
    { type: 'tool_call', id: 'call-1', name: 'lookup', arguments: { query: SECRET } },
    { type: 'tool_call_response', id: 'call-1', response: { nested: [SECRET] } },
    { type: 'server_tool_call', name: 'search', server_tool_call: { type: 'search', query: SECRET } },
    { type: 'server_tool_call_response', server_tool_call_response: { type: 'search', results: [SECRET] } },
    { type: 'future_part', nested: { secret: SECRET } },
    { type: 'blob', modality: 'image', content: SECRET },
    { type: 'uri', modality: 'image', uri: SECRET },
    { type: 'file', modality: 'image', file_id: SECRET },
  ]) {
    test(`${flag}: redacts ${part.type} payloads and preserves metadata`, async () => {
      const { attributes } = await record({ [flag]: true }, key, native([part]));
      assert.ok(!attributes[key].includes(SECRET));
      const redacted = JSON.parse(attributes[key])[0].parts[0];
      assert.equal(redacted.type, part.type);
      if (part.id) assert.equal(redacted.id, part.id);
      if (part.name) assert.equal(redacted.name, part.name);
    });
  }
  test(`${flag}: legacy content and tool payloads, extensions, dual shapes`, async () => {
    const message = { role: 'assistant', content: SECRET, parts: [{ type: 'text', content: SECRET }],
      tool_calls: [{ id: 'call-1', function: { name: 'lookup', arguments: SECRET } }],
      function_call: { arguments: SECRET }, extension: { data: SECRET } };
    const { attributes } = await record({ [flag]: true }, key, JSON.stringify([message]));
    assert.ok(!attributes[key].includes(SECRET));
    assert.equal(JSON.parse(attributes[key])[0].content, REDACTED);
  });
}

for (const part of [
  { type: 'image', content: SECRET },
  { type: 'image_url', image_url: { url: SECRET } },
  { type: 'input_image', image_url: SECRET },
  { type: 'blob', modality: 'image', mime_type: 'image/png', content: SECRET },
  { type: 'uri', modality: 'image', uri: SECRET },
  { type: 'file', modality: 'image', file_id: SECRET },
  { type: 'blob', mime_type: 'image/jpeg', content: SECRET },
  { type: 'uri', modality: 'audio', mime_type: 'image/png', uri: SECRET },
]) {
  for (const field of ['parts', 'content']) {
    test(`hideInputImages: removes ${part.type}/${part.modality || part.mime_type || 'legacy'} in ${field}`, async () => {
      const value = JSON.stringify([{ role: 'user', [field]: [{ type: 'text', content: 'keep text' }, part] }]);
      const { attributes } = await record({ hideInputImages: true }, INPUT, value);
      assert.ok(!attributes[INPUT].includes(SECRET));
      assert.deepEqual(JSON.parse(attributes[INPUT])[0][field], [{ type: 'text', content: 'keep text' }]);
    });
  }
}

test('hideInputImages: preserves declared non-image media and legacy text', async () => {
  const parts = [{ type: 'blob', modality: 'audio', mime_type: 'audio/wav', content: 'audio' },
    { type: 'uri', modality: 'video', uri: 'https://example.invalid/video' },
    { type: 'text', content: 'keep text' }];
  const value = JSON.stringify([{ role: 'user', parts, content: 'keep legacy' }]);
  const { attributes } = await record({ hideInputImages: true }, INPUT, value);
  assert.deepEqual(JSON.parse(attributes[INPUT]), JSON.parse(value));
});

test('hideInputImages: opaque tool payloads and unknown parts fail closed', async () => {
  const value = native([{ type: 'custom', payload: SECRET }, { type: 'blob', content: SECRET },
    { type: ['text'], content: SECRET },
    { type: 'blob', modality: ['audio'], content: SECRET },
    { type: 'blob', mime_type: 'application/octet-stream', content: SECRET },
    { type: 'tool_call_response', response: { image_url: SECRET } }]);
  const { attributes } = await record({ hideInputImages: true }, INPUT, value);
  assert.ok(!attributes[INPUT].includes(SECRET));
});

for (const config of [{ hideInputText: true }, { hideInputImages: true }, { hideInputText: true, hideInputImages: true }]) {
  for (const value of ['[broken-private-payload', '{"content":"synthetic-private-payload"}', 'null',
    '[null]', '["synthetic-private-payload"]', 42, ['synthetic-private-payload'],
    native([null, SECRET]), JSON.stringify([{ role: 'user', parts: SECRET }])]) {
    test(`fails closed for malformed messages ${JSON.stringify(config)} ${JSON.stringify(value)}`, async () => {
      const { attributes } = await record(config, INPUT, value);
      assert.ok(!JSON.stringify(attributes).includes(SECRET));
      assert.notEqual(attributes[INPUT], value);
    });
  }
}

test('image and text flags compose instead of the first rule winning', async () => {
  for (const field of ['parts', 'content']) {
    const value = JSON.stringify([{ role: 'user', [field]: [{ type: 'text', content: SECRET },
      { type: 'image_url', image_url: { url: SECRET } }] }]);
    const { attributes } = await record({ hideInputText: true, hideInputImages: true }, INPUT, value);
    assert.ok(!attributes[INPUT].includes(SECRET));
  }
});

test('image hiding still applies PII redaction to retained text', async () => {
  const { attributes } = await record({ hideInputImages: true, piiRedaction: true }, INPUT,
    native([{ type: 'text', content: 'Contact john@example.com' }]));
  assert.ok(!attributes[INPUT].includes('john@example.com'));
  assert.ok(attributes[INPUT].includes('<EMAIL_ADDRESS>'));
});

test('disabled and opposite-direction flags preserve bytes; broad flags remove messages', async () => {
  for (const config of [{}, { hideOutputText: true }]) {
    assert.equal((await record(config, INPUT, text)).attributes[INPUT], text);
  }
  for (const flag of ['hideInputs', 'hideInputMessages']) {
    assert.equal((await record({ [flag]: true }, INPUT, text)).attributes[INPUT], undefined);
  }
  for (const flag of ['hideOutputs', 'hideOutputMessages']) {
    assert.equal((await record({ [flag]: true }, OUTPUT, text)).attributes[OUTPUT], undefined);
  }
});

test('empty messages and repeated masking are stable', async () => {
  const config = { hideInputText: true, hideInputImages: true };
  assert.equal((await record(config, INPUT, '[]')).attributes[INPUT], '[]');
  const onceMasked = (await record(config, INPUT, text)).attributes[INPUT];
  assert.equal((await record(config, INPUT, onceMasked)).attributes[INPUT], onceMasked);
});

test('structured metadata and a large mixed message batch cannot carry payloads through', async () => {
  const value = JSON.stringify(Array.from({ length: 1000 }, () => ({ role: 'user',
    name: { data: SECRET }, parts: [{ type: 'text', content: SECRET, id: { data: SECRET } },
      { type: 'tool_call_response', response: [SECRET] }] })));
  const { attributes } = await record({ hideInputText: true }, INPUT, value);
  assert.ok(!attributes[INPUT].includes(SECRET));
  assert.equal(JSON.parse(attributes[INPUT]).length, 1000);
});

test('environment privacy flags use the same native message masking', async () => {
  const previous = process.env.FI_HIDE_INPUT_TEXT;
  process.env.FI_HIDE_INPUT_TEXT = 'true';
  try {
    assert.ok(!(await record({}, INPUT, text)).attributes[INPUT].includes(SECRET));
  } finally {
    if (previous === undefined) delete process.env.FI_HIDE_INPUT_TEXT;
    else process.env.FI_HIDE_INPUT_TEXT = previous;
  }
});

test('real OTLP HTTP wire: wrapped payloads private, raw external spans remain outside boundary', async () => {
  const bodies = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('{}');
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const exporter = new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` });
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const raw = provider.getTracer('wire-test');
    const tracer = new FITracer({ tracer: raw, traceConfig: { hideInputText: true, hideOutputText: true, hideInputImages: true } });
    const parent = tracer.startSpan('parent', { attributes: { [INPUT]: text, 'gen_ai.request.model': 'synthetic-model' } });
    const child = tracer.startSpan('child', {}, trace.setSpan(ROOT_CONTEXT, parent));
    child.setAttributes({ [OUTPUT]: native([{ type: 'tool_call', name: 'lookup', arguments: { query: SECRET } }]) });
    child.end();
    parent.end();
    raw.startSpan('external-control', { attributes: { [INPUT]: text } }).end();
    await provider.forceFlush();
    const spans = bodies.flatMap(body => body.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    assert.equal(spans.length, 3, 'no duplicate spans');
    const wrapped = spans.filter(span => span.name !== 'external-control');
    assert.equal(wrapped.length, 2);
    assert.ok(!JSON.stringify(wrapped).includes(SECRET));
    const wireParent = spans.find(span => span.name === 'parent');
    const wireChild = spans.find(span => span.name === 'child');
    assert.equal(wireChild.traceId, wireParent.traceId);
    assert.equal(wireChild.parentSpanId, wireParent.spanId);
    assert.ok(JSON.stringify(wireParent).includes('synthetic-model'));
    assert.ok(JSON.stringify(spans.find(span => span.name === 'external-control')).includes(SECRET));
  } finally {
    await provider.shutdown();
    await new Promise(resolve => server.close(resolve));
  }
});
