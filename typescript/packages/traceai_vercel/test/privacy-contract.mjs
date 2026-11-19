// Real SDK privacy and operation regressions; synthetic providers only.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const require = createRequire(resolve(process.argv[2], 'package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const ai = await load('ai');
const { createOpenAI } = await load('@ai-sdk/openai');
const major = Number(require('ai/package.json').version.split('.')[0]);
for (const key of ['FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_OUTPUT_TEXT', 'FI_HIDE_ERROR_DETAILS', 'FI_HIDE_EMBEDDING_VECTORS']) process.env[key] = 'true';
const { BasicTracerProvider, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { FISimpleSpanProcessor, FIBatchSpanProcessor, isFISpan } = require('@traceai/vercel');
const { context, trace } = require('@opentelemetry/api');
const provider = new BasicTracerProvider();
const manager = new AsyncLocalStorageContextManager().enable();
provider.register({ contextManager: manager });
const exporter = new InMemorySpanExporter();
const raw = [];
provider.addSpanProcessor({ onStart() {}, onEnd(span) { raw.push({ name: span.name, kind: span.kind, attrs: { ...span.attributes } }); }, forceFlush: async () => {}, shutdown: async () => {} });
const Processor = process.argv[3] === 'batch' ? FIBatchSpanProcessor : FISimpleSpanProcessor;
provider.addSpanProcessor(new Processor({ exporter, spanFilter: isFISpan }));
const tracer = provider.getTracer('review-probes');
if (major >= 7) {
  const { OpenTelemetry } = await load('@ai-sdk/otel');
  ai.registerTelemetry(new OpenTelemetry({ tracer, headers: true, embedding: true, reranking: true, experimental_decision: true }));
}
const telemetry = (record = true) => major >= 7 ? { telemetry: { recordInputs: record, recordOutputs: record } } : { experimental_telemetry: { isEnabled: true, tracer, recordInputs: record, recordOutputs: record } };
const completion = (message, finish = 'stop') => ({ id: 'synthetic-completion', object: 'chat.completion', created: 1, model: 'test-model', choices: [{ index: 0, message: { role: 'assistant', ...message }, finish_reason: finish }], usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 } });
function model(responses) {
  let index = 0;
  return createOpenAI({ apiKey: 'synthetic-provider-key', baseURL: 'https://fixture.invalid/v1', fetch: async () => {
    const response = responses[Math.min(index++, responses.length - 1)];
    return response instanceof Response ? response : Response.json(response);
  } }).chat('test-model');
}
async function collect() {
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans().map(s => ({ name: s.name, kind: s.kind, spanId: s.spanContext().spanId, traceId: s.spanContext().traceId, parentSpanId: s.parentSpanId, attrs: { ...s.attributes }, status: s.status, events: s.events }));
  exporter.reset();
  return { exported: spans, raw: raw.splice(0) };
}
const report = { sdk: require('ai/package.json').version, processor: Processor.name };
try {
  await ai.generateText({ model: model([completion({ content: 'private-output-marker' })]), prompt: 'private-input-marker', ...telemetry() });
  report.fiHide = await collect();
  report.fiHide.inputLeaked = JSON.stringify(report.fiHide.exported).includes('private-input-marker');
  report.fiHide.outputLeaked = JSON.stringify(report.fiHide.exported).includes('private-output-marker');
  assert.ok(!report.fiHide.inputLeaked && !report.fiHide.outputLeaked, 'FI_HIDE masks all input/output copies');

  await ai.generateText({ model: model([completion({ content: 'safe' })]), prompt: 'safe', headers: { Authorization: 'Bearer synthetic-header-secret', 'X-Api-Key': 'synthetic-extra-key' }, ...telemetry(false) });
  report.headers = await collect();
  report.headers.authorizationLeaked = JSON.stringify(report.headers.exported).includes('synthetic-header-secret');
  report.headers.apiKeyLeaked = JSON.stringify(report.headers.exported).includes('synthetic-extra-key');
  assert.ok(!report.headers.authorizationLeaked && !report.headers.apiKeyLeaked, 'credential headers never exported');

  await assert.rejects(ai.generateText({ model: model([Response.json({ error: { message: 'private-error-marker', type: 'invalid_request_error' } }, { status: 400 })]), prompt: 'private-error-input', maxRetries: 0, ...telemetry(false) }));
  report.errors = await collect();
  report.errors.detailLeaked = JSON.stringify(report.errors.exported).includes('private-error-marker');
  assert.ok(!report.errors.detailLeaked, 'FI_HIDE_ERROR_DETAILS masks event, attribute and status details');
  assert.ok(report.errors.exported.some(s => s.status.code === 2), 'native error status retained');

  const toolResponse = completion({ content: 'Tool explanation', tool_calls: [{ id: 'synthetic-call', type: 'function', function: { name: 'lookup', arguments: '{"city":"Paris"}' } }] }, 'tool_calls');
  const tools = { lookup: ai.tool({ inputSchema: ai.jsonSchema({ type: 'object', properties: { city: { type: 'string' } }, required: ['city'], additionalProperties: false }), execute: async () => 'private-tool-output' }) };
  await ai.generateText({ model: model([toolResponse, completion({ content: 'finished' })]), tools, stopWhen: ai.stepCountIs(2), prompt: 'tools', ...telemetry() });
  report.tools = await collect();
  assert.ok(!JSON.stringify(report.tools.exported).includes('private-tool-output'));
  assert.ok(!JSON.stringify(report.tools.exported).includes('Paris'));
  report.tools.fiToolFields = report.tools.exported.filter(s => s.attrs['fi.span.kind'] === 'TOOL').map(s => ({ name: s.attrs['tool.name'] ?? null, id: s.attrs['tool_call.id'] ?? null, input: s.attrs['input.value'] ?? null, output: s.attrs['output.value'] ?? null }));

  await ai.generateText({ model: model([toolResponse, completion({ content: 'finished' })]), tools, stopWhen: ai.stepCountIs(2), prompt: 'tools', ...telemetry(false) });
  report.toolOptOut = await collect();
  assert.ok(!JSON.stringify(report.toolOptOut.exported).includes('private-tool-output'), 'native tool output opt-out works');
  assert.ok(!JSON.stringify(report.toolOptOut.exported).includes('Paris'), 'native tool input opt-out works');

  const embeddingModel = createOpenAI({ apiKey: 'synthetic-provider-key', fetch: async () => Response.json({ object: 'list', data: [{ object: 'embedding', index: 0, embedding: [0.123456, 0.654321] }], model: 'text-embedding-3-small', usage: { prompt_tokens: 7, total_tokens: 7 } }) }).embedding('text-embedding-3-small');
  await ai.embed({ model: embeddingModel, value: 'private-embedding-input', ...telemetry() });
  report.embedding = await collect();
  report.embedding.fiKinds = report.embedding.exported.map(s => s.attrs['fi.span.kind']);
  assert.ok(!JSON.stringify(report.embedding.exported).includes('private-embedding-input'));
  assert.ok(!JSON.stringify(report.embedding.exported).includes('0.123456'));

  await ai.rerank({ model: { specificationVersion: major >= 7 ? 'v4' : 'v3', provider: 'synthetic.rerank', modelId: 'synthetic-reranker', doRerank: async () => ({ ranking: [{ index: 0, relevanceScore: 0.9 }], warnings: [] }) }, documents: ['Paris', 'Berlin'], query: 'France', topN: 1, ...telemetry(false) });
  report.rerank = await collect();
  assert.equal(report.rerank.raw.length, 2);
  assert.equal(report.rerank.exported.length, report.rerank.raw.length, 'all native rerank spans exported');
  assert.ok(report.rerank.exported.every(s => s.attrs['fi.span.kind'] === 'RERANKER'));

  if (major >= 7) {
    const speech = await ai.generateSpeech({ model: { specificationVersion: 'v4', provider: 'synthetic.speech', modelId: 'synthetic-speech', doGenerate: async () => ({ audio: new Uint8Array([0x49, 0x44, 0x33, 1, 2, 3]), warnings: [], response: { timestamp: new Date(0), modelId: 'synthetic-speech', headers: {} } }) }, text: 'Hello', ...telemetry(false) });
    assert.ok(speech.audio);
    report.speech = await collect();
    assert.ok(report.speech.raw.length > 0);
    assert.equal(report.speech.exported.length, report.speech.raw.length, 'all native speech spans exported');

    const audio = new Uint8Array([0x49, 0x44, 0x33, 1, 2, 3]);
    const transcriptionModel = {
      specificationVersion: 'v4', provider: 'synthetic.transcription', modelId: 'synthetic-transcription',
      doGenerate: async () => ({ text: 'private-transcript', segments: [], language: 'en', durationInSeconds: 1,
        warnings: [], response: { timestamp: new Date(0), modelId: 'synthetic-transcription', headers: {} } }),
      doStream: async () => ({ stream: new ReadableStream({ start(controller) {
        controller.enqueue({ type: 'stream-start', warnings: [] });
        controller.enqueue({ type: 'transcript-delta', delta: 'private-transcript' });
        controller.enqueue({ type: 'finish', text: 'private-transcript', segments: [] });
        controller.close();
      } }) }),
    };
    await ai.transcribe({ model: transcriptionModel, audio, ...telemetry() });
    report.transcription = await collect();
    assert.ok(report.transcription.raw.length > 0);
    assert.equal(report.transcription.exported.length, report.transcription.raw.length);
    assert.ok(!JSON.stringify(report.transcription.exported).includes('private-transcript'));

    const transcriptionStream = ai.experimental_streamTranscribe({ model: transcriptionModel,
      audio: new ReadableStream({ start(controller) { controller.enqueue(audio); controller.close(); } }),
      inputAudioFormat: { type: 'audio/pcm', rate: 16000 }, ...telemetry() });
    for await (const _ of transcriptionStream.fullStream) { /* drain native stream finalization */ }
    report.streamTranscription = await collect();
    assert.ok(report.streamTranscription.raw.length > 0);
    assert.equal(report.streamTranscription.exported.length, report.streamTranscription.raw.length);
    assert.ok(!JSON.stringify(report.streamTranscription.exported).includes('private-transcript'));

    await ai.experimental_decide({ model: {
      specificationVersion: 'v4', provider: 'synthetic.decision', modelId: 'synthetic-decision',
      supportedQuestionTypes: ['boolean'],
      doDecide: async () => ({ answers: { safe: { type: 'boolean', probability: 0.75 } }, warnings: [] }),
    }, state: 'private-decision-state', questions: { safe: { type: 'boolean', instructions: 'private-decision-question' } }, ...telemetry() });
    report.decision = await collect();
    assert.equal(report.decision.raw.length, 2);
    assert.equal(report.decision.exported.length, report.decision.raw.length);
    assert.ok(report.decision.exported.every(s => s.attrs['fi.span.kind'] === 'EVALUATOR'));
    assert.ok(!JSON.stringify(report.decision.exported).includes('private-decision'));

  }

  const parent = tracer.startSpan('synthetic-parent', { attributes: { 'fi.span.kind': 'CHAIN' } });
  await context.with(trace.setSpan(context.active(), parent), async () => {
    await Promise.all([1, 2].map(n => ai.generateText({ model: model([completion({ content: `concurrent-${n}` })]), prompt: `concurrent-${n}`, ...telemetry(false) })));
  });
  parent.end();
  report.concurrency = await collect();
  assert.equal(new Set(report.concurrency.exported.map(s => s.spanId)).size, report.concurrency.exported.length);
  assert.equal(new Set(report.concurrency.exported.map(s => s.traceId)).size, 1);
  assert.equal(report.concurrency.exported.filter(s => s.parentSpanId === parent.spanContext().spanId).length, 2, 'async siblings parented correctly');
  console.log(JSON.stringify(report, null, 2));
} finally {
  await provider.shutdown();
  manager.disable();
}
