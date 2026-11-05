// Regression for SDK-native abort and transport-error span finalization.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const require = createRequire(resolve(process.argv[2], 'package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const ai = await load('ai');
const { createOpenAI } = await load('@ai-sdk/openai');
const { BasicTracerProvider, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { FISimpleSpanProcessor, FIBatchSpanProcessor, isFISpan } = require('@traceai/vercel');
const major = Number(require('ai/package.json').version.split('.')[0]);
const provider = new BasicTracerProvider();
const manager = new AsyncLocalStorageContextManager().enable();
provider.register({ contextManager: manager });
const started = new Set(), ended = new Set(), names = new Map();
provider.addSpanProcessor({ onStart(span) { started.add(span.spanContext().spanId); names.set(span.spanContext().spanId, span.name); }, onEnd(span) { ended.add(span.spanContext().spanId); }, forceFlush: async () => {}, shutdown: async () => {} });
const exporter = new InMemorySpanExporter();
const Processor = process.argv[3] === 'batch' ? FIBatchSpanProcessor : FISimpleSpanProcessor;
provider.addSpanProcessor(new Processor({ exporter, spanFilter: isFISpan }));
const tracer = provider.getTracer('lifecycle-probe');
if (major >= 7) { const { OpenTelemetry } = await load('@ai-sdk/otel'); ai.registerTelemetry(new OpenTelemetry({ tracer })); }
const telemetry = major >= 7 ? { telemetry: { recordInputs: false, recordOutputs: false } } : { experimental_telemetry: { isEnabled: true, tracer, recordInputs: false, recordOutputs: false } };
const chunk = text => new TextEncoder().encode(`data: ${JSON.stringify({ id: 'test', object: 'chat.completion.chunk', created: 1, model: 'test', choices: [{ index: 0, delta: { role: 'assistant', content: text }, finish_reason: null }] })}\n\n`);
const results = [];
try {
  for (const scenario of ['abort', 'stream-error']) {
    const controller = new AbortController();
    let streamController;
    const model = createOpenAI({ apiKey: 'synthetic', fetch: async (_url, init) => new Response(new ReadableStream({ start(c) { streamController = c; c.enqueue(chunk('first')); init.signal?.addEventListener('abort', () => c.error(new DOMException('Synthetic abort', 'AbortError')), { once: true }); } }), { headers: { 'content-type': 'text/event-stream' } }) }).chat('test');
    const result = ai.streamText({ model, prompt: 'synthetic', abortSignal: controller.signal, maxRetries: 0, onError() {}, ...telemetry });
    const observed = [];
    try {
      for await (const event of result.fullStream) {
        observed.push(event.type);
        if (event.type === 'text-delta') {
          if (scenario === 'abort') controller.abort();
          else streamController.error(new Error('Synthetic stream error'));
        }
      }
    } catch (error) { observed.push(`reader-threw:${error.message}`); }
    await new Promise(resolve => setTimeout(resolve, 250));
    await provider.forceFlush();
    const spans = exporter.getFinishedSpans();
    assert.equal(new Set(spans.map(s => s.spanContext().spanId)).size, spans.length);
    results.push({ scenario, events: observed, started: started.size, ended: ended.size, unfinished: [...started].filter(id => !ended.has(id)).map(id => names.get(id)), exported: spans.map(s => ({ name: s.name, kind: s.attributes['fi.span.kind'], status: s.status.code })) });
    started.clear(); ended.clear(); exporter.reset();
  }
  await provider.forceFlush();
  await provider.shutdown();
  await provider.shutdown();
  console.log(JSON.stringify({ sdk: require('ai/package.json').version, processor: Processor.name, results }));
  for (const result of results) {
    assert.equal(result.ended, result.started, `${result.scenario}: native SDK must end every span`);
    assert.equal(result.exported.length, result.started, `${result.scenario}: every native span exported exactly once`);
  }
} finally { await provider.shutdown(); manager.disable(); }
