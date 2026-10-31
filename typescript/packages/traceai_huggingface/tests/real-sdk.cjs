const assert = require('node:assert/strict');
const { test, after } = require('node:test');
const http = require('node:http');
const { context, trace } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { SemanticConventions: S } = require('@traceai/fi-semantic-conventions');
const contextManager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(contextManager);
after(() => context.disable());

const endpointUrl = 'http://localhost:9999';
const textInput = { model: 'offline-model', endpointUrl, inputs: 'private-prompt-sentinel' };
const chatInput = { model: 'offline-model', endpointUrl, messages: [{ role: 'user', content: 'private-prompt-sentinel' }] };
const chatOutput = { id: 'offline-response', created: 1, system_fingerprint: 'offline', model: 'offline-model', choices: [{ index: 0, message: { role: 'assistant', content: 'private-answer-sentinel' }, finish_reason: 'stop' }], usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 } };
const json = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
const sse = chunks => new Response(chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join(''), { headers: { 'content-type': 'text/event-stream' } });
const chatChunks = [
  { id: 'offline-response', model: 'offline-model', choices: [{ index: 0, delta: { content: 'private-' } }] },
  { choices: [{ index: 0, delta: { content: 'answer-sentinel' }, finish_reason: 'stop' }], usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 } },
];
const textChunks = [
  { token: { id: 1, text: 'private-', logprob: 0, special: false }, generated_text: null },
  { token: { id: 2, text: 'answer-sentinel', logprob: 0, special: false }, generated_text: 'private-answer-sentinel' },
];

async function fixture(Instrumentation, run, traceConfig, extraExporter) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), ...(extraExporter ? [new SimpleSpanProcessor(extraExporter)] : [])] });
  const instrumentation = new Instrumentation({ traceConfig });
  instrumentation.setTracerProvider(provider); // Must refresh FITracer without another enable().
  const originalFetch = global.fetch;
  try {
    await run({ instrumentation, exporter, provider });
  } finally {
    global.fetch = originalFetch;
    instrumentation.disable();
    await provider.shutdown();
  }
}

for (const mode of ['cjs', 'esm']) {
  for (const sdkName of ['@huggingface/inference-floor', '@huggingface/inference', '@huggingface/inference-current']) {
    const current = sdkName.endsWith('-current');
    test(`${mode} ${sdkName}: real SDK contracts`, async t => {
      const { HuggingFaceInstrumentation } = mode === 'cjs' ? require('../dist/cjs') : await import('../dist/esm/index.js');
      const sdk = mode === 'cjs' ? require(sdkName) : await import(sdkName);
      const textResponse = () => json(current ? { choices: [{ text: 'private-answer-sentinel' }] } : [{ generated_text: 'private-answer-sentinel' }]);
      const constructorNames = ['InferenceClient', 'HfInference', 'InferenceClientEndpoint', 'HfInferenceEndpoint'].filter(name => typeof sdk[name] === 'function');

      for (const name of constructorNames) {
        await t.test(`${name}, immutable own methods and derived endpoints`, async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
          let calls = 0;
          const fetch = async () => { calls++; return textResponse(); };
          global.fetch = fetch;
          const wrapped = instrumentation.manuallyInstrument(sdk);
          assert.equal(instrumentation.manuallyInstrument(sdk), wrapped);
          assert.equal(instrumentation.manuallyInstrument(wrapped), wrapped);
          const client = name === 'HfInferenceEndpoint'
            ? new wrapped[name](endpointUrl, '', { fetch })
            : new wrapped[name]('', { fetch, endpointUrl });
          assert.ok(client instanceof sdk[name]);
          assert.equal(client.textGeneration, client.textGeneration);
          assert.equal(instrumentation.instrumentClient(client), client);
          await client.textGeneration(textInput);
          if (client.endpoint) await client.endpoint(endpointUrl).textGeneration(textInput);
          assert.equal(exporter.getFinishedSpans().length, calls);
          assert.ok(calls >= 1);
          for (const span of exporter.getFinishedSpans()) {
            assert.equal(span.name, 'HuggingFace Text Generation');
            assert.equal(span.attributes[S.OUTPUT_VALUE], 'private-answer-sentinel');
          }
        }));
      }

      await t.test('non-chat tasks keep their results and span kinds', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
        const wrapped = instrumentation.manuallyInstrument(sdk);
        const Client = wrapped.InferenceClient || wrapped.HfInference;
        let value;
        const fetch = async () => json(value);
        global.fetch = fetch;
        const client = new Client('', { fetch });
        for (const [method, response, input, kind] of [
          ['featureExtraction', [0.25, 0.75], textInput, 'EMBEDDING'],
          ['summarization', [{ summary_text: 'summary' }], textInput, 'LLM'],
          ['translation', [{ translation_text: 'translation' }], textInput, 'LLM'],
          ['questionAnswering', { answer: 'answer', score: 0.9, start: 0, end: 6 }, { ...textInput, inputs: { question: 'question', context: 'context' } }, 'LLM'],
        ]) {
          value = response;
          const result = await client[method](input);
          assert.deepEqual(result, method === 'summarization' || method === 'translation' ? response[0] : response);
          const span = exporter.getFinishedSpans().at(-1);
          assert.equal(span.attributes[S.FI_SPAN_KIND], kind);
        }
        assert.equal(exporter.getFinishedSpans().length, 4);
      }));

      await t.test('stream is immediately iterable, ends once, and supports early return', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
        const wrapped = instrumentation.manuallyInstrument(sdk);
        const Client = wrapped.InferenceClient || wrapped.HfInference;
        const fetch = async () => sse(textChunks);
        global.fetch = fetch;
        const client = new Client('', { fetch });
        const stream = client.textGenerationStream(textInput);
        assert.equal(typeof stream[Symbol.asyncIterator], 'function');
        assert.equal(stream.then, undefined);
        assert.equal(exporter.getFinishedSpans().length, 0);
        const chunks = [];
        for await (const chunk of stream) chunks.push(chunk);
        assert.deepEqual(chunks, textChunks);
        assert.equal(exporter.getFinishedSpans().length, 1);
        assert.equal(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE], 'private-answer-sentinel');
        for await (const _chunk of client.textGenerationStream(textInput)) break;
        assert.equal(exporter.getFinishedSpans().length, 2);
        assert.equal(exporter.getFinishedSpans()[1].attributes[S.OUTPUT_VALUE], 'private-');
        await client.textGenerationStream(textInput).return();
        assert.equal(exporter.getFinishedSpans().length, 2, 'unstarted stream must not leak a span');
      }));

      if (current) {
        await t.test('text streams also preserve completion-style provider chunks', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
          const chunks = [{ choices: [{ text: 'first' }] }, { choices: [{ text: ' second' }] }];
          const wrapped = instrumentation.manuallyInstrument(sdk);
          const client = new wrapped.InferenceClient('', { fetch: async () => sse(chunks) });
          const received = [];
          for await (const chunk of client.textGenerationStream(textInput)) received.push(chunk);
          assert.deepEqual(received, chunks);
          assert.equal(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE], 'first second');
        }));
      }

      if (sdkName !== '@huggingface/inference-floor') {
        await t.test('chat, chat stream, usage and endpoint clients', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
          let streaming = false;
          const fetch = async () => streaming ? sse(chatChunks) : json(chatOutput);
          global.fetch = fetch;
          const wrapped = instrumentation.manuallyInstrument(sdk);
          const Client = wrapped.InferenceClient || wrapped.HfInference;
          const client = new Client('', { fetch }).endpoint(endpointUrl);
          assert.deepEqual(await client.chatCompletion(chatInput), chatOutput);
          streaming = true;
          const chunks = [];
          for await (const chunk of client.chatCompletionStream(chatInput)) chunks.push(chunk);
          assert.deepEqual(chunks, chatChunks);
          const spans = exporter.getFinishedSpans();
          assert.equal(spans.length, 2);
          for (const span of spans) {
            assert.equal(span.attributes[S.LLM_TOKEN_COUNT_TOTAL], 5);
            assert.equal(span.attributes[S.GEN_AI_RESPONSE_MODEL], 'offline-model');
          }
          assert.equal(spans[1].attributes[S.OUTPUT_VALUE], 'private-answer-sentinel');
        }));
      }

      await t.test('parent context, suppression, per-request transport and enable/disable', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter, provider }) => {
        const wrapped = instrumentation.manuallyInstrument(sdk);
        const Client = wrapped.InferenceClient || wrapped.HfInference;
        const client = new Client('', { fetch: async () => { throw Error('per-request fetch was lost'); } });
        let activeSpan;
        const fetch = async () => { activeSpan = trace.getSpan(context.active()); return textResponse(); };
        global.fetch = fetch;
        const parent = provider.getTracer('test').startSpan('parent');
        await context.with(trace.setSpan(context.active(), parent), () => client.textGeneration(textInput, { fetch }));
        const child = exporter.getFinishedSpans()[0];
        assert.equal(child.parentSpanContext.spanId, parent.spanContext().spanId);
        assert.equal(activeSpan.spanContext().spanId, child.spanContext().spanId);
        await context.with(suppressTracing(context.active()), () => client.textGeneration(textInput, { fetch }));
        assert.equal(exporter.getFinishedSpans().length, 1);
        instrumentation.disable();
        await client.textGeneration(textInput, { fetch });
        assert.equal(exporter.getFinishedSpans().length, 1);
        instrumentation.enable();
        await client.textGeneration(textInput, { fetch });
        assert.equal(exporter.getFinishedSpans().length, 2);
        const rewrapped = instrumentation.manuallyInstrument(wrapped);
        await rewrapped.textGeneration(textInput, { fetch });
        assert.equal(exporter.getFinishedSpans().length, 3, 're-wrapping a prior facade after disable must not duplicate spans');
        parent.end();
      }));

      await t.test('transport and stream failures preserve the original error', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
        const failure = new Error('synthetic transport failure');
        const fetch = async () => { throw failure; };
        global.fetch = fetch;
        const wrapped = instrumentation.manuallyInstrument(sdk);
        const Client = wrapped.InferenceClient || wrapped.HfInference;
        const client = new Client('', { fetch });
        await assert.rejects(client.textGeneration(textInput), error => error === failure);
        await assert.rejects(async () => { for await (const _chunk of client.textGenerationStream(textInput)) {} }, error => error === failure);
        assert.equal(exporter.getFinishedSpans().length, 2);
        for (const span of exporter.getFinishedSpans()) assert.equal(span.status.code, 2);
      }));

      await t.test('standalone task functions and pre-existing clients', async () => fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
        const Original = sdk.InferenceClient || sdk.HfInference;
        const fetch = async () => textResponse();
        global.fetch = fetch;
        const original = new Original('synthetic-key', { fetch });
        const descriptor = Object.getOwnPropertyDescriptor(original, 'textGeneration');
        assert.equal(descriptor.configurable, false);
        assert.equal(descriptor.writable, false);
        const client = instrumentation.instrumentClient(original);
        assert.ok(client instanceof Original);
        await client.textGeneration(textInput);
        const module = instrumentation.manuallyInstrument(sdk);
        await module.textGeneration({ ...textInput, accessToken: 'synthetic-key' }, { fetch });
        assert.equal(exporter.getFinishedSpans().length, 2);
        const attrs = exporter.getFinishedSpans().map(span => span.attributes);
        assert.ok(!JSON.stringify(attrs).includes('synthetic-key'));
        assert.equal(Object.getOwnPropertyDescriptor(original, 'textGeneration').value, descriptor.value);
      }));
    });
  }
}

test('privacy survives constructor setup, provider replacement and OTLP HTTP export', async () => {
  const bodies = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => { bodies.push(Buffer.concat(chunks).toString()); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}'); });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const otlp = new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` });
    const { HuggingFaceInstrumentation } = require('../dist/cjs');
    await fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter, provider }) => {
      const sdk = instrumentation.manuallyInstrument(require('@huggingface/inference-current'));
      let value;
      let streaming = false;
      const fetch = async () => streaming ? sse(textChunks) : json(value);
      const client = new sdk.InferenceClient('synthetic-key', { fetch });
      value = { choices: [{ text: 'private-answer-sentinel' }] };
      await client.textGeneration(textInput);
      value = [0.25, 0.75];
      await client.featureExtraction(textInput);
      value = chatOutput;
      await client.chatCompletion(chatInput);
      streaming = true;
      for await (const _chunk of client.textGenerationStream(textInput)) {}
      await provider.forceFlush();
      assert.equal(exporter.getFinishedSpans().length, 4);
      assert.equal(bodies.length, 4);
      const wire = bodies.join('');
      for (const secret of ['private-prompt-sentinel', 'private-answer-sentinel', 'synthetic-key']) assert.ok(!wire.includes(secret), `${secret} leaked on OTLP wire`);
      assert.ok(wire.includes('HuggingFace Text Generation'));
      assert.ok(wire.includes('REDACTED'));
      for (const span of exporter.getFinishedSpans()) {
        assert.equal(span.attributes[S.INPUT_VALUE], '__REDACTED__');
        assert.equal(span.attributes[S.OUTPUT_VALUE], '__REDACTED__');
        assert.equal(span.attributes[`${S.LLM_PROMPTS}.0`], undefined);
        assert.equal(span.attributes[`${S.EMBEDDING_EMBEDDINGS}.0.${S.EMBEDDING_TEXT}`], undefined);
        assert.equal(span.attributes[`${S.EMBEDDING_EMBEDDINGS}.0.${S.EMBEDDING_VECTOR}`], undefined);
      }
    }, { hideInputs: true, hideOutputs: true }, otlp);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('mutable CommonJS exports keep the old ignored-return entry point', async () => {
  const { HuggingFaceInstrumentation } = require('../dist/cjs');
  const sdk = require('@huggingface/inference-current');
  const Original = sdk.InferenceClient;
  await fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
    instrumentation.manuallyInstrument(sdk);
    const client = new sdk.InferenceClient('', { fetch: async () => json(chatOutput) });
    assert.deepEqual(await client.chatCompletion(chatInput), chatOutput);
    assert.equal(exporter.getFinishedSpans().length, 1);
    instrumentation.disable();
    assert.equal(sdk.InferenceClient, Original);
  });
});

test('subclasses, frozen clients, iterator closure and call arguments survive wrapping', async () => {
  const { HuggingFaceInstrumentation } = require('../dist/cjs');
  await fixture(HuggingFaceInstrumentation, async ({ instrumentation, exporter }) => {
    const options = { signal: new AbortController().signal };
    let closed = false;
    let seen;
    class Client {
      static marker = 'static';
      #marker = 'private';
      privateMethod() { return this.#marker; }
      constructor() {
        Object.defineProperty(this, 'textGeneration', { value: async (...args) => { seen = args; return { generated_text: 'ok' }; } });
        Object.defineProperty(this, 'textGenerationStream', { value: async function* () { try { yield textChunks[0]; yield textChunks[1]; } finally { closed = true; } } });
      }
    }
    const module = instrumentation.manuallyInstrument(Object.freeze({ InferenceClient: Client }));
    class Subclass extends module.InferenceClient {}
    const client = new Subclass();
    assert.ok(client instanceof Subclass);
    assert.ok(client instanceof Client);
    assert.equal(Subclass.marker, 'static');
    assert.equal(client.privateMethod(), 'private');
    await client.textGeneration(textInput, options);
    assert.equal(seen[0], textInput);
    assert.equal(seen[1], options);
    for await (const _chunk of client.textGenerationStream(textInput)) break;
    assert.equal(closed, true);
    const frozen = instrumentation.instrumentClient(Object.freeze(new Client()));
    await frozen.textGeneration(textInput, options);
    assert.equal(exporter.getFinishedSpans().length, 3);
  });
});
