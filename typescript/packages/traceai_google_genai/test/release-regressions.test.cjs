// Run against packed packages and real SDKs at the floor/latest peer checkpoints.
const assert = require('node:assert/strict');
const { test, before, after } = require('node:test');
const { createRequire } = require('node:module');
const path = require('node:path');
const http = require('node:http');
const req = createRequire(path.join(process.env.GENAI_TEST_CONSUMER, 'package.json'));
const google = req('@google/generative-ai');
const { version: sdkVersion } = req(path.resolve(req.resolve('@google/generative-ai'), '../../package.json'));
const { GoogleGenAIInstrumentation, isPatched } = req('@traceai/google-genai');
const { NodeTracerProvider } = req('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = req('@opentelemetry/sdk-trace-base');
const { context, trace } = req('@opentelemetry/api');
const { suppressTracing } = req('@opentelemetry/core');
const { OTLPTraceExporter } = req('@opentelemetry/exporter-trace-otlp-http');
const memory = new InMemorySpanExporter();
let provider;
const nativeFetch = global.fetch;
const answer = {
  candidates: [{ content: { role: 'model', parts: [{ text: 'OUTPUT_SENTINEL' }] }, finishReason: 'STOP' }],
  usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 3, totalTokenCount: 8 },
  modelVersion: 'gemini-resolved',
};
let server, response, bodies, providerFailure;
let exportedPayloads = [];
before(async () => {
  server = http.createServer(async (request, res) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    res.setHeader('content-type', 'application/json');
    if (request.url === '/v1/traces') {
      exportedPayloads.push(body);
      return res.end('{}');
    }
    bodies.push(body);
    if (providerFailure) {
      res.statusCode = 400;
      return res.end(JSON.stringify({ error: { message: 'synthetic failure', code: 400, status: 'INVALID_ARGUMENT' } }));
    }
    res.end(JSON.stringify(request.url.includes('batchEmbedContents') ? { embeddings: [{ values: [0.2] }] }
      : request.url.includes('embedContent') ? { embedding: { values: [0.2] } } : response));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(memory),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` })),
  ] });
  provider.register();
  global.fetch = (url, options) => nativeFetch(`http://127.0.0.1:${server.address().port}${new URL(url).pathname}`, options);
});
after(async () => {
  global.fetch = nativeFetch;
  await provider.shutdown();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
});
async function fixture(traceConfig, callback) {
  await provider.forceFlush();
  exportedPayloads = [];
  memory.reset(); bodies = []; response = answer; providerFailure = false;
  const instrumentation = new GoogleGenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider);
  const model = new google.GoogleGenerativeAI('SYNTHETIC_GOOGLE_KEY').getGenerativeModel({ model: 'gemini-test' });
  try { await callback(instrumentation, model); }
  finally {
    instrumentation.disable();
    if (process.env.OTLP_EVIDENCE) {
      await provider.forceFlush();
      require('node:fs').appendFileSync(process.env.OTLP_EVIDENCE,
        JSON.stringify({ wire: bodies, payloads: exportedPayloads }) + '\n');
    }
  }
}
const telemetry = () => JSON.stringify(memory.getFinishedSpans().map(s => ({ attributes: s.attributes, status: s.status, events: s.events })));

test('all generateContent overloads preserve wire requests, results and parent context', async () => {
  for (const input of ['INPUT_SENTINEL', ['INPUT_SENTINEL', 'SECOND'], [{ text: 'INPUT_SENTINEL' }],
    { contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }] }]) {
    await fixture({}, async (instrumentation, model) => {
      const baseline = await model.generateContent(input);
      instrumentation.manuallyInstrument(google);
      const parent = provider.getTracer('test').startSpan('parent');
      const result = await context.with(trace.setSpan(context.active(), parent), () => model.generateContent(input));
      assert.deepEqual(bodies[1], bodies[0]);
      assert.equal(result.response.text(), baseline.response.text());
      assert.equal(memory.getFinishedSpans().length, 1);
      assert.equal(memory.getFinishedSpans()[0].parentSpanContext.spanId, parent.spanContext().spanId);
      assert.match(telemetry(), /INPUT_SENTINEL/);
      parent.end();
    });
  }
});

test('resolved safety, empty and candidate-without-content responses stay resolved', async () => {
  for (const body of [{ promptFeedback: { blockReason: 'SAFETY' } }, {}, { candidates: [{ finishReason: 'SAFETY' }] }]) {
    await fixture({}, async (instrumentation, model) => {
      response = body;
      const baseline = await model.generateContent('INPUT_SENTINEL');
      instrumentation.manuallyInstrument(google);
      const result = await model.generateContent('INPUT_SENTINEL');
      assert.deepEqual(JSON.parse(JSON.stringify(result)), JSON.parse(JSON.stringify(baseline)));
      assert.equal(memory.getFinishedSpans().length, 1);
      assert.equal(memory.getFinishedSpans()[0].status.code, 1);
    });
  }
});

for (const hidden of [undefined, 'hideInputs', 'hideInputText', 'hideInputMessages',
  'FI_HIDE_INPUTS', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_INPUT_MESSAGES']) {
  test(`tool declarations and every duplicate input surface honor ${hidden || 'visible capture'}`, async () => {
    const env = hidden?.startsWith('FI_');
    const previous = env && process.env[hidden];
    if (env) process.env[hidden] = 'true';
    try {
      await fixture(hidden && !env ? { [hidden]: true } : {}, async (instrumentation, model) => {
        instrumentation.manuallyInstrument(google);
        await model.generateContent({ contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }],
          tools: [{ functionDeclarations: [{ name: 'lookup', description: 'TOOL_PRIVATE_SENTINEL',
            parameters: { type: 'OBJECT', properties: { query: { type: 'STRING', description: 'SCHEMA_PRIVATE_SENTINEL' } } } }] }] });
        assert.equal(bodies.length, 1);
        assert.match(JSON.stringify(bodies), /TOOL_PRIVATE_SENTINEL/);
        for (const sentinel of ['INPUT_SENTINEL', 'TOOL_PRIVATE_SENTINEL', 'SCHEMA_PRIVATE_SENTINEL']) {
          assert.equal(telemetry().includes(sentinel), !hidden, sentinel);
        }
      });
    } finally {
      if (env) { if (previous === undefined) delete process.env[hidden]; else process.env[hidden] = previous; }
    }
  });
}

for (const operation of ['generateContent', 'embedContent', 'batchEmbedContents']) {
  test(`${operation} real SDK errors preserve rejection but never export keys`, async () => {
    for (const traceConfig of [{}, { hideInputs: true }, { hideErrorDetails: false }]) {
      await fixture(traceConfig, async (instrumentation) => {
        // The floor SDK sends the key in the URL and quotes it in provider errors.
        // The latest SDK uses Headers, whose validation can echo an invalid key.
        providerFailure = sdkVersion === '0.1.0';
        const key = providerFailure ? 'SYNTHETIC_GOOGLE_KEY' : 'SYNTHETIC_GOOGLE_KEY\nINVALID';
        const model = new google.GoogleGenerativeAI(key).getGenerativeModel({ model: 'gemini-test' });
        instrumentation.manuallyInstrument(google);
        const input = operation === 'batchEmbedContents'
          ? { requests: [{ content: { role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] } }] } : 'INPUT_SENTINEL';
        await assert.rejects(model[operation](input), error => {
          assert.match(error.message, /SYNTHETIC_GOOGLE_KEY/);
          return true;
        });
        assert.equal(bodies.length, providerFailure ? 1 : 0, 'latest SDK rejects the header before dispatch; floor quotes the request URL');
        assert.equal(memory.getFinishedSpans().length, 1);
        assert.equal(memory.getFinishedSpans()[0].status.code, 2);
        assert.doesNotMatch(telemetry(), /SYNTHETIC_GOOGLE_KEY|INVALID/);
      });
    }
  });
}

test('manual disable restores all wrappers; enable is idempotent and cannot steal ownership', async () => {
  await fixture({}, async (instrumentation, model) => {
    const methods = ['generateContent', 'embedContent', 'batchEmbedContents'];
    const originals = methods.map(m => google.GenerativeModel.prototype[m]);
    const other = new GoogleGenAIInstrumentation({ instrumentationConfig: { enabled: false } });
    other.setTracerProvider(provider);
    const callAll = async () => {
      await model.generateContent('INPUT_SENTINEL');
      await model.embedContent('INPUT_SENTINEL');
      await model.batchEmbedContents({ requests: [{ content: { parts: [{ text: 'INPUT_SENTINEL' }] } }] });
    };
    try {
      instrumentation.manuallyInstrument(google);
      other.manuallyInstrument(google);
      other.disable();
      await callAll(); assert.equal(memory.getFinishedSpans().length, 3);
      instrumentation.disable(); instrumentation.disable();
      assert.equal(isPatched(), false);
      methods.forEach((m, i) => assert.equal(google.GenerativeModel.prototype[m], originals[i]));
      await callAll(); assert.equal(memory.getFinishedSpans().length, 3);
      instrumentation.enable(); instrumentation.enable();
      await callAll(); assert.equal(memory.getFinishedSpans().length, 6);
    } finally { other.disable(); }
  });
});

test('capture failures do not alter sync throws, promise rejections or resolved response identity', async () => {
  await fixture({}, async instrumentation => {
    const rejection = new Error('SYNTHETIC_GOOGLE_KEY');
    const result = { response: { get candidates() { throw new Error('capture failed'); } } };
    const circular = {}; circular.contents = circular;
    const options = { timeout: 123 };
    class Model {
      generateContent(input, requestOptions) {
        assert.equal(input, circular);
        assert.equal(requestOptions, options);
        return Promise.resolve(result);
      }
      embedContent() { throw rejection; }
      batchEmbedContents() { return Promise.reject(rejection); }
    }
    instrumentation.manuallyInstrument({ GenerativeModel: Model });
    const model = new Model();
    assert.equal(await model.generateContent(circular, options), result);
    assert.throws(() => model.embedContent(undefined), error => error === rejection);
    await assert.rejects(model.batchEmbedContents(undefined), error => error === rejection);
    assert.equal(memory.getFinishedSpans().length, 3);
    assert.deepEqual(memory.getFinishedSpans().map(s => s.status.code), [1, 2, 2]);
    assert.doesNotMatch(telemetry(), /SYNTHETIC_GOOGLE_KEY|capture failed/);
  });
});

test('suppressed calls bypass capture and do not emit spans', async () => {
  await fixture({}, async (instrumentation, model) => {
    instrumentation.manuallyInstrument(google);
    await context.with(suppressTracing(context.active()), () => model.generateContent('INPUT_SENTINEL'));
    assert.equal(bodies.length, 1);
    assert.equal(memory.getFinishedSpans().length, 0);
  });
});

test('one instance tracks independent module copies and finishes in-flight spans after disable', async () => {
  await fixture({}, async instrumentation => {
    let resolve;
    class First { generateContent() { return Promise.resolve({ response: answer }); } }
    class Second { generateContent() { return new Promise(done => { resolve = done; }); } }
    const originals = [First.prototype.generateContent, Second.prototype.generateContent];
    instrumentation.manuallyInstrument({ GenerativeModel: First });
    instrumentation.manuallyInstrument({ GenerativeModel: Second });
    await new First().generateContent('INPUT_SENTINEL');
    const pending = new Second().generateContent('INPUT_SENTINEL');
    assert.equal(memory.getFinishedSpans().length, 1);
    instrumentation.disable();
    assert.equal(First.prototype.generateContent, originals[0]);
    assert.equal(Second.prototype.generateContent, originals[1]);
    const result = { response: answer };
    resolve(result);
    assert.equal(await pending, result);
    assert.equal(memory.getFinishedSpans().length, 2);
    await new First().generateContent('INPUT_SENTINEL');
    assert.equal(memory.getFinishedSpans().length, 2);
  });
});

for (const option of ['hideOutputText', 'hideOutputMessages', 'hideOutputs', 'hideInputImages', 'hideEmbeddingVectors']) {
  for (const environment of [false, true]) {
    test(`published-core granular privacy: ${option} (${environment ? 'environment' : 'config'})`, async () => {
      const key = 'FI_' + option.replace(/[A-Z]/g, c => '_' + c).toUpperCase();
      const previous = process.env[key];
      if (environment) process.env[key] = 'true';
      try {
        await fixture(environment ? {} : { [option]: true }, async (instrumentation, model) => {
          instrumentation.manuallyInstrument(google);
          response = { ...answer, candidates: [{ content: { role: 'model', parts: [
            { text: 'OUTPUT_SENTINEL' }, { functionCall: { name: 'lookup', args: { query: 'TOOL_OUTPUT_SENTINEL' } } },
          ] }, finishReason: 'STOP' }] };
          const input = { contents: [{ role: 'user', parts: [
            { text: 'INPUT_SENTINEL' }, { inlineData: { mimeType: 'image/png', data: 'IMAGE_SENTINEL' } },
          ] }] };
          const baseline = await model.generateContent(input);
          assert.equal(baseline.response.text(), 'OUTPUT_SENTINEL');
          assert.match(JSON.stringify(bodies), /IMAGE_SENTINEL/);
          if (option.startsWith('hideOutput')) {
            assert.doesNotMatch(telemetry(), /OUTPUT_SENTINEL|TOOL_OUTPUT_SENTINEL/);
          }
          if (option === 'hideInputImages') {
            assert.doesNotMatch(telemetry(), /IMAGE_SENTINEL/);
            const embeddingInput = { content: input.contents[0] };
            await model.embedContent(embeddingInput);
            await model.batchEmbedContents({ requests: [embeddingInput] });
            assert.doesNotMatch(telemetry(), /IMAGE_SENTINEL/);
            assert.match(JSON.stringify(bodies[1]), /IMAGE_SENTINEL/);
            assert.match(JSON.stringify(bodies[2]), /IMAGE_SENTINEL/);
          }
          const attributes = memory.getFinishedSpans()[0].attributes;
          assert.equal(attributes['gen_ai.usage.input_tokens'], 5);
          assert.equal(attributes['gen_ai.usage.output_tokens'], 3);
          assert.equal(attributes['gen_ai.request.model'], model.model);
          assert.equal(attributes['gen_ai.response.model'], answer.modelVersion);
          if (option === 'hideEmbeddingVectors' || option === 'hideOutputs') {
            memory.reset();
            const single = await model.embedContent('INPUT_SENTINEL');
            const batch = await model.batchEmbedContents({ requests: [{ content: { parts: [{ text: 'INPUT_SENTINEL' }] } }] });
            assert.deepEqual(single.embedding.values, [0.2]);
            assert.deepEqual(batch.embeddings[0].values, [0.2]);
            assert.doesNotMatch(telemetry(), /0\.2/);
            assert.equal(memory.getFinishedSpans().length, 2);
          }
        });
      } finally {
        if (previous === undefined) delete process.env[key]; else process.env[key] = previous;
      }
    });
  }
}

async function exportedAttributes() {
  await provider.forceFlush();
  const spans = exportedPayloads.flatMap(p => p.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.length, 1, 'one actual OTLP span per generation');
  const attributes = Object.fromEntries(spans[0].attributes.map(({ key, value }) => [key,
    value.stringValue ?? Number(value.intValue ?? value.doubleValue)]));
  assert.equal(attributes['gen_ai.usage.input_tokens'], 5);
  assert.equal(attributes['gen_ai.usage.output_tokens'], 3);
  assert.equal(attributes['gen_ai.usage.total_tokens'], 8);
  assert.doesNotMatch(JSON.stringify(exportedPayloads), /SYNTHETIC_GOOGLE_KEY/);
  return attributes;
}

function configuredModel() {
  return new google.GoogleGenerativeAI('SYNTHETIC_GOOGLE_KEY').getGenerativeModel({
    model: 'gemini-request-alias',
    generationConfig: { temperature: 0.73, topP: 0.91, stopSequences: ['STOP_PRIVATE_SENTINEL'] },
    systemInstruction: 'SYSTEM_PRIVATE_SENTINEL',
  });
}

for (const input of ['INPUT_SENTINEL', ['INPUT_SENTINEL'], [{ text: 'INPUT_SENTINEL' }],
  { contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }] }]) {
  test(`effective model defaults match provider HTTP and OTLP (${JSON.stringify(input)})`, async () => {
    await fixture({}, async instrumentation => {
      const model = configuredModel();
      const configBefore = JSON.stringify(model.generationConfig);
      const baseline = await model.generateContent(input);
      instrumentation.manuallyInstrument(google);
      const result = await model.generateContent(input);
      assert.deepEqual(bodies[1], bodies[0], 'instrumentation must not change the wire request');
      assert.equal(result.response.text(), baseline.response.text());
      assert.equal(JSON.stringify(model.generationConfig), configBefore);
      const attributes = await exportedAttributes();
      assert.equal(bodies[1].generationConfig.temperature, 0.73);
      assert.deepEqual(JSON.parse(attributes['gen_ai.request.parameters']), bodies[1].generationConfig);
      for (const key of ['input.value', 'raw.input']) {
        const captured = JSON.parse(attributes[key]);
        assert.deepEqual(captured.generationConfig, bodies[1].generationConfig);
        assert.deepEqual(captured.systemInstruction, bodies[1].systemInstruction);
      }
      const messages = JSON.parse(attributes['gen_ai.input.messages']);
      if (sdkVersion === '0.1.0') {
        // This SDK predates model-level system instructions; do not invent support.
        assert.equal(bodies[1].systemInstruction, undefined);
        assert.doesNotMatch(JSON.stringify(exportedPayloads), /SYSTEM_PRIVATE_SENTINEL/);
      } else {
        assert.equal(bodies[1].systemInstruction.parts[0].text, 'SYSTEM_PRIVATE_SENTINEL');
        assert.deepEqual(messages[0], { role: 'system', content: 'SYSTEM_PRIVATE_SENTINEL' });
      }
    });
  });
}

test('per-call generation config replaces model defaults, including empty and undefined values', async () => {
  for (const generationConfig of [{ temperature: 0.12 }, {}, undefined, null]) {
    await fixture({}, async instrumentation => {
      const model = configuredModel();
      const input = { contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }], generationConfig };
      const before = JSON.stringify(model.generationConfig);
      await model.generateContent(structuredClone(input));
      instrumentation.manuallyInstrument(google);
      await model.generateContent(input);
      assert.deepEqual(bodies[1], bodies[0]);
      assert.equal(JSON.stringify(model.generationConfig), before);
      assert.equal(input.generationConfig, generationConfig, 'retain the caller object');
      const attributes = await exportedAttributes();
      if (generationConfig != null) {
        assert.deepEqual(JSON.parse(attributes['gen_ai.request.parameters']), generationConfig);
      } else {
        assert.equal(attributes['gen_ai.request.parameters'], undefined);
      }
      assert.equal(bodies[1].generationConfig?.topP, undefined, 'the SDK does not deep-merge defaults');
      assert.doesNotMatch(JSON.stringify(exportedPayloads), /STOP_PRIVATE_SENTINEL/);
    });
  }
});

test('per-call system instruction forms override defaults without extra argument mutation', async () => {
  for (const systemInstruction of ['CALL_SYSTEM_SENTINEL', { text: 'CALL_SYSTEM_SENTINEL' },
    { role: 'system', parts: [{ text: 'CALL_SYSTEM_SENTINEL' }] }, undefined, null]) {
    await fixture({}, async instrumentation => {
      const model = configuredModel();
      const input = { contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }], systemInstruction };
      const nativeInput = structuredClone(input);
      const modelBefore = JSON.stringify(model.systemInstruction);
      await model.generateContent(nativeInput);
      instrumentation.manuallyInstrument(google);
      await model.generateContent(input);
      assert.deepEqual(input, nativeInput, 'only native SDK normalization may change input');
      assert.equal(JSON.stringify(model.systemInstruction), modelBefore);
      assert.deepEqual(bodies[1], bodies[0]);
      const attributes = await exportedAttributes();
      assert.doesNotMatch(JSON.stringify(exportedPayloads), /SYSTEM_PRIVATE_SENTINEL/);
      const messages = JSON.parse(attributes['gen_ai.input.messages']);
      if (systemInstruction != null) {
        assert.deepEqual(messages[0], { role: 'system', content: 'CALL_SYSTEM_SENTINEL' });
        assert.match(JSON.stringify(bodies[1].systemInstruction), /CALL_SYSTEM_SENTINEL/);
      } else {
        assert.equal(messages.some(m => m.role === 'system'), false);
        assert.equal(bodies[1].systemInstruction ?? null, null);
      }
    });
  }
});

test('response model comes only from returned modelVersion, never the request alias', async () => {
  for (const modelVersion of ['gemini-resolved', undefined, '', '  ', null, 123]) {
    await fixture({}, async instrumentation => {
      const model = configuredModel();
      response = { ...answer, modelVersion };
      instrumentation.manuallyInstrument(google);
      const result = await model.generateContent('INPUT_SENTINEL');
      assert.equal(result.response.modelVersion, modelVersion);
      const attributes = await exportedAttributes();
      assert.equal(attributes['gen_ai.request.model'], model.model);
      assert.equal(attributes['gen_ai.response.model'], modelVersion === 'gemini-resolved' ? modelVersion : undefined);
    });
  }
});

for (const option of ['hideInputs', 'hideInputText', 'hideInputMessages', 'hideInputImages',
  'hideOutputs', 'hideOutputText', 'hideOutputMessages']) {
  for (const environment of [false, true]) {
    test(`effective metadata privacy over OTLP: ${option} (${environment ? 'environment' : 'config'})`, async () => {
      const key = 'FI_' + option.replace(/[A-Z]/g, c => '_' + c).toUpperCase();
      const previous = process.env[key];
      if (environment) process.env[key] = 'true';
      try {
        await fixture(environment ? {} : { [option]: true }, async instrumentation => {
          const model = configuredModel();
          instrumentation.manuallyInstrument(google);
          const result = await model.generateContent('INPUT_SENTINEL');
          assert.equal(result.response.text(), 'OUTPUT_SENTINEL');
          assert.equal(bodies[0].generationConfig.temperature, 0.73);
          assert.match(JSON.stringify(bodies), /INPUT_SENTINEL|STOP_PRIVATE_SENTINEL/);
          const attributes = await exportedAttributes();
          assert.equal(attributes['gen_ai.request.model'], model.model);
          assert.equal(attributes['gen_ai.response.model'], 'gemini-resolved');
          if (option.startsWith('hideInput')) {
            assert.doesNotMatch(JSON.stringify(exportedPayloads), /INPUT_SENTINEL|SYSTEM_PRIVATE_SENTINEL|STOP_PRIVATE_SENTINEL/);
          } else {
            assert.doesNotMatch(JSON.stringify(exportedPayloads), /OUTPUT_SENTINEL/);
            // fi-core 1.2.0 projects request metadata for either input or output
            // hiding. Only canonical configuration keys survive; provider-specific
            // topP and content-bearing stopSequences remain fail-closed.
            assert.deepEqual(JSON.parse(attributes['gen_ai.request.parameters']), {
              temperature: 0.73, topP: '__REDACTED__', stopSequences: '__REDACTED__',
            });
            assert.doesNotMatch(attributes['gen_ai.request.parameters'], /STOP_PRIVATE_SENTINEL/);
          }
        });
      } finally {
        if (previous === undefined) delete process.env[key]; else process.env[key] = previous;
      }
    });
  }
}

const modelTool = [{ functionDeclarations: [{ name: 'lookup', description: 'MODEL_TOOL_SENTINEL',
  parameters: { type: 'OBJECT', properties: { q: { type: 'STRING', description: 'MODEL_SCHEMA_SENTINEL' } } } }] }];
function toolModel() {
  return new google.GoogleGenerativeAI('SYNTHETIC_GOOGLE_KEY').getGenerativeModel({
    model: 'gemini-request-alias', generationConfig: { temperature: 0.73 },
    safetySettings: [{ category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_ONLY_HIGH' }],
    tools: modelTool, toolConfig: { functionCallingConfig: { mode: 'AUTO' } },
    cachedContent: { name: 'cachedContents/MODEL_CACHE_SENTINEL',
      get contents() { throw new Error('cache contents must never be read'); },
      toJSON() { throw new Error('cache object must never be serialized'); } },
  });
}
const effectiveKeys = ['generationConfig', 'safetySettings', 'tools', 'toolConfig', 'cachedContent'];
function assertEffectiveInput(attributes, wire) {
  for (const key of ['raw.input', 'input.value']) {
    const captured = JSON.parse(attributes[key]);
    for (const field of effectiveKeys) assert.deepEqual(captured[field], wire[field], `${key}.${field}`);
  }
  const declarations = wire.tools?.flatMap(tool => tool.functionDeclarations || []);
  assert.deepEqual(attributes['gen_ai.tool.definitions'] && JSON.parse(attributes['gen_ai.tool.definitions']),
    declarations?.length ? declarations : undefined);
}

for (const [name, input] of [['string', 'INPUT_SENTINEL'], ['strings', ['INPUT_SENTINEL']],
  ['parts', [{ text: 'INPUT_SENTINEL' }]], ['contents', { contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }] }]]) {
  test(`GG-14 supported model tools, safety and cache name match native HTTP: ${name}`, async () => {
    await fixture({}, async instrumentation => {
      const model = toolModel();
      const before = [model.tools, model.toolConfig, model.cachedContent, model.safetySettings];
      const baseline = await model.generateContent(structuredClone(input));
      instrumentation.manuallyInstrument(google);
      const result = await model.generateContent(structuredClone(input));
      assert.deepEqual(bodies[1], bodies[0]);
      assert.deepEqual(result.response.candidates, baseline.response.candidates);
      [model.tools, model.toolConfig, model.cachedContent, model.safetySettings].forEach((value, i) => assert.equal(value, before[i]));
      const attributes = await exportedAttributes();
      assertEffectiveInput(attributes, bodies[1]);
      assert.deepEqual(bodies[1].safetySettings, model.safetySettings);
      if (sdkVersion === '0.1.0') {
        assert.equal(bodies[1].tools, undefined);
        assert.doesNotMatch(JSON.stringify(exportedPayloads), /MODEL_TOOL_SENTINEL|MODEL_CACHE_SENTINEL/);
      } else {
        assert.deepEqual(bodies[1].tools, modelTool);
        assert.equal(bodies[1].cachedContent, 'cachedContents/MODEL_CACHE_SENTINEL');
      }
    });
  });
}

for (const [key, overrides] of [
  ['tools', [[{ functionDeclarations: [{ name: 'override' }] }], [], undefined, null]],
  ['toolConfig', [{ functionCallingConfig: { mode: 'NONE' } }, {}, undefined, null]],
  ['safetySettings', [[{ category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_NONE' }], [], undefined, null]],
  ['cachedContent', ['cachedContents/override', '', undefined, null]],
]) {
  test(`GG-14 per-call ${key} shallowly replaces model defaults`, async () => {
    for (const override of overrides) await fixture({}, async instrumentation => {
      const model = toolModel();
      const input = { contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] }], [key]: override };
      const snapshot = structuredClone(input);
      const before = model[key];
      await model.generateContent(structuredClone(input));
      instrumentation.manuallyInstrument(google);
      await model.generateContent(input);
      assert.deepEqual(input, snapshot);
      assert.equal(model[key], before);
      assert.deepEqual(bodies[1], bodies[0]);
      assertEffectiveInput(await exportedAttributes(), bodies[1]);
    });
  });
}

const toolHistory = { contents: [
  { role: 'user', parts: [{ text: 'INPUT_SENTINEL' }] },
  { role: 'model', parts: [{ text: 'TOOL_PLAN_SENTINEL' },
    { functionCall: { name: 'lookup', args: { q: 'ARG_SENTINEL', nested: { count: 0, ok: false } } } },
    { functionCall: { name: 'second', args: { list: [1, null, 'ARG_SECOND_SENTINEL'] } } }] },
  { role: 'function', parts: [
    { functionResponse: { name: 'lookup', response: { value: 'RESULT_SENTINEL', nested: [false, null, 0] } } },
    { functionResponse: { name: 'second', response: { value: 'RESULT_SECOND_SENTINEL' } } }] },
] };
const expectedHistory = [
  { role: 'user', content: 'INPUT_SENTINEL' },
  { role: 'model', content: [
    { type: 'text', text: 'TOOL_PLAN_SENTINEL' },
    ...toolHistory.contents[1].parts.slice(1).map(p => ({ type: 'functionCall', ...p.functionCall })),
  ] },
  { role: 'function', content: toolHistory.contents[2].parts.map(p => ({ type: 'functionResponse', ...p.functionResponse })) },
];

test('GG-15 function response Part arrays use the actual SDK conversation role', async () => {
  await fixture({}, async instrumentation => {
    const model = toolModel();
    const input = toolHistory.contents[2].parts;
    const nativeInput = structuredClone(input);
    const baseline = await model.generateContent(nativeInput);
    instrumentation.manuallyInstrument(google);
    const result = await model.generateContent(input);
    assert.deepEqual(input, nativeInput);
    assert.deepEqual(bodies[1], bodies[0]);
    assert.deepEqual(result.response.candidates, baseline.response.candidates);
    const attributes = await exportedAttributes();
    for (const key of ['input.value', 'raw.input']) assert.deepEqual(JSON.parse(attributes[key]).contents, bodies[1].contents);
    const role = sdkVersion === '0.1.0' ? 'user' : 'function';
    assert.equal(bodies[1].contents[0].role, role);
    assert.deepEqual(JSON.parse(attributes['gen_ai.input.messages']), [{ ...expectedHistory[2], role }]);
  });
});

test('GG-16 canonical input retains ordered tool calls, arguments and results', async () => {
  await fixture({}, async (instrumentation, model) => {
    const nativeInput = structuredClone(toolHistory);
    const baseline = await model.generateContent(nativeInput);
    instrumentation.manuallyInstrument(google);
    const result = await model.generateContent(toolHistory);
    assert.deepEqual(toolHistory, nativeInput);
    assert.deepEqual(bodies[1], bodies[0]);
    assert.deepEqual(result.response.candidates, baseline.response.candidates);
    const attributes = await exportedAttributes();
    assert.deepEqual(JSON.parse(attributes['gen_ai.input.messages']), expectedHistory);
    assert.deepEqual(JSON.parse(attributes['raw.input']).contents, bodies[1].contents);
  });
});

for (const option of ['hideInputs', 'hideInputText', 'hideInputMessages', 'hideInputImages',
  'hideOutputs', 'hideOutputText', 'hideOutputMessages']) for (const environment of [false, true]) {
  test(`GG-14/16/17 tool history and finish metadata honor directional ${option} (${environment ? 'environment' : 'config'})`, async () => {
    const key = 'FI_' + option.replace(/[A-Z]/g, c => '_' + c).toUpperCase();
    const previous = process.env[key];
    if (environment) process.env[key] = 'true';
    try {
      await fixture(environment ? {} : { [option]: true }, async instrumentation => {
        const model = toolModel();
        response = { ...answer, candidates: [
          { content: { role: 'model', parts: [{ text: 'OUTPUT_SENTINEL' },
            { functionCall: { name: 'outputTool', args: { q: 'OUTPUT_ARGS_SENTINEL' } } }] }, finishReason: 'MAX_TOKENS' },
          { finishReason: 'SAFETY' },
        ] };
        const input = structuredClone(toolHistory);
        const baseline = await model.generateContent(structuredClone(input));
        instrumentation.manuallyInstrument(google);
        const result = await model.generateContent(input);
        assert.deepEqual(bodies[1], bodies[0]);
        assert.deepEqual(result.response.candidates, baseline.response.candidates);
        assert.deepEqual(input, toolHistory);
        const attributes = await exportedAttributes();
        assert.equal(attributes['gen_ai.response.model'], 'gemini-resolved');
        assert.deepEqual(JSON.parse(attributes['gen_ai.response.finish_reasons']), ['MAX_TOKENS', 'SAFETY']);
        const payload = JSON.stringify(exportedPayloads);
        if (option.startsWith('hideInput')) {
          assert.doesNotMatch(payload, /INPUT_SENTINEL|TOOL_PLAN_SENTINEL|ARG_SENTINEL|ARG_SECOND_SENTINEL|RESULT_SENTINEL|RESULT_SECOND_SENTINEL|MODEL_TOOL_SENTINEL|MODEL_SCHEMA_SENTINEL|MODEL_CACHE_SENTINEL/);
          assert.match(payload, /OUTPUT_SENTINEL/);
        } else {
          assert.doesNotMatch(payload, /OUTPUT_SENTINEL|OUTPUT_ARGS_SENTINEL/);
          assert.equal(attributes['raw.output'], undefined);
          assert.equal(attributes['gen_ai.output.messages'], undefined);
          assert.deepEqual(JSON.parse(attributes['gen_ai.input.messages']), expectedHistory);
          assertEffectiveInput(attributes, bodies[1]);
        }
      });
    } finally {
      if (previous === undefined) delete process.env[key]; else process.env[key] = previous;
    }
  });
}

test('GG-17 blocked candidates without content still export finish metadata', async () => {
  await fixture({}, async (instrumentation, model) => {
    response = { ...answer, candidates: [{ finishReason: 'SAFETY' }] };
    const baseline = await model.generateContent('INPUT_SENTINEL');
    instrumentation.manuallyInstrument(google);
    const result = await model.generateContent('INPUT_SENTINEL');
    assert.deepEqual(result.response.candidates, baseline.response.candidates);
    assert.deepEqual(JSON.parse((await exportedAttributes())['gen_ai.response.finish_reasons']), ['SAFETY']);
  });
});

for (const option of ['hideInputs', 'hideInputText', 'hideInputMessages', 'hideInputImages']) {
  test(`GG-14/16 ${option} never traverses lazy model defaults or tool history`, async () => {
    await fixture({ [option]: true }, async instrumentation => {
      let reads = 0;
      const inaccessible = () => { reads++; throw new Error('hidden input getter visited'); };
      const input = { get contents() { return inaccessible(); } };
      const result = { response: answer };
      class Model {
        generateContent(value) { assert.equal(value, input); return Promise.resolve(result); }
      }
      for (const key of effectiveKeys) Object.defineProperty(Model.prototype, key, { get: inaccessible });
      instrumentation.manuallyInstrument({ GenerativeModel: Model });
      assert.equal(await new Model().generateContent(input), result);
      const attributes = await exportedAttributes();
      assert.equal(reads, 0);
      assert.equal(attributes['gen_ai.input.messages'], undefined);
      assert.equal(attributes['raw.input'], undefined);
      assert.equal(attributes['input.value'], undefined);
      assert.equal(attributes['gen_ai.tool.definitions'], undefined);
    });
  });
}

for (const option of ['hideOutputs', 'hideOutputText', 'hideOutputMessages']) {
  test(`GG-17 ${option} captures finish reasons without touching lazy output`, async () => {
    await fixture({ [option]: true }, async instrumentation => {
      let reads = 0;
      const inaccessible = () => { reads++; throw new Error('hidden output getter visited'); };
      const result = { response: { ...answer, candidates: [{ finishReason: 'STOP', get content() { return inaccessible(); } }],
        text: inaccessible, toJSON: inaccessible } };
      class Model { generateContent() { return Promise.resolve(result); } }
      instrumentation.manuallyInstrument({ GenerativeModel: Model });
      assert.equal(await new Model().generateContent('INPUT_SENTINEL'), result);
      const attributes = await exportedAttributes();
      assert.equal(reads, 0);
      assert.deepEqual(JSON.parse(attributes['gen_ai.response.finish_reasons']), ['STOP']);
      assert.equal(attributes['raw.output'], undefined);
      assert.equal(attributes['gen_ai.output.messages'], undefined);
      assert.equal(attributes['output.value'], undefined);
    });
  });
}
