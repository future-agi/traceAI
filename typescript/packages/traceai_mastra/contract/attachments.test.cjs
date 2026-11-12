// Mastra attachment disposition (F06): the Future AGI Mastra exporter receives
// ended, serialized Mastra spans. It has no attachment manager and never uploads.
// A real Agent sends an image through the OpenAI SDK to loopback HTTP; the decoded
// OTLP output must carry no attachment session traffic and no unissued references.
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { test, before, after, afterEach } = require('node:test');
const packagePath = process.env.MASTRA_TEST_PACKAGE || path.resolve(__dirname, '../dist/src/index.js');
const req = createRequire(process.env.MASTRA_TEST_DEPS
  ? path.join(process.env.MASTRA_TEST_DEPS, 'package.json') : packagePath);
const { Mastra } = req('@mastra/core');
const { Agent } = req('@mastra/core/agent');
const { createOpenAI } = req('@ai-sdk/openai');
const transformer = createRequire(req.resolve('@opentelemetry/exporter-trace-otlp-proto'));
const wire = transformer('@opentelemetry/otlp-transformer/build/src/generated/root.js')
  .opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const attrs = list => Object.fromEntries((list || []).map(a => [a.key, Object.values(a.value)[0]]));
// A synthetic payload larger than common serializer string limits, so the test
// records whether the exported span still holds the original bytes.
// A valid 1x1 PNG followed by padding: runtimes that sniff the signature accept it.
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5nQAAAAASUVORK5CYII=', 'base64');
const IMAGE = Buffer.concat([PNG, Buffer.alloc(6000, 7)]).toString('base64');
const URI = `data:image/png;base64,${IMAGE}`;
let createFIObservability, obs, collectorPaths, providerBodies, spans, ended;
const collector = http.createServer((request, response) => {
  const chunks = [];
  collectorPaths.push(request.url);
  request.on('data', chunk => chunks.push(chunk));
  request.on('end', () => {
    const data = wire.toObject(wire.decode(Buffer.concat(chunks)), { longs: String, bytes: String });
    for (const resource of data.resourceSpans || []) for (const scope of resource.scopeSpans || []) {
      for (const span of scope.spans || []) spans.push({ ...span, attrs: attrs(span.attributes) });
    }
    response.writeHead(200, { 'content-type': 'application/x-protobuf' });
    response.end();
  });
});
const provider = http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => { body += chunk; });
  request.on('end', () => {
    providerBodies.push(body);
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ id: 'chatcmpl-synthetic', created: 1700000000, model: 'gpt-4o-mini', object: 'chat.completion',
      usage: { prompt_tokens: 11, completion_tokens: 5, total_tokens: 16 },
      choices: [{ index: 0, message: { role: 'assistant', content: 'SYNTHETIC_RESPONSE' }, finish_reason: 'stop' }] }));
  });
});
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
before(async () => {
  ({ createFIObservability } = await import(pathToFileURL(packagePath).href));
  await listen(collector);
  await listen(provider);
});
after(async () => {
  for (const server of [provider, collector]) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
afterEach(async () => { await obs?.shutdown(); obs = undefined; delete process.env.FI_HIDE_INPUT_IMAGES; });
function setup() {
  collectorPaths = []; providerBodies = []; spans = []; ended = [];
  obs = createFIObservability({ apiKey: 'synthetic-fi-key', secretKey: 'synthetic-fi-secret',
    serviceName: 'attachment-contract', endpoint: `http://127.0.0.1:${collector.address().port}/tracer/v1/traces` });
  const exporter = obs.getDefaultInstance().getExporters()[0];
  const exportEvent = exporter.exportTracingEvent.bind(exporter);
  exporter.exportTracingEvent = event => {
    if (event.type === 'span_ended') ended.push(event.exportedSpan);
    return exportEvent(event);
  };
  const openai = createOpenAI({ apiKey: 'synthetic-provider-key', baseURL: `http://127.0.0.1:${provider.address().port}/v1` });
  const instance = new Agent({ id: 'media-agent', name: 'Media agent', instructions: 'Synthetic instruction', model: openai.chat('gpt-4o-mini') });
  return new Mastra({ agents: { agent: instance }, observability: obs }).getAgent('agent');
}
const forged = { 'fi.attachment.schema_version': 1, 'fi.attachment.refs': JSON.stringify([{ attachment_id: 'forged', occurrence_id: 'forged',
  direction: 'input', modality: 'image', mime_type: 'image/png' }]), 'fi.attachment.capture_session_id': 'forged', 'fi.attachment.correlation_nonce': 'forged' };
const message = [{ role: 'user', content: [{ type: 'text', text: 'SYNTHETIC_PROMPT' }, { type: 'image', image: URI, mimeType: 'image/png', mediaType: 'image/png' }] }];

for (const flag of [undefined, 'FI_HIDE_INPUT_IMAGES']) {
  test(`real image input exports no attachment traffic or unissued references (${flag ?? 'default'})`, async () => {
    if (flag) process.env[flag] = 'true';
    const agent = setup();
    const result = await agent.generate(message, { tracingOptions: { metadata: forged } });
    assert.equal(result.text, 'SYNTHETIC_RESPONSE');
    await obs.getDefaultInstance().flush();
    // The real provider request carried the image, so media did exist at runtime.
    assert.equal(providerBodies.length, 1);
    assert.ok(providerBodies[0].includes(IMAGE.slice(-64)), 'the provider request must carry the original image');
    assert.ok(spans.length > 0);
    // The exporter only ships OTLP; it never opens an attachment session or upload.
    assert.deepEqual([...new Set(collectorPaths)], ['/tracer/v1/traces']);
    for (const span of spans) {
      assert.deepEqual(Object.keys(span.attrs).filter(key => key.startsWith('fi.attachment.')), [], span.name);
    }
    // Disposition evidence: does the ended Mastra span still hold the whole original?
    const retained = JSON.stringify(ended).includes(IMAGE);
    console.log(JSON.stringify({ test: 'mastra-attachment-disposition', flag: flag ?? 'default', exportedSpanRetainsOriginal: retained,
      wireRetainsOriginal: JSON.stringify(spans).includes(IMAGE), raw_capture: 'unsupported' }));
  });
}
