/* Real SDKs, synthetic provider responses, real loopback attachment HTTP traffic. */
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
const { fixture, png, wav } = require('./fixtures.cjs');
const coreLoad = createRequire(path.resolve(__dirname, '../../fi-core/package.json'));
const { NodeTracerProvider } = coreLoad('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = coreLoad('@opentelemetry/sdk-trace-base');
const api = coreLoad('@opentelemetry/api');
const OWNER = Symbol.for('traceai.auto.attachment_owner.v1');
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
provider.register();
const sdkLoad = createRequire(path.join(process.env.MEDIA_SDK_ROOT, 'package.json'));
const routes = [];
let server, base;
const originalFetch = globalThis.fetch;
const redirect = (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url);
  // Older SDK shims pass node-fetch-only options (agent, polyfilled signals, stream bodies).
  const { agent, signal, ...rest } = init;
  const body = rest.body;
  const streamed = body != null && typeof body !== 'string' && !(body instanceof Uint8Array) && !(body instanceof ArrayBuffer) &&
    !(typeof FormData !== 'undefined' && body instanceof FormData) && !(typeof Blob !== 'undefined' && body instanceof Blob) && !(body instanceof URLSearchParams);
  return originalFetch(`${base}${url.pathname}${url.search}`, { ...rest, ...(signal instanceof AbortSignal ? { signal } : {}), ...(streamed ? { duplex: 'half' } : {}) });
};
// The loopback fixture speaks HTTP/1.1; newer Bedrock clients default to HTTP/2.
const http1 = tier => { const { NodeHttpHandler } = createRequire(sdkLoad.resolve(`sdk-bedrock-${tier}`))('@smithy/node-http-handler'); return new NodeHttpHandler(); };
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

// One tier per process: some instrumentations keep process-wide patch state.
const tiers = process.env.MEDIA_SDK_TIER ? [process.env.MEDIA_SDK_TIER] : ['floor', 'latest'];
for (const tier of tiers) {
  for (const privacy of ['enabled', 'disabled', 'hideInputs', 'piiRedaction']) test(`OpenAI ${tier}: speech ${privacy}; chat media stays with the OpenAI chat adapter`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { OpenAIInstrumentation } = require('../../traceai_openai/dist/src');
    const f = await fixture(privacy === 'disabled' ? { enabled: false } : { traceConfig: privacy === 'enabled' ? {} : { [privacy]: true } });
    const inst = new OpenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const client = new sdk.OpenAI({ apiKey: 'synthetic', fetch: redirect });
      await client.chat.completions.create({ model: 'fixture', messages });
      // This lane does not own the chat hook; no duplicate chat capture is added here.
      await f.manager.flush(); assert.equal(f.uploads.length, 0);
      if (client.audio.speech) {
        const response = await client.audio.speech.create({ model: 'tts-1', input: 'fixture', voice: 'alloy', response_format: 'wav' });
        const before = f.uploads.length; await f.manager.flush(); assert.equal(f.uploads.length, before);
        assert.deepEqual(Buffer.from(await response.arrayBuffer()), wav()); await f.manager.flush();
        assert.equal(f.uploads.length - before, ['enabled', 'hideInputs'].includes(privacy) ? 1 : 0);
        if (f.uploads.length > before) assert.equal(f.uploads.at(-1).request.direction, 'output');
        const unread = await client.audio.speech.create({ model: 'tts-1', input: 'fixture', voice: 'alloy', response_format: 'wav' });
        await unread.body?.cancel?.(); const after = f.uploads.length; await f.manager.flush(); assert.equal(f.uploads.length, after);
      }
      const file = new File([wav()], 'synthetic-private-name.wav', { type: 'audio/wav' });
      for (const operation of ['transcriptions', 'translations']) {
        const before = f.uploads.length;
        const result = await client.audio[operation].create({ model: 'whisper-1', file }); assert.equal(result.text, 'synthetic transcript');
        await f.manager.flush(); assert.equal(f.uploads.length - before, privacy === 'enabled' ? 1 : 0);
        if (f.uploads.length > before) assert.deepEqual(f.uploads.at(-1).data, wav());
      }
      const spans = exporter.getFinishedSpans();
      assert.ok(spans.some(s => s.name === 'OpenAI Chat Completions'));
      // Attachments OFF keeps audio calls span-free, exactly as before this change.
      assert.equal(spans.some(s => s.name.startsWith('OpenAI Audio')), privacy !== 'disabled');
      const exported = JSON.stringify(spans.map(s => s.attributes));
      assert.equal(exported.includes(wav().toString('base64')), false); assert.equal(exported.includes('synthetic-private-name'), false);
      assert.equal(JSON.stringify(f.uploads.map(u => u.request)).includes('synthetic-private-name'), false);
    } finally { inst.disable(); await f.close(); exporter.reset(); }
  });

  test(`OpenAI ${tier}: speech capture only on qualified audio hosts`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { OpenAIInstrumentation } = require('../../traceai_openai/dist/src');
    const f = await fixture(); const inst = new OpenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      for (const baseURL of ['https://api.groq.com/openai/v1', 'https://unknown.invalid/v1', 'https://api.fireworks.ai/inference/v1']) {
        const before = f.uploads.length; const client = new sdk.OpenAI({ apiKey: 'synthetic', baseURL, fetch: redirect });
        await client.audio.transcriptions.create({ model: 'whisper-1', file: new File([wav()], 'a.wav', { type: 'audio/wav' }) }); await f.manager.flush();
        assert.equal(f.uploads.length - before, baseURL.includes('api.groq.com') ? 1 : 0, baseURL);
      }
    } finally { inst.disable(); await f.close(); exporter.reset(); }
  });

  // The context the SDK's own fetch runs in is the one wire-level capture (the auto module) inspects.
  const marking = seen => (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url);
    seen.push([url.pathname.replace(/^.*\/(chat|audio)\//, '$1/'), api.context.active().getValue(OWNER)]);
    return redirect(input, init);
  };
  const ownerRows = async (client, model, voice, f, seen) => {
    // Claimed: audio calls whose media this capture owns carry the marker; chat (owned by the OpenAI chat adapter) does not.
    await client.chat.completions.create({ model: 'fixture', messages });
    if (client.audio?.speech) await (await client.audio.speech.create({ model: 'fixture', input: 'fixture', voice, response_format: 'wav' })).arrayBuffer();
    for (const operation of ['transcriptions', 'translations']) if (client.audio?.[operation]) await client.audio[operation].create({ model, file: new File([wav()], 'synthetic.wav', { type: 'audio/wav' }) });
    await f.manager.flush();
    const claimed = f.uploads.length;
    // Honoured: an application or adapter that already owns the call's media keeps it; this capture uploads nothing.
    await api.context.with(api.context.active().setValue(OWNER, true), async () => {
      if (client.audio?.speech) assert.deepEqual(Buffer.from(await (await client.audio.speech.create({ model: 'fixture', input: 'fixture', voice, response_format: 'wav' })).arrayBuffer()), wav());
      for (const operation of ['transcriptions', 'translations']) if (client.audio?.[operation]) await client.audio[operation].create({ model, file: new File([wav()], 'synthetic.wav', { type: 'audio/wav' }) });
    });
    await f.manager.flush();
    return { claimed, honoured: f.uploads.length - claimed };
  };

  for (const mode of ['enabled', 'disabled']) test(`OpenAI ${tier}: audio calls carry the shared owner marker (${mode}); an owned call uploads nothing`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { OpenAIInstrumentation } = require('../../traceai_openai/dist/src');
    const f = await fixture(mode === 'disabled' ? { enabled: false } : {});
    const inst = new OpenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const seen = [];
      const result = await ownerRows(new sdk.OpenAI({ apiKey: 'synthetic', fetch: marking(seen) }), 'whisper-1', 'alloy', f, seen);
      assert.deepEqual(seen[0], ['chat/completions', undefined]);
      const audio = seen.filter(([route]) => route.startsWith('audio/'));
      const claimed = audio.slice(0, audio.length / 2);
      assert.ok(claimed.length >= 2, JSON.stringify(seen));
      // Attachments OFF: audio calls are untouched, exactly as before (no span, no marker, no upload).
      for (const [, value] of claimed) assert.equal(value, mode === 'enabled' ? true : undefined, JSON.stringify(seen));
      assert.deepEqual(result, { claimed: mode === 'enabled' ? claimed.length : 0, honoured: 0 });
    } finally { inst.disable(); await f.close(); exporter.reset(); }
  });

  test(`OpenAI ${tier}: unqualified audio hosts are not claimed`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { OpenAIInstrumentation } = require('../../traceai_openai/dist/src');
    const f = await fixture(); const inst = new OpenAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const seen = [];
      await new sdk.OpenAI({ apiKey: 'synthetic', baseURL: 'https://unknown.invalid/v1', fetch: marking(seen) }).audio.transcriptions.create({ model: 'whisper-1', file: new File([wav()], 'a.wav', { type: 'audio/wav' }) });
      await f.manager.flush();
      assert.deepEqual(seen, [['audio/transcriptions', undefined]]); assert.equal(f.uploads.length, 0);
    } finally { inst.disable(); await f.close(); exporter.reset(); }
  });

  test(`Groq ${tier}: audio calls carry the shared owner marker; an owned call uploads nothing`, async () => {
    const sdk = sdkLoad(`sdk-groq-${tier}`);
    const { GroqInstrumentation } = require('../../traceai_groq/dist/src');
    const f = await fixture();
    const inst = new GroqInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const Groq = sdk.Groq ?? sdk.default ?? sdk;
      const seen = [];
      const client = new Groq({ apiKey: 'synthetic', fetch: marking(seen) });
      const plain = [{ role: 'user', content: 'fixture' }];
      await client.chat.completions.create({ model: 'fixture', messages: plain });
      const result = await ownerRows(client, 'whisper-large-v3', 'fixture', f, seen);
      const chat = seen.filter(([route]) => route === 'chat/completions');
      const audio = seen.filter(([route]) => route.startsWith('audio/'));
      assert.equal(chat.length, 2); for (const [, value] of chat) assert.equal(value, undefined);
      const owned = audio.length / 2;
      for (const [, value] of audio.slice(0, owned)) assert.equal(value, true, JSON.stringify(seen));
      // Chat media (one typed image) is still captured; audio uploads once per claimed call and never under an owner.
      assert.deepEqual(result, { claimed: 1 + owned, honoured: 0 });
    } finally { inst.disable(); await f.close(); exporter.reset(); }
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
    const client = new sdk.BedrockRuntimeClient({ endpoint: base, region: 'us-east-1', credentials: { accessKeyId: 'synthetic', secretAccessKey: 'synthetic' }, requestHandler: http1(tier) });
    try {
      await client.send(new sdk.ConverseCommand({ modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0', messages: [{ role: 'user', content: [{ image: { format: 'png', source: { bytes: png } } }] }] }));
      await f.manager.flush(); assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
    } finally { inst.disable(); client.destroy(); await f.close(); }
  });

  test(`Bedrock ${tier}: unsupported models keep span tracing without media capture`, async () => {
    const sdk = sdkLoad(`sdk-bedrock-${tier}`);
    const { BedrockInstrumentation } = require('../../traceai_bedrock/dist/src');
    const f = await fixture(); const inst = new BedrockInstrumentation({ attachments: f.manager, tracerProvider: provider, instrumentationConfig: { enabled: false } });
    inst.enable(); inst.manuallyInstrument(sdk);
    const client = new sdk.BedrockRuntimeClient({ endpoint: base, region: 'us-east-1', credentials: { accessKeyId: 'synthetic', secretAccessKey: 'synthetic' }, requestHandler: http1(tier) });
    try {
      await client.send(new sdk.ConverseCommand({ modelId: 'meta.llama3-70b-instruct-v1:0', messages: [{ role: 'user', content: [{ image: { format: 'png', source: { bytes: png } } }] }] }));
      await f.manager.flush(); assert.equal(f.uploads.length, 0);
      assert.ok(exporter.getFinishedSpans().some(s => s.name === 'bedrock.converse'));
    } finally { inst.disable(); client.destroy(); await f.close(); exporter.reset(); }
  });

  test(`Vertex AI ${tier}: inline Parts in/out through the real SDK`, async () => {
    const sdk = sdkLoad(`sdk-vertex-${tier}`);
    const auth = createRequire(sdkLoad.resolve(`sdk-vertex-${tier}`))('google-auth-library');
    const restoreToken = auth.GoogleAuth.prototype.getAccessToken;
    auth.GoogleAuth.prototype.getAccessToken = async () => 'synthetic-token';
    const { VertexAIInstrumentation } = require('../../traceai_vertexai/dist/src');
    const f = await fixture(); const inst = new VertexAIInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    globalThis.fetch = (input, init) => String(typeof input === 'object' ? input.url : input).startsWith('https://vertex.fixture.invalid') ? redirect(input, init) : originalFetch(input, init);
    try {
      const model = new sdk.VertexAI({ project: 'synthetic-project', location: 'us-central1', apiEndpoint: 'vertex.fixture.invalid' }).getGenerativeModel({ model: 'gemini-fixture' });
      await model.generateContent({ contents: [{ role: 'user', parts: [{ inlineData: { mimeType: 'image/png', data: png.toString('base64') } }] }] });
      await f.manager.flush(); assert.equal(f.uploads.length, 2);
      assert.deepEqual(f.uploads.map(u => u.request.direction).sort(), ['input', 'output']); for (const u of f.uploads) assert.deepEqual(u.data, png);
    } finally { globalThis.fetch = originalFetch; auth.GoogleAuth.prototype.getAccessToken = restoreToken; inst.disable(); await f.close(); }
  });

  for (const privacy of ['enabled', 'hideInputImages', 'piiRedaction']) test(`Mistral ${tier}: typed imageUrl chunk ${privacy}`, async () => {
    const imported = await import(pathToFileURL(sdkLoad.resolve(`sdk-mistral-${tier}`)).href);
    const sdk = imported.Mistral ? imported : imported.default;
    const { MistralInstrumentation } = require('../../traceai_mistral/dist/src');
    const f = await fixture({ traceConfig: privacy === 'enabled' ? {} : { [privacy]: true } });
    const inst = new MistralInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const sent = [];
      const client = new sdk.Mistral({ apiKey: 'synthetic', serverURL: base, httpClient: { request: async req => { sent.push(await req.clone().text()); return redirect(req.url, { method: req.method, headers: req.headers, body: await req.text() }); } } });
      await client.chat.complete({ model: 'pixtral-fixture', messages: [{ role: 'user', content: [{ type: 'text', text: 'describe' }, { type: 'image_url', imageUrl: `data:image/png;base64,${png.toString('base64')}` }] }] });
      assert.ok(sent.some(body => body.includes(png.toString('base64'))), 'SDK sent the typed image chunk');
      await f.manager.flush(); assert.equal(f.uploads.length, privacy === 'enabled' ? 1 : 0);
      if (f.uploads.length) { assert.deepEqual(f.uploads[0].data, png); assert.equal(f.uploads[0].request.part_index, 1); }
    } finally { inst.disable(); await f.close(); }
  });

  test(`Portkey ${tier}: OpenAI-shaped chat image parts`, async () => {
    const sdk = sdkLoad(`sdk-portkey-${tier}`);
    const { PortkeyInstrumentation } = require('../../traceai_portkey/dist/src');
    const f = await fixture(); const inst = new PortkeyInstrumentation({ attachments: f.manager, instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      const Portkey = sdk.Portkey ?? sdk.default;
      const client = new Portkey({ apiKey: 'synthetic', baseURL: `${base}/v1`, provider: 'openai' });
      inst.manuallyInstrument(client);
      await client.chat.completions.create({ model: 'fixture', messages });
      await f.manager.flush(); assert.equal(f.uploads.length, 1); assert.deepEqual(f.uploads[0].data, png);
    } finally { inst.disable(); await f.close(); }
  });

  test(`vLLM ${tier}: matched self-hosted route captures; unmatched route does not`, async () => {
    const sdk = sdkLoad(`sdk-openai-${tier}`);
    const { VLLMInstrumentation } = require('../../traceai_vllm/dist/cjs');
    const f = await fixture(); const inst = new VLLMInstrumentation({ attachments: f.manager, baseUrlPattern: '127.0.0.1', instrumentationConfig: { enabled: false } });
    inst.setTracerProvider(provider); inst.enable(); inst.manuallyInstrument(sdk);
    try {
      await new sdk.OpenAI({ apiKey: 'synthetic', baseURL: `${base}/v1` }).chat.completions.create({ model: 'fixture', messages });
      await f.manager.flush(); assert.equal(f.uploads.length, 1);
      await new sdk.OpenAI({ apiKey: 'synthetic', baseURL: 'https://other.invalid/v1', fetch: redirect }).chat.completions.create({ model: 'fixture', messages });
      await f.manager.flush(); assert.equal(f.uploads.length, 1);
    } finally { inst.disable(); await f.close(); }
  });
}

test('provider requests reached only the loopback server', () => { assert.ok(routes.length >= 20 * tiers.length); });
