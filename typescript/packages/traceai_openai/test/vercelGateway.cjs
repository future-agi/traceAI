// Actual OpenAI SDK calls with synthetic fetch responses and a localhost OTLP receiver.
// Run only with outbound networking disabled (loopback allowed).
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { test, before, after, beforeEach } = require('node:test');
const SDK = require(process.env.OPENAI_TEST_SDK || 'openai');
const OpenAI = SDK.OpenAI;
const { SemanticConventions: SC } = require('@traceai/fi-semantic-conventions');
const { register, ProjectType } = require('@traceai/fi-core');
const { OpenAIInstrumentation } = require('../dist/src');
const { diag, DiagLogLevel } = require('@opentelemetry/api');
const diagnostics = [];
diag.setLogger({ debug: (...args) => diagnostics.push(args), info() {}, warn() {}, error() {}, verbose() {} }, DiagLogLevel.DEBUG);

const gateway = 'https://ai-gateway.vercel.sh/v1';
const requestedModel = 'vendor/requested-model';
const responseModel = 'different-vendor/resolved-model';
const key = 'synthetic-gateway-credential';
const payloads = [];
let provider;
let server;

function attributes(list) {
  return Object.fromEntries(list.map(({ key, value }) => [key,
    value.stringValue ?? (value.intValue === undefined ? undefined : Number(value.intValue))
      ?? value.doubleValue ?? value.boolValue]));
}
function wireSpans() {
  return payloads.flatMap(p => p.resourceSpans.flatMap(r => {
    assert.equal(attributes(r.resource.attributes).project_name, 'vercel-gateway-contract');
    return r.scopeSpans.flatMap(s => s.spans);
  }));
}
function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
function reply(kind) {
  const usage = { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 };
  if (kind === 'embeddings') return { object: 'list', model: responseModel,
    data: [{ object: 'embedding', index: 0, embedding: [0.1, 0.2] }], usage };
  if (kind === 'responses') return { object: 'response', id: 'resp_synthetic', model: responseModel,
    status: 'completed', output: [{ type: 'message', role: 'assistant', content: [
      { type: 'output_text', text: 'synthetic-output', annotations: [] }], status: 'completed' }],
    usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 } };
  return { object: kind === 'chat' ? 'chat.completion' : 'text_completion', id: 'cmpl_synthetic',
    model: responseModel, created: 1, usage, choices: [{ index: 0, finish_reason: 'stop',
      ...(kind === 'chat' ? { message: { role: 'assistant', content: 'synthetic-output' } }
        : { text: 'synthetic-output' }) }] };
}
function client(baseURL = gateway, kind = 'chat', fail = false, stream = false) {
  return new OpenAI({ baseURL, apiKey: key, maxRetries: 0, fetch: async (_url, init) => {
    // Ensure tracing did not mutate the SDK request or credentials.
    const body = JSON.parse(init.body);
    assert.equal(body.model, requestedModel);
    if (fail) return json({ error: { message: 'synthetic-error-content', type: 'invalid_request_error' } }, 400);
    if (stream) {
      const chunks = [
        { id: 'chunk_synthetic', object: 'chat.completion.chunk', created: 1, model: responseModel,
          choices: [{ index: 0, delta: { role: 'assistant', content: 'synthetic-output' }, finish_reason: null }] },
        { id: 'chunk_synthetic', object: 'chat.completion.chunk', created: 1, model: responseModel,
          choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } },
      ];
      return new Response(chunks.map(c => `data: ${JSON.stringify(c)}\n\n`).join('') + 'data: [DONE]\n\n',
        { headers: { 'content-type': 'text/event-stream' } });
    }
    return json(reply(kind));
  } });
}
function invoke(sdk, kind) {
  if (kind === 'chat') return sdk.chat.completions.create({ model: requestedModel,
    messages: [{ role: 'user', content: 'synthetic-input' }] });
  if (kind === 'completions') return sdk.completions.create({ model: requestedModel, prompt: 'synthetic-input' });
  return sdk[kind].create({ model: requestedModel, input: 'synthetic-input' });
}
async function check(expected, kind = 'chat', error = false, streaming = false) {
  await provider.forceFlush();
  const spans = wireSpans();
  assert.equal(spans.length, 1, 'exactly one client span, not one per instrumentation registration');
  const span = spans[0];
  const attrs = attributes(span.attributes);
  assert.equal(attrs['gen_ai.provider.name'], expected);
  if (!streaming) assert.equal(span.status.code, error ? 2 : 1);
  if (kind !== 'embeddings') {
    assert.equal(attrs[SC.LLM_MODEL_NAME], error || streaming ? requestedModel : responseModel);
    if (!error && !streaming) {
      assert.equal(attrs[SC.LLM_TOKEN_COUNT_PROMPT], 3);
      assert.equal(attrs[SC.LLM_TOKEN_COUNT_COMPLETION], 2);
      assert.equal(attrs[SC.LLM_TOKEN_COUNT_TOTAL], 5);
    }
  }
  const wire = JSON.stringify(payloads);
  for (const marker of [key, 'url-user-marker', 'url-password-marker', 'url-query-marker', 'url-path-marker', 'url-fragment-marker']) {
    assert.ok(!wire.includes(marker), `URL or credential marker leaked: ${marker}`);
  }
  if (process.env.FI_HIDE_INPUTS === 'true' && kind !== 'embeddings') {
    for (const marker of ['synthetic-input', 'synthetic-output', 'synthetic-error-content']) {
      assert.ok(!wire.includes(marker), `content marker leaked: ${marker}`);
    }
  }
  return attrs;
}

before(async () => {
  server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      assert.equal(req.url, '/tracer/v1/traces');
      assert.equal(req.headers['x-api-key'], 'synthetic-fi-api');
      assert.equal(req.headers['x-secret-key'], 'synthetic-fi-secret');
      payloads.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{}');
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  provider = register({ projectName: 'vercel-gateway-contract', projectType: ProjectType.OBSERVE,
    endpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, batch: false,
    headers: { 'X-Api-Key': 'synthetic-fi-api', 'X-Secret-Key': 'synthetic-fi-secret' } });
  const instrumentation = new OpenAIInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(SDK);
  instrumentation.manuallyInstrument(SDK);
});
beforeEach(async () => { await provider.forceFlush(); payloads.length = 0; diagnostics.length = 0; });
after(async () => { await provider?.shutdown(); if (server) await new Promise(r => server.close(r)); });

for (const kind of ['chat', 'completions', 'embeddings', ...(OpenAI.Responses ? ['responses'] : [])]) {
  test(`${kind}: gateway attribution survives a different response model`, async () => {
    const result = await invoke(client(gateway, kind), kind);
    assert.equal(result.model, responseModel);
    await check('vercel', kind);
  });
  test(`${kind}: gateway attribution exists before an HTTP failure`, async () => {
    await assert.rejects(invoke(client(gateway, kind, true), kind), { status: 400 });
    await check('vercel', kind, true);
  });
}
for (const baseURL of [
  'https://AI-GATEWAY.VERCEL.SH/v1', 'https://ai-gateway.vercel.sh.:443/v1',
  'https://url-user-marker:url-password-marker@ai-gateway.vercel.sh:8443/url-path-marker?token=url-query-marker#url-fragment-marker',
]) {
  test(`exact normalized gateway host: ${baseURL}`, async () => {
    await invoke(client(baseURL), 'chat');
    await check('vercel');
  });
}
for (const baseURL of [
  'https://api.openai.com/v1', 'https://proxy.example/v1',
  'https://ai-gateway.vercel.sh.evil.example/v1', 'https://not-ai-gateway.vercel.sh/v1',
  'https://ai-gateway.vercel.sh@evil.example/v1', 'https://evil.example/ai-gateway.vercel.sh/v1',
]) {
  test(`preserve existing provider behavior outside the gateway: ${baseURL}`, async () => {
    await invoke(client(baseURL), 'chat');
    await check('openai');
  });
}
test('resolve per request after changing the SDK base URL', async () => {
  const sdk = client();
  await invoke(sdk, 'chat');
  await check('vercel');
  payloads.length = 0;
  sdk.baseURL = 'https://api.openai.com/v1';
  await invoke(sdk, 'chat');
  await check('openai');
});
test('concurrent clients do not share gateway attribution', async () => {
  await Promise.all([invoke(client(), 'chat'), invoke(client('https://api.openai.com/v1'), 'chat')]);
  await provider.forceFlush();
  const spans = wireSpans();
  assert.equal(spans.length, 2);
  assert.deepEqual(spans.map(s => attributes(s.attributes)['gen_ai.provider.name']).sort(), ['openai', 'vercel']);
});
for (const value of [undefined, 'not-a-url', new Error('url-password-marker')]) {
  test(`unavailable SDK metadata does not break the request: ${String(value)}`, async () => {
    const sdk = client();
    let reads = 0;
    Object.defineProperty(sdk, 'baseURL', { get() {
      // Metadata is unavailable to tracing; the SDK still obtains a working URL.
      if (reads++ > 0) return gateway;
      if (value instanceof Error) throw value;
      return value;
    } });
    await invoke(sdk, 'chat');
    await check('openai');
    if (value !== undefined) assert.ok(diagnostics.some(args => String(args[0]).includes('unable to resolve endpoint provider')));
    assert.ok(!require('node:util').inspect(diagnostics).includes('url-password-marker'));
  });
}
// The 4.0.0 SDK has no Stream.tee(); that existing lifecycle defect is repaired separately.
const sdkDirectory = require('node:path').dirname(require.resolve(process.env.OPENAI_TEST_SDK || 'openai'));
const major = Number(require(`${sdkDirectory}/package.json`).version.split('.')[0]);
if (major >= 5) {
  test('completed chat stream keeps gateway attribution on one client span', async () => {
    const stream = await client(gateway, 'chat', false, true).chat.completions.create({
      model: requestedModel, messages: [{ role: 'user', content: 'synthetic-input' }],
      stream: true, stream_options: { include_usage: true },
    });
    const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.equal(chunks.length, 2);
    await check('vercel', 'chat', false, true);
  });
}
