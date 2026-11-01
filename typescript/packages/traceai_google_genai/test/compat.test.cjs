// GENAI_TEST_CONSUMER points at a clean npm installation of the packed adapter.
// CI installs the real SDK at both ends of the existing >=0.1.0 peer range.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createRequire } = require('node:module');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { once } = require('node:events');
const http = require('node:http');
const path = require('node:path');
const { gunzipSync } = require('node:zlib');
const consumer = createRequire(path.join(process.env.GENAI_TEST_CONSUMER, 'package.json'));
const exec = promisify(execFile);

const answer = {
  candidates: [{ content: { role: 'model', parts: [{ text: 'synthetic-answer' }] }, finishReason: 'STOP' }],
  usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 2, totalTokenCount: 7 },
};

test('unsupported modern SDK does not poison legacy setup; legacy methods stay compatible', async () => {
  const { GoogleGenAIInstrumentation, isPatched } = consumer('@traceai/google-genai');
  const { NodeTracerProvider } = consumer('@opentelemetry/sdk-trace-node');
  const { InMemorySpanExporter, SimpleSpanProcessor } = consumer('@opentelemetry/sdk-trace-base');
  const { diag, DiagLogLevel } = consumer('@opentelemetry/api');
  const { SemanticConventions: S } = consumer('@traceai/fi-semantic-conventions');
  const modern = consumer('@google/genai');
  const google = consumer('@google/generative-ai');
  const warnings = [];
  diag.setLogger({ warn: message => warnings.push(message), error() {}, info() {}, debug() {}, verbose() {} }, DiagLogLevel.WARN);
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new GoogleGenAIInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  const originalFetch = global.fetch;
  const originals = Object.fromEntries(['generateContent', 'generateContentStream', 'embedContent', 'batchEmbedContents']
    .map(method => [method, google.GenerativeModel.prototype[method]]));
  try {
    assert.equal(isPatched(), false);
    instrumentation.manuallyInstrument(modern);
    instrumentation.manuallyInstrument({});
    instrumentation.manuallyInstrument({ GenerativeModel: { prototype: {} } });
    assert.equal(isPatched(), false);
    assert.notEqual(modern.fiPatched, true);
    assert.equal(warnings.length, 3);
    assert.ok(warnings.every(message => message.includes('legacy @google/generative-ai') && message.includes('@google/genai')));

    const requests = [];
    global.fetch = async (url, options) => {
      assert.match(String(url), /^https:\/\/generativelanguage\.googleapis\.com\//);
      requests.push(JSON.parse(options.body));
      const operation = new URL(url).pathname;
      const body = operation.endsWith(':batchEmbedContents') ? { embeddings: [{ values: [0.1, 0.2] }] }
        : operation.endsWith(':embedContent') ? { embedding: { values: [0.1, 0.2] } } : answer;
      return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
    };
    instrumentation.manuallyInstrument(google);
    assert.equal(isPatched(), true);
    const wrapped = google.GenerativeModel.prototype.generateContent;
    instrumentation.manuallyInstrument(google);
    assert.equal(google.GenerativeModel.prototype.generateContent, wrapped, 'no duplicate wrappers');
    assert.equal(google.GenerativeModel.prototype.generateContentStream, originals.generateContentStream, 'no added legacy streaming features');
    const model = new google.GoogleGenerativeAI('synthetic-google-key').getGenerativeModel({ model: 'synthetic-model' });
    const result = await model.generateContent('synthetic-prompt');
    assert.equal(result.response.text(), 'synthetic-answer');
    assert.deepEqual((await model.embedContent('synthetic-document')).embedding.values, [0.1, 0.2]);
    assert.deepEqual((await model.batchEmbedContents({ requests: [{ content: { role: 'user', parts: [{ text: 'synthetic-document' }] } }] })).embeddings[0].values, [0.1, 0.2]);
    assert.equal(requests.length, 3);
    await provider.forceFlush();
    const spans = exporter.getFinishedSpans();
    assert.deepEqual(spans.map(span => span.name), [
      'Google GenAI generateContent', 'Google GenAI embedContent', 'Google GenAI batchEmbedContents',
    ]);
    assert.equal(spans[0].attributes[S.LLM_MODEL_NAME], model.model);
    assert.equal(spans[0].attributes[S.OUTPUT_VALUE], 'synthetic-answer');
    assert.equal(spans[0].attributes[S.LLM_TOKEN_COUNT_TOTAL], 7);
    assert.ok(spans.every(span => span.status.code === 1));
    assert.ok(!JSON.stringify(spans.map(span => span.attributes)).includes('synthetic-google-key'));

    global.fetch = async () => { throw new Error('synthetic-transport-error'); };
    await assert.rejects(model.generateContent('synthetic-prompt'), /synthetic-transport-error/);
    await provider.forceFlush();
    assert.equal(exporter.getFinishedSpans().length, 4);
    assert.equal(exporter.getFinishedSpans()[3].status.code, 2);
  } finally {
    global.fetch = originalFetch;
    instrumentation.disable();
    // Manual instrumentation is process-scoped; restore the real SDK for test isolation.
    for (const [method, original] of Object.entries(originals)) google.GenerativeModel.prototype[method] = original;
    delete google.fiPatched;
    diag.disable();
    await provider.shutdown();
  }
});

for (const format of ['json', 'protobuf']) {
  for (const compression of ['none', 'gzip']) {
    test(`packed README collector setup exports ${format}/${compression}`, () => checkReadme('collector', format, compression));
    test(`packed README privacy configuration redacts ${format}/${compression}`, () => checkReadme('collector', format, compression, true));
  }
}
test('packed README Future AGI setup exports through the registered provider', () => checkReadme('futureagi', 'json', 'none'));
test('packed README privacy configuration hides generation content', () => checkReadme('futureagi', 'json', 'none', true));

async function checkReadme(mode, format, compression, hide = false) {
  const received = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      received.push({ headers: req.headers, url: req.url, body: Buffer.concat(chunks) });
      res.setHeader('content-type', format === 'json' ? 'application/json' : 'application/x-protobuf');
      res.end(format === 'json' ? '{}' : Buffer.alloc(0));
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  try {
    const { stdout } = await exec(process.execPath, [path.join(__dirname, 'readme-runner.cjs'), mode], {
      timeout: 30000,
      env: {
        PATH: process.env.PATH,
        GENAI_TEST_CONSUMER: process.env.GENAI_TEST_CONSUMER,
        FI_BASE_URL: endpoint,
        FI_API_KEY: 'synthetic-fi-key', FI_SECRET_KEY: 'synthetic-fi-secret',
        GOOGLE_API_KEY: 'synthetic-google-key', GOOGLE_MODEL: 'synthetic-model',
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: `${endpoint}/v1/traces`,
        OTEL_EXPORTER_OTLP_TRACES_COMPRESSION: compression,
        WIRE_FORMAT: format, TEST_HIDE_CONTENT: String(hide),
      },
    });
    assert.match(stdout, /synthetic-answer/);
    assert.equal(received.length, 1, 'one generation must emit exactly one request');
    const wire = received[0];
    assert.match(wire.url, /\/v1\/traces\/?$/);
    assert.equal(wire.headers['content-encoding'] || 'none', compression);
    const bytes = compression === 'gzip' ? gunzipSync(wire.body) : wire.body;
    let payload;
    if (format === 'json') {
      assert.match(wire.headers['content-type'], /application\/json/);
      payload = JSON.parse(bytes);
    } else {
      assert.match(wire.headers['content-type'], /application\/x-protobuf/);
      // Use the exporter's exact OTLP schema rather than a hand-written decoder.
      const root = consumer('@opentelemetry/otlp-transformer/build/src/generated/root');
      const message = root.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
      payload = message.toObject(message.decode(bytes), { longs: Number });
    }
    const spans = payload.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans));
    assert.equal(spans.length, 1);
    assert.equal(spans[0].name, 'Google GenAI generateContent');
    const attributes = Object.fromEntries(spans[0].attributes.map(attribute => [attribute.key, attribute.value]));
    const { SemanticConventions: S } = consumer('@traceai/fi-semantic-conventions');
    assert.match(attributes[S.LLM_MODEL_NAME].stringValue, /^(models\/)?synthetic-model$/);
    assert.equal(Number(attributes[S.LLM_TOKEN_COUNT_TOTAL].intValue), 7);
    const serialized = JSON.stringify(payload);
    assert.ok(!serialized.includes('synthetic-google-key'));
    if (hide) {
      assert.ok(!serialized.includes('synthetic-answer'));
      assert.ok(!serialized.includes('Explain quantum computing'));
    } else {
      assert.equal(attributes[S.OUTPUT_VALUE].stringValue, 'synthetic-answer');
    }
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}
