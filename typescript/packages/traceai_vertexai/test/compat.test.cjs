const { test, beforeEach, afterEach, after } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const path = require('node:path');
const local = createRequire(process.env.VERTEX_INSTRUMENTATION ? path.resolve(process.env.VERTEX_INSTRUMENTATION, 'package.json') : path.resolve(__dirname, '../package.json'));
const consumer = process.env.VERTEX_CONSUMER ? createRequire(path.resolve(process.env.VERTEX_CONSUMER, 'package.json')) : local;
const { VertexAIInstrumentation } = local('@traceai/fi-instrumentation-vertexai');
const { context, trace } = local('@opentelemetry/api');
const core = createRequire(local.resolve('@traceai/fi-core'));
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = core('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = core('@opentelemetry/context-async-hooks');
const { suppressTracing } = core('@opentelemetry/core');
const { SemanticConventions: S } = local('@traceai/fi-semantic-conventions');
const sdk = consumer('@google-cloud/vertexai');
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation, requests, reply, failure, streamBody;
const originalFetch = global.fetch;
const response = (text = 'hello') => ({ candidates: [{ index: 0, content: { role: 'model', parts: [{ text }] }, finishReason: 'STOP' }], usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 0, totalTokenCount: 3 } });
function model(preview = false) {
  const vertex = new sdk.VertexAI({ project: 'synthetic-project', location: 'us-central1', googleAuthOptions: { authClient: { getAccessToken: async () => ({ token: 'synthetic-only' }) } } });
  return (preview ? vertex.preview : vertex).getGenerativeModel({ model: 'gemini-test', generationConfig: { temperature: 0.2 } });
}
function instrument(config = {}) {
  instrumentation = new VertexAIInstrumentation(config);
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  return instrumentation;
}
const settle = () => new Promise(resolve => setImmediate(resolve));
beforeEach(() => {
  exporter.reset(); requests = []; reply = response(); failure = false; streamBody = undefined;
  global.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    if (failure) return new Response(JSON.stringify({ error: { message: 'secret-provider-error' } }), { status: 400 });
    if (String(url).includes('countTokens')) return new Response(JSON.stringify({ totalTokens: 0 }));
    if (String(url).includes('streamGenerateContent')) {
      return new Response(streamBody ?? `data: ${JSON.stringify(reply)}\n\n`, { headers: { 'content-type': 'text/event-stream' } });
    }
    return new Response(JSON.stringify(reply));
  };
});
afterEach(() => { instrumentation?.disable(); instrumentation = undefined; global.fetch = originalFetch; });
after(async () => { await provider.shutdown(); context.disable(); });

for (const preview of [false, true]) {
  test(`${preview ? 'preview' : 'stable'} generation emits exactly one parented span and preserves zero usage`, async () => {
    instrument();
    const parent = provider.getTracer('test').startSpan('parent');
    const result = await context.with(trace.setSpan(context.active(), parent), () => model(preview).generateContent('question'));
    assert.equal(result.response.candidates[0].content.parts[0].text, 'hello');
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 1);
    assert.equal(spans[0].parentSpanContext.spanId, parent.spanContext().spanId);
    assert.equal(spans[0].attributes[S.LLM_TOKEN_COUNT_COMPLETION], 0);
    assert.equal(spans[0].attributes[S.LLM_MODEL_NAME], 'gemini-test');
    assert.match(spans[0].attributes[S.LLM_INPUT_MESSAGES], /question/);
    parent.end();
  });
  test(`${preview ? 'preview' : 'stable'} chat uses actual resource model and captures multi-turn history`, async () => {
    instrument();
    const chat = model(preview).startChat();
    await chat.sendMessage('first question');
    await chat.sendMessage('second question');
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 2);
    assert.equal(spans[1].attributes[S.LLM_MODEL_NAME], 'gemini-test');
    assert.match(spans[1].attributes[S.LLM_INPUT_MESSAGES], /first question/);
    assert.match(spans[1].attributes[S.LLM_INPUT_MESSAGES], /second question/);
    assert.equal((await chat.getHistory()).length, 4);
    assert.equal(requests.length, 2);
  });
  test(`${preview ? 'preview' : 'stable'} aggregate-only stream ends once without consuming caller iterator`, async () => {
    instrument();
    const result = await model(preview).generateContentStream('question');
    await result.response;
    await settle();
    assert.equal(exporter.getFinishedSpans().length, 1);
    assert.match(exporter.getFinishedSpans()[0].attributes[S.LLM_OUTPUT_MESSAGES], /hello/);
    const chunks = [];
    for await (const chunk of result.stream) chunks.push(chunk);
    assert.equal(chunks.length, 1);
    assert.equal(exporter.getFinishedSpans().length, 1);
  });
}

test('constructor privacy config applies without a second enable call', async () => {
  instrument({ traceConfig: { hideInputs: true, hideOutputs: true } });
  await model().generateContent('private-question');
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  assert.doesNotMatch(JSON.stringify(spans.map(({ attributes, status, events }) => ({ attributes, status, events }))), /private-question|hello/);
});

test('manual instrumentation disables and re-enables without duplicate spans', async () => {
  instrument();
  const m = model();
  await m.generateContent('one');
  instrumentation.disable();
  await m.generateContent('two');
  assert.equal(exporter.getFinishedSpans().length, 1);
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
  await m.generateContent('three');
  assert.equal(exporter.getFinishedSpans().length, 2);
});

test('suppressed calls are not traced', async () => {
  instrument();
  await context.with(suppressTracing(context.active()), () => model().generateContent('question'));
  assert.equal(exporter.getFinishedSpans().length, 0);
});

test('all candidates and tool calls are captured; provider error messages stay out of spans', async () => {
  instrument();
  reply.candidates.push({ index: 1, content: { role: 'model', parts: [{ functionCall: { name: 'weather', args: { city: 'Paris' } } }] }, finishReason: 'STOP' });
  await model().generateContent('question');
  assert.match(exporter.getFinishedSpans()[0].attributes[S.LLM_OUTPUT_MESSAGES], /weather/);
  failure = true;
  await assert.rejects(model().generateContent('question'));
  assert.equal(exporter.getFinishedSpans()[1].status.code, 2);
  assert.doesNotMatch(JSON.stringify(exporter.getFinishedSpans().map(({ attributes, status, events }) => ({ attributes, status, events }))), /secret-provider-error/);
});

test('raw binary media and URIs are never exported', async () => {
  instrument();
  await model().generateContent({ contents: [{ role: 'user', parts: [{ text: 'question' }, { inlineData: { mimeType: 'image/png', data: 'BINARY_SENTINEL' } }, { fileData: { mimeType: 'image/png', fileUri: 'gs://URI_SENTINEL' } }] }] });
  assert.equal(requests[0].body.contents[0].parts[1].inlineData.data, 'BINARY_SENTINEL');
  assert.doesNotMatch(JSON.stringify(exporter.getFinishedSpans().map(({ attributes, status, events }) => ({ attributes, status, events }))), /BINARY_SENTINEL|URI_SENTINEL/);
});

test('zero countTokens result is preserved', async () => {
  instrument();
  assert.equal((await model().countTokens({ contents: [{ role: 'user', parts: [{ text: 'question' }] }] })).totalTokens, 0);
  assert.equal(exporter.getFinishedSpans()[0].attributes['vertexai.total_tokens'], 0);
});

for (const flag of ['hideInputs', 'hideInputMessages', 'hideInputText', 'hideOutputs', 'hideOutputMessages', 'hideOutputText']) {
  test(`${flag} hides content including tool arguments without hiding model/usage`, async () => {
    instrument({ traceConfig: { [flag]: true } });
    reply = response('OUTPUT_SENTINEL');
    reply.candidates[0].content.parts.push({ functionCall: { name: 'outputTool', args: { secret: 'OUTPUT_SENTINEL' } } });
    await model().generateContent({ contents: [{ role: 'user', parts: [{ text: 'INPUT_SENTINEL' }, { functionResponse: { name: 'inputTool', response: { secret: 'INPUT_SENTINEL' } } }] }], tools: [{ functionDeclarations: [{ name: 'inputTool', description: 'INPUT_SENTINEL' }] }] });
    const span = exporter.getFinishedSpans()[0];
    assert.equal(span.attributes[S.LLM_MODEL_NAME], 'gemini-test');
    assert.equal(span.attributes[S.LLM_TOKEN_COUNT_PROMPT], 3);
    assert.doesNotMatch(JSON.stringify(span.attributes), flag.startsWith('hideInput') ? /INPUT_SENTINEL/ : /OUTPUT_SENTINEL/);
  });
}

test('environment privacy controls apply to automatically initialized FITracer', async () => {
  process.env.FI_HIDE_INPUTS = 'true'; process.env.FI_HIDE_OUTPUTS = 'true';
  try {
    instrument();
    await model().generateContent('INPUT_SENTINEL');
    const span = exporter.getFinishedSpans()[0];
    assert.equal(span.attributes[S.LLM_MODEL_NAME], 'gemini-test');
    assert.doesNotMatch(JSON.stringify(span.attributes), /INPUT_SENTINEL|hello/);
  } finally { delete process.env.FI_HIDE_INPUTS; delete process.env.FI_HIDE_OUTPUTS; }
});

test('stream aggregate preserves repeated parts and final usage without double counting', async () => {
  instrument();
  streamBody = [response('same'), response('same')].map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('');
  const result = await model().generateContentStream('question');
  const chunks = [];
  for await (const chunk of result.stream) chunks.push(chunk);
  const aggregate = await result.response;
  await settle();
  assert.equal(chunks.length, 2);
  assert.equal(exporter.getFinishedSpans().length, 1);
  const span = exporter.getFinishedSpans()[0];
  assert.equal(JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES])[0].content.map(part => part.text).join(''), 'samesame');
  assert.equal(span.attributes[S.LLM_TOKEN_COUNT_PROMPT], aggregate.usageMetadata.promptTokenCount);
});

test('early iterator exit retains SDK-owned aggregate and chat history', async () => {
  instrument();
  const chat = model().startChat();
  const result = await chat.sendMessageStream('question');
  for await (const chunk of result.stream) { assert.ok(chunk.candidates); break; }
  await result.response;
  await settle();
  assert.equal(exporter.getFinishedSpans().length, 1);
  assert.equal((await chat.getHistory()).length, 2);
  assert.equal(requests.length, 1);
});

test('malformed SSE keeps aggregate rejection and emits one error span', async () => {
  instrument();
  streamBody = 'data: not-json\n\n';
  const result = await model().generateContentStream('question');
  await assert.rejects(result.response);
  await assert.rejects(async () => { for await (const chunk of result.stream) void chunk; });
  await settle();
  assert.equal(exporter.getFinishedSpans().length, 1);
  assert.equal(exporter.getFinishedSpans()[0].status.code, 2);
});

test('empty SSE retains the SDK aggregate rejection and completes exactly once', async () => {
  instrument(); streamBody = '';
  const result = await model().generateContentStream('question');
  await assert.rejects(result.response, /final response in stream chunk is undefined/);
  await settle();
  assert.equal(exporter.getFinishedSpans().length, 1);
  assert.equal(exporter.getFinishedSpans()[0].status.code, 2);
});

test('an active stream finishes after disabling while new calls remain untraced', async () => {
  instrument();
  let release;
  streamBody = new ReadableStream({ start(controller) { release = () => { controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(reply)}\n\n`)); controller.close(); }; } });
  const result = await model().generateContentStream('question');
  instrumentation.disable(); release();
  await result.response; await settle();
  await model().generateContent('untraced');
  assert.equal(exporter.getFinishedSpans().length, 1);
});

test('changing the provider updates FITracer without re-enabling', async () => {
  instrument();
  const secondExporter = new InMemorySpanExporter();
  const secondProvider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(secondExporter)] });
  try {
    await model().generateContent('one');
    instrumentation.setTracerProvider(secondProvider);
    await model().generateContent('two');
    assert.equal(exporter.getFinishedSpans().length, 1);
    assert.equal(secondExporter.getFinishedSpans().length, 1);
  } finally { await secondProvider.shutdown(); }
});

test('function-result chat messages retain the SDK role', async () => {
  instrument();
  const chat = model().startChat();
  await chat.sendMessage([{ functionResponse: { name: 'weather', response: { city: 'Paris' } } }]);
  const messages = JSON.parse(exporter.getFinishedSpans()[0].attributes[S.LLM_INPUT_MESSAGES]);
  assert.equal(messages.at(-1).role, requests[0].body.contents.at(-1).role);
});

test('bounded history keeps the latest request when tools add a prefix message', async () => {
  instrument();
  const contents = Array.from({ length: 39 }, (_, i) => ({ role: i % 2 ? 'model' : 'user', parts: [{ text: 'history' }] }));
  contents.push({ role: 'user', parts: [{ text: 'LATEST_REQUEST' }] });
  await model().generateContent({ contents, tools: [{ functionDeclarations: [{ name: 'weather' }] }] });
  assert.match(exporter.getFinishedSpans()[0].attributes[S.LLM_INPUT_MESSAGES], /LATEST_REQUEST/);
});

test('list truncation is marked independently from the text budget', async () => {
  instrument();
  reply.candidates[0].content.parts = Array.from({ length: 40 }, () => ({ text: 'x' }));
  await model().generateContent('question');
  assert.equal(exporter.getFinishedSpans()[0].attributes['vertexai.capture.truncated'], true);
});

test('bounded capture marks truncation while leaving the SDK response intact', async () => {
  instrument(); reply = response('x'.repeat(200000));
  const result = await model().generateContent('q'.repeat(200000));
  assert.equal(result.response.candidates[0].content.parts[0].text.length, 200000);
  const span = exporter.getFinishedSpans()[0];
  assert.equal(span.attributes['vertexai.capture.truncated'], true);
  assert.ok(span.attributes[S.LLM_OUTPUT_MESSAGES].length < 10000);
  assert.ok(span.attributes[S.LLM_INPUT_MESSAGES].length < 10000);
});

for (const preview of [false, true]) {
  for (const method of ['sendMessage', 'sendMessageStream']) {
    test(`${preview ? 'preview' : 'stable'} ${method} captures strings in request arrays without mutating them`, async () => {
      instrument();
      const request = ['ARRAY_STRING_SENTINEL', { text: 'part-object' }];
      const result = await model(preview).startChat()[method](request);
      await result.response; await settle();
      assert.deepEqual(request, ['ARRAY_STRING_SENTINEL', { text: 'part-object' }]);
      assert.equal(requests[0].body.contents.at(-1).parts[0].text, request[0]);
      const messages = JSON.parse(exporter.getFinishedSpans()[0].attributes[S.LLM_INPUT_MESSAGES]);
      assert.deepEqual(messages.at(-1).content, request.map(part => ({ type: 'text', text: typeof part === 'string' ? part : part.text })));
    });

    test(`${preview ? 'preview' : 'stable'} ${method} budgets the current turn before recent history`, async () => {
      instrument();
      const history = Array.from({ length: 10 }, (_, i) => ({ role: i % 2 ? 'model' : 'user', parts: [{ text: `history-${i} ${'h'.repeat(4000)}` }] }));
      const result = await model(preview).startChat({ history })[method]('CURRENT_QUESTION_SENTINEL');
      await result.response; await settle();
      assert.equal(requests[0].body.contents.at(-1).parts[0].text, 'CURRENT_QUESTION_SENTINEL');
      const span = exporter.getFinishedSpans()[0];
      const messages = JSON.parse(span.attributes[S.LLM_INPUT_MESSAGES]);
      assert.deepEqual(messages.at(-1), { role: 'user', content: [{ type: 'text', text: 'CURRENT_QUESTION_SENTINEL' }] });
      assert.match(JSON.stringify(messages.at(-2)), /history-9/);
      assert.equal(span.attributes['vertexai.capture.truncated'], true);
      assert.ok(span.attributes[S.LLM_INPUT_MESSAGES].length < 26000);
      assert.ok(messages.every(message => typeof message.role === 'string' && Array.isArray(message.content)));
      const indices = messages.slice(0, -1).map(message => Number(message.content[0].text.match(/history-(\d+)/)[1]));
      assert.deepEqual(indices, [...indices].sort((a, b) => a - b));
    });
  }

  for (const method of ['generateContent', 'generateContentStream']) {
    test(`${preview ? 'preview' : 'stable'} ${method} carries scoped FI context and OTel parent`, async () => {
      instrument();
      const fi = local('@traceai/fi-core');
      const parent = provider.getTracer('test').startSpan('parent');
      const ctx = fi.setUser(fi.setSession(trace.setSpan(context.active(), parent), { sessionId: 'synthetic-session' }), { userId: 'synthetic-user' });
      try {
        const result = await context.with(ctx, () => model(preview)[method]('question'));
        await result.response; await settle();
        const span = exporter.getFinishedSpans()[0];
        assert.equal(span.attributes['session.id'], 'synthetic-session');
        assert.equal(span.attributes['user.id'], 'synthetic-user');
        assert.equal(span.parentSpanContext.spanId, parent.spanContext().spanId);
        await model(preview).generateContent('unscoped');
        assert.equal(exporter.getFinishedSpans()[1].attributes['session.id'], undefined);
        assert.equal(exporter.getFinishedSpans()[1].attributes['user.id'], undefined);
      } finally { parent.end(); }
    });
  }
}

for (const method of ['generateContent', 'generateContentStream']) {
  test(`${method} preserves output message and part structure when the budget runs out`, async () => {
    instrument();
    reply.candidates = Array.from({ length: 8 }, (_, index) => ({ index, content: { role: 'model', parts: [{ text: 'r'.repeat(4000) }] }, finishReason: 'STOP' }));
    const result = await model()[method]('question');
    const aggregate = await result.response; await settle();
    assert.equal(aggregate.candidates.length, 8);
    assert.equal(aggregate.candidates[7].content.parts[0].text.length, 4000);
    const span = exporter.getFinishedSpans()[0];
    const messages = JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES]);
    assert.ok(messages.length > 0);
    for (const message of messages) {
      assert.equal(message.role, 'model');
      assert.ok(Array.isArray(message.content));
      for (const part of message.content) {
        assert.equal(part.type, 'text');
        assert.equal(typeof part.text, 'string');
      }
    }
    assert.equal(span.attributes['vertexai.capture.truncated'], true);
    assert.ok(span.attributes[S.LLM_OUTPUT_MESSAGES].length < 26000);
  });
}

test('large instructions and tool declarations cannot spend the current request budget', async () => {
  instrument();
  await model().generateContent({
    systemInstruction: { parts: Array.from({ length: 10 }, () => ({ text: 's'.repeat(4000) })) },
    tools: [{ functionDeclarations: Array.from({ length: 10 }, (_, i) => ({ name: `tool_${i}`, description: 'd'.repeat(4000) })) }],
    contents: [{ role: 'user', parts: [{ text: 'CURRENT_QUESTION_SENTINEL' }] }],
  });
  const span = exporter.getFinishedSpans()[0];
  const messages = JSON.parse(span.attributes[S.LLM_INPUT_MESSAGES]);
  assert.deepEqual(messages.at(-1), { role: 'user', content: [{ type: 'text', text: 'CURRENT_QUESTION_SENTINEL' }] });
  assert.ok(messages.every(message => typeof message.role === 'string' && Array.isArray(message.content)));
  assert.equal(span.attributes['vertexai.capture.truncated'], true);
});

test('budget exhaustion within a message preserves part envelopes and does not modify responses', async () => {
  instrument();
  reply.candidates[0].content.parts = Array.from({ length: 20 }, (_, i) => i % 2
    ? { functionCall: { name: `tool_${i}`, args: { payload: 'a'.repeat(4000) } } }
    : { text: 't'.repeat(4000) });
  const result = await model().generateContent('question');
  const span = exporter.getFinishedSpans()[0];
  const messages = JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES]);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].role, 'model');
  for (const part of messages[0].content) {
    assert.ok(part.type === 'text' || part.type === 'tool_call');
    if (part.type === 'text') assert.equal(typeof part.text, 'string');
    else { assert.equal(typeof part.name, 'string'); assert.ok(Object.hasOwn(part, 'arguments')); }
  }
  assert.equal(result.response.candidates[0].content.parts.length, 20);
  assert.equal(span.attributes['vertexai.capture.truncated'], true);
  assert.ok(span.attributes[S.LLM_OUTPUT_MESSAGES].length < 26000);
});

test('FI context attributes are privacy-masked and cannot override observed model/provider', async () => {
  instrument({ traceConfig: { hideInputs: true } });
  const fi = local('@traceai/fi-core');
  const ctx = fi.setAttributes(fi.setSession(context.active(), { sessionId: 'synthetic-session' }), {
    [S.INPUT_VALUE]: 'CONTEXT_SECRET', [S.LLM_PROVIDER]: 'incorrect', [S.LLM_MODEL_NAME]: 'incorrect',
  });
  await context.with(ctx, () => model().generateContent('question'));
  const span = exporter.getFinishedSpans()[0];
  assert.equal(span.attributes['session.id'], 'synthetic-session');
  assert.equal(span.attributes[S.LLM_PROVIDER], 'google');
  assert.equal(span.attributes[S.LLM_MODEL_NAME], 'gemini-test');
  assert.doesNotMatch(JSON.stringify(span.attributes), /CONTEXT_SECRET|question/);
});

test('concurrent streaming calls keep their own FI session and user context', async () => {
  instrument();
  const fi = local('@traceai/fi-core');
  await Promise.all(['first', 'second'].map(async id => {
    const ctx = fi.setUser(fi.setSession(context.active(), { sessionId: `session-${id}` }), { userId: `user-${id}` });
    const result = await context.with(ctx, () => model().generateContentStream(id));
    await result.response;
  }));
  await settle();
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 2);
  for (const id of ['first', 'second']) {
    const span = spans.find(span => span.attributes['session.id'] === `session-${id}`);
    assert.ok(span);
    assert.equal(span.attributes['user.id'], `user-${id}`);
    assert.equal(JSON.parse(span.attributes[S.LLM_INPUT_MESSAGES])[0].content[0].text, id);
  }
});
