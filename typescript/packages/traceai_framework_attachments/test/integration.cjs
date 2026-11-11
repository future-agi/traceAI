// Real framework invocation against loopback attachment HTTP. No provider keys.
// The matrix installs exact upstream versions in a separate cloud-only directory.
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { join } = require('node:path');
const { fixture, PNG, URI } = require('./fixture.cjs');
const { context, trace } = require('@opentelemetry/api');
const packageRequire = name => createRequire(join(__dirname, `../../traceai_${name}/package.json`));
const upstream = process.env.FRAMEWORK_MATRIX_DIR ? createRequire(join(process.env.FRAMEWORK_MATRIX_DIR, 'package.json')) : require;
const [framework, version] = process.argv.slice(2);
const alias = `${framework}-${version}`;
async function main() {
  const { AsyncLocalStorageContextManager } = packageRequire('langchain')('@opentelemetry/context-async-hooks');
  if (framework !== "genkit") context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  const sdk = framework === "langchain" ? undefined : upstream(alias);
  for (const privacy of ['allowed', 'disabled', 'hide', 'pii', 'native-disabled']) {
    const f = await fixture({ enabled: privacy !== 'disabled', traceConfig: { hideInputs: privacy === 'hide', hideOutputs: privacy === 'hide', piiRedaction: privacy === 'pii' } });
    const binding = { attachments: f.manager, capture: { captureContent: privacy !== 'native-disabled' } };
    try {
      if (framework === 'langchain') {
        const { FITracer } = require('@traceai/fi-core');
        const { LangChainTracer } = require('../../traceai_langchain/dist/src/tracer.js');
        const { FakeListChatModel } = upstream(`${alias}/utils/testing`);
        const { HumanMessage } = upstream(`${alias}/messages`);
        const tracer = new LangChainTracer(new FITracer({ tracer: f.tracer }), true, binding);
        const model = new FakeListChatModel({ responses: ['synthetic response'] });
        const result = await model.invoke([new HumanMessage({ content: [{ type: 'text', text: 'synthetic input' }, { type: 'image_url', image_url: { url: URI } }] })], { callbacks: [tracer] });
        assert.equal(result.content, 'synthetic response');
      } else if (framework === 'llamaindex') {
        // The real OpenAI chat path is version-qualified; the provider transport
        // receives a synthetic response through its supported custom fetch hook.
        const { chatWrapper } = require('../../traceai_llamaindex/dist/src/wrapper.js');
        const OpenAI = sdk.OpenAI ?? upstream(`llamaopenai-${version}`).OpenAI;
        const llm = new OpenAI({ apiKey: 'synthetic-key', model: 'gpt-4o', additionalSessionOptions: {
          fetch: async () => new Response(JSON.stringify({ id: 'synthetic', object: 'chat.completion', created: 1, model: 'gpt-4o',
            choices: [{ index: 0, message: { role: 'assistant', content: 'synthetic response' }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }), { headers: { 'content-type': 'application/json' } }),
        } });
        const wrapped = chatWrapper({ className: 'OpenAI' }, binding, { warn() {} }, () => f.tracer)(llm.chat);
        const result = await wrapped.call(llm, { messages: [{ role: 'user', content: [{ type: 'image_url', image_url: { url: URI } }] }] });
        assert.equal(result.message.content, 'synthetic response');
      } else if (framework === 'ai') {
        const { VercelAIInstrumentation } = require('../../traceai_vercel/dist/src/auto.js');
        const instrumentation = new VercelAIInstrumentation({ ...binding, instrumentationConfig: { enabled: true } });
        instrumentation.setTracerProvider(f.provider);
        const major = version === 'floor' ? 5 : 7;
        const api = instrumentation.manuallyInstrument(sdk, `${major}.0.0`);
        const mocks = upstream(`${alias}/test`);
        const Model = mocks[`MockLanguageModelV${major === 5 ? 2 : 4}`];
        const usage = major === 5 ? { inputTokens: 1, outputTokens: 1 } : {
          inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 1, text: 1, reasoning: 0 } };
        const finishReason = major === 5 ? 'stop' : { unified: 'stop', raw: 'stop' };
        const result = await api.generateText({ messages: [{ role: 'user', content: [{ type: 'image', image: PNG, mediaType: 'image/png' },
          { type: 'file', data: Buffer.from('%PDF-1.4 synthetic'), mediaType: 'application/pdf' }] }], model: new Model({ doGenerate: {
            content: [{ type: 'text', text: 'synthetic response' }, { type: 'file', data: major === 5 ? PNG : { type: 'data', data: PNG }, mediaType: 'image/png' }], warnings: [], usage, finishReason,
          } }) });
        assert.equal(result.text, 'synthetic response');
        if (privacy === 'allowed') {
          await f.manager.flush();
          const before = f.bodies.length;
          const controller = new AbortController();
          let consumed = 0;
          const streaming = api.streamText({ abortSignal: controller.signal,
            messages: [{ role: 'user', content: [{ type: 'image', image: PNG, mediaType: 'image/png' }] }],
            model: new Model({ doStream: async () => ({ stream: new ReadableStream({ start(stream) {
              stream.enqueue({ type: 'stream-start', warnings: [] });
              stream.enqueue({ type: 'text-start', id: 'synthetic' });
              stream.enqueue({ type: 'text-delta', id: 'synthetic', delta: 'first' });
              controller.signal.addEventListener('abort', () => stream.close(), { once: true });
            } }) }) }),
          });
          for await (const part of streaming.textStream) { assert.equal(part, 'first'); consumed++; controller.abort(); break; }
          await new Promise(resolve => setTimeout(resolve, 20));
          await f.manager.flush();
          assert.equal(consumed, 1); assert.equal(f.bodies.length - before, 1);
          assert.ok(f.exporter.getFinishedSpans().some(span => span.name.includes('streamText')));
          console.log(JSON.stringify({ framework: alias, operation: 'stream-cancel', status: 'PASS' }));
          const beforeTool = f.bodies.length;
          const toolResult = await api.generateText({ prompt: 'synthetic tool', maxRetries: 0,
            model: new Model({ doGenerate: { content: [{ type: 'tool-call', toolCallId: 'tool_1', toolName: 'imageTool', input: '{}' }],
              warnings: [], usage, finishReason: major === 5 ? 'tool-calls' : { unified: 'tool-calls', raw: 'tool-calls' } } }),
            tools: { imageTool: api.tool({ inputSchema: api.jsonSchema({ type: 'object', properties: {} }),
              execute: async () => ({ type: 'content', value: [{ type: 'image-data', data: PNG.toString('base64'), mediaType: 'image/png' }] }),
            }) },
          });
          assert.equal(toolResult.toolResults.length, 1); await f.manager.flush();
          assert.equal(f.bodies.length - beforeTool, major === 5 ? 0 : 1);
          console.log(JSON.stringify({ framework: alias, operation: 'typed-tool', rawCapture: major !== 5, status: 'PASS' }));
        }
        instrumentation.disable();
      } else if (framework === 'genkit') {
        const { enableTelemetry, flushTracing } = upstream(`${alias}/tracing`);
        const { withGenkitModelAttachments, withGenkitToolAttachments } = require('../../traceai_genkit/dist/src/media.js');
        const ai = sdk.genkit({});
        await enableTelemetry({ spanProcessors: [{ onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} }] });
        const model = ai.defineModel({ name: `test/media-${privacy}`, supports: { media: true } }, withGenkitModelAttachments(binding, async () => ({
          message: { role: 'model', content: [{ media: { url: URI, contentType: 'image/png' } }] }, finishReason: 'stop', usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
        })));
        const result = await ai.generate({ model, prompt: [{ media: { url: URI, contentType: 'image/png' } }] });
        assert.equal(result.message.content[0].media.url, URI);
        const tool = ai.defineTool({ name: `media_tool_${privacy}`, description: 'synthetic tool' }, withGenkitToolAttachments(binding, async () => [{ media: { url: URI, contentType: 'image/png' } }]));
        const toolResult = await tool({}); assert.equal(toolResult[0].media.url, URI);
        await flushTracing();
      } else throw new Error('unknown framework');
      await f.manager.flush();
      if (privacy === 'allowed') {
        const expected = framework === 'genkit' ? 3 : framework === 'ai' ? (version === 'floor' ? 3 : 5) : 1;
        assert.equal(f.bodies.length, expected, `${alias}: uploaded count ${f.bodies.length}; spans ${f.exporter.getFinishedSpans().map(span => span.name).join(',')}`);
        assert.ok(f.bodies.some(body => body.equals(PNG)));
        assert.equal(f.completed.length, expected);
      } else assert.equal(f.bodies.length, 0, `${alias} ${privacy}`);
      console.log(JSON.stringify({ framework: alias, privacy, uploaded: f.bodies.length, completed: f.completed.length, status: 'PASS' }));
    } finally { await f.close(); }
  }
  const delegate = trace.getTracerProvider().getDelegate?.();
  await delegate?.shutdown?.();
  trace.disable(); context.disable();
}
main().catch(error => { console.error(error); process.exitCode = 1; });
