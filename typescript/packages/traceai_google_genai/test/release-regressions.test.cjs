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
const memory = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
const nativeFetch = global.fetch;
const answer = {
  candidates: [{ content: { role: 'model', parts: [{ text: 'OUTPUT_SENTINEL' }] }, finishReason: 'STOP' }],
  usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 3, totalTokenCount: 8 },
};
let server, response, bodies, providerFailure;
before(async () => {
  provider.register();
  server = http.createServer(async (request, res) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
    res.setHeader('content-type', 'application/json');
    if (providerFailure) {
      res.statusCode = 400;
      return res.end(JSON.stringify({ error: { message: 'synthetic failure', code: 400, status: 'INVALID_ARGUMENT' } }));
    }
    res.end(JSON.stringify(request.url.includes('batchEmbedContents') ? { embeddings: [{ values: [0.2] }] }
      : request.url.includes('embedContent') ? { embedding: { values: [0.2] } } : response));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  global.fetch = (url, options) => nativeFetch(`http://127.0.0.1:${server.address().port}${new URL(url).pathname}`, options);
});
after(async () => {
  global.fetch = nativeFetch;
  await provider.shutdown();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
});
async function fixture(traceConfig, callback) {
  memory.reset(); bodies = []; response = answer; providerFailure = false;
  const instrumentation = new GoogleGenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider);
  const model = new google.GoogleGenerativeAI('SYNTHETIC_GOOGLE_KEY').getGenerativeModel({ model: 'gemini-test' });
  try { await callback(instrumentation, model); }
  finally { instrumentation.disable(); }
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
