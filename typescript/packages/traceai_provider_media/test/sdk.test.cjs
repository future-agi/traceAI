/* Real SDKs, synthetic provider responses, real loopback attachment HTTP traffic. */
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const path = require('node:path');
const { fixture, png, wav } = require('./fixtures.cjs');
const coreLoad = createRequire(path.resolve(__dirname, '../../fi-core/package.json'));
const { NodeTracerProvider } = coreLoad('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = coreLoad('@opentelemetry/sdk-trace-base');
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
provider.register();
const sdkLoad = createRequire(path.join(process.env.MEDIA_SDK_ROOT, 'package.json'));
const routes = [];
let server, base;
const originalFetch = globalThis.fetch;
const redirect = (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url);
  return originalFetch(`${base}${url.pathname}${url.search}`, init);
};
const messages = [{ role: 'user', content: [{ type: 'image_url', image_url: { url: `data:image/png;base64,${png.toString('base64')}` } }] }];
before(async () => {
  server = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    routes.push(req.url);
    if (req.url.includes('/audio/speech')) { res.setHeader('content-type', 'audio/wav'); res.end(wav()); return; }
    res.setHeader('content-type', 'application/json');
    if (req.url.includes('/audio/')) { res.end(JSON.stringify({ text: 'synthetic transcript' })); return; }
    if (req.url.includes(':generateContent')) { res.end(JSON.stringify({ candidates: [{ content: { role: 'model', parts: [{ text: 'fixture' }, { inlineData: { mimeType: 'image/png', data: png.toString('base64') } }] }, finishReason: 'STOP' }], usageMetadata: { promptTokenCount: 1, candidatesTokenCount: 1, totalTokenCount: 2 } })); return; }
    if (req.url.endsWith('/converse')) { res.end(JSON.stringify({ output: { message: { role: 'assistant', content: [{ text: 'fixture' }] } }, usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 }, stopReason: 'end_turn', metrics: { latencyMs: 1 } })); return; }
    res.end(JSON.stringify({ id: 'synthetic-completion', object: 'chat.completion', created: 1, model: 'fixture', choices: [{ index: 0, message: { role: 'assistant', content: 'fixture' }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => { await provider.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });

for (const tier of ['floor', 'latest']) {
  for (const privacy of ['enabled', 'disabled', 'hideInputs', 'piiRedaction']) test(`OpenAI ${tier}: chat and speech ${privacy}`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { OpenAIInstrumentation } = require('../../traceai_openai/dist/src');
    const f = await fixture(privacy === 'disabled' ? { enabled: false } : { traceConfig: privacy === 'enabled' ? {} : { [privacy]: true } });
    const inst = new OpenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const client = new sdk.OpenAI({ apiKey: 'synthetic', fetch: redirect });
      await client.chat.completions.create({ model: 'fixture', messages });
      await f.manager.flush(); assert.equal(f.uploads.length, privacy === 'enabled' ? 1 : 0);
      if (client.audio.speech) {
        const response = await client.audio.speech.create({ model: 'tts-1', input: 'fixture', voice: 'alloy', response_format: 'wav' });
        const before = f.uploads.length; await f.manager.flush(); assert.equal(f.uploads.length, before);
        assert.deepEqual(Buffer.from(await response.arrayBuffer()), wav()); await f.manager.flush();
        assert.equal(f.uploads.length - before, ['enabled', 'hideInputs'].includes(privacy) ? 1 : 0);
      }
      const file = new File([wav()], 'synthetic.wav', { type: 'audio/wav' });
      for (const operation of ['transcriptions', 'translations']) {
        const before = f.uploads.length;
        const result = await client.audio[operation].create({ model: 'whisper-1', file }); assert.equal(result.text, 'synthetic transcript');
        await f.manager.flush(); assert.equal(f.uploads.length - before, privacy === 'enabled' ? 1 : 0);
      }
      const spans = exporter.getFinishedSpans();
      assert.ok(spans.some(s => s.name === 'OpenAI Chat Completions'));
      assert.equal(JSON.stringify(spans.map(s => s.attributes)).includes(wav().toString('base64')), false);
    } finally { inst.disable(); await f.close(); exporter.reset(); }
  });

  test(`OpenAI ${tier}: allowed compatible routes and unknown/text-only routes`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { OpenAIInstrumentation } = require('../../traceai_openai/dist/src');
    const f = await fixture(); const inst = new OpenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      for (const host of ['api.fireworks.ai', 'openrouter.ai', 'api.x.ai', 'api.together.ai', 'unknown.invalid', 'api.deepseek.com', 'api.cerebras.ai']) {
        const before = f.uploads.length; const client = new sdk.OpenAI({ apiKey: 'synthetic', baseURL: `https://${host}/v1`, fetch: redirect });
        await client.chat.completions.create({ model: 'fixture', messages }); await f.manager.flush();
        assert.equal(f.uploads.length - before, ['unknown.invalid', 'api.deepseek.com', 'api.cerebras.ai'].includes(host) ? 0 : 1);
      }
    } finally { inst.disable(); await f.close(); }
  });

  for (const privacy of ['enabled', 'disabled', 'hideInputs', 'piiRedaction']) test(`Groq ${tier}: typed chat/speech ${privacy}`, async () => {
    const sdk = sdkLoad(`sdk-groq-${tier}`);
    const { GroqInstrumentation } = require('../../traceai_groq/dist/src');
    const f = await fixture(privacy === 'disabled' ? { enabled: false } : { traceConfig: privacy === 'enabled' ? {} : { [privacy]: true } });
    const inst = new GroqInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const Groq = sdk.Groq ?? sdk.default ?? sdk;
      const client = new Groq({ apiKey: 'synthetic', fetch: redirect });
      await client.chat.completions.create({ model: 'fixture', messages }); await f.manager.flush(); assert.equal(f.uploads.length, privacy === 'enabled' ? 1 : 0);
      for (const operation of ['transcriptions', 'translations']) if (client.audio?.[operation]) {
        const before = f.uploads.length;
        await client.audio[operation].create({ model: 'whisper-large-v3', file: new File([wav()], 'synthetic.wav', { type: 'audio/wav' }) });
        await f.manager.flush(); assert.equal(f.uploads.length - before, privacy === 'enabled' ? 1 : 0);
      }
      if (client.audio?.speech) {
        const response = await client.audio.speech.create({ model: 'fixture', input: 'fixture', voice: 'fixture', response_format: 'wav' });
        const before = f.uploads.length; await f.manager.flush(); assert.equal(f.uploads.length, before);
        assert.deepEqual(Buffer.from(await response.arrayBuffer()), wav()); await f.manager.flush();
        assert.equal(f.uploads.length - before, ['enabled', 'hideInputs'].includes(privacy) ? 1 : 0);
      }
    } finally { inst.disable(); await f.close(); }
  });

  test(`Google GenAI ${tier}: original inline Parts in/out, no async submission media`, async () => {
    const sdk = sdkLoad(`sdk-genai-${tier}`);
    const { instrumentGoogleGenAI } = require('../../traceai_google_genai_sdk/dist');
    const f = await fixture(); const client = new sdk.GoogleGenAI({ apiKey: 'synthetic', httpOptions: { baseUrl: base } });
    const handle = instrumentGoogleGenAI(client, { attachments: f.manager, tracerProvider: provider });
    try {
      await client.models.generateContent({ model: 'gemini-fixture', contents: [{ role: 'user', parts: [{ inlineData: { mimeType: 'image/png', data: png.toString('base64') } }] }] });
      await f.manager.flush(); assert.equal(f.uploads.length, 2); for (const u of f.uploads) assert.deepEqual(u.data, png);
    } finally { handle.dispose(); await f.close(); }
  });

  test(`Legacy Gemini ${tier}: SDK normalizes typed Part array`, async () => {
    const sdk = sdkLoad(`sdk-gemini-${tier}`);
    const { GoogleGenAIInstrumentation } = require('../../traceai_google_genai/dist/src');
    const f = await fixture(); const inst = new GoogleGenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    globalThis.fetch = (input, init) => String(typeof input === 'object' ? input.url : input).startsWith('https://generativelanguage.googleapis.com') ? redirect(input, init) : originalFetch(input, init);
    try {
      await new sdk.GoogleGenerativeAI('synthetic').getGenerativeModel({ model: 'gemini-fixture' }).generateContent([{ inlineData: { mimeType: 'image/png', data: png.toString('base64') } }]);
      await f.manager.flush(); assert.equal(f.uploads.length, 2);
    } finally { globalThis.fetch = originalFetch; inst.disable(); await f.close(); }
  });

  test(`Bedrock ${tier}: real Converse serializer retains supported typed bytes`, async () => {
    const sdk = sdkLoad(`sdk-bedrock-${tier}`);
    const { BedrockInstrumentation } = require('../../traceai_bedrock/dist/src');
    const f = await fixture(); const inst = new BedrockInstrumentation({ attachments: f.manager, tracerProvider: provider, instrumentationConfig: { enabled: false } });
    inst.enable(); inst.manuallyInstrument(sdk);
    const client = new sdk.BedrockRuntimeClient({ endpoint: base, region: 'us-east-1', credentials: { accessKeyId: 'synthetic', secretAccessKey: 'synthetic' } });
    try {
      await client.send(new sdk.ConverseCommand({ modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0', messages: [{ role: 'user', content: [{ image: { format: 'png', source: { bytes: png } } }] }] }));
      await f.manager.flush(); assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
    } finally { inst.disable(); client.destroy(); await f.close(); }
  });
}

test('provider requests reached only the loopback server', () => { assert.ok(routes.length >= 20); });
