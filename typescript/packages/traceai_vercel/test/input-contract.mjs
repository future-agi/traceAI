// Real SDK input-only privacy and reranking regressions; synthetic providers only.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const require = createRequire(resolve(process.argv[2], 'package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const ai = await load('ai');
const { createOpenAI } = await load('@ai-sdk/openai');
const sdk = require('ai/package.json').version;
const major = Number(sdk.split('.')[0]);
for (const key of Object.keys(process.env)) if (key.startsWith('FI_HIDE_')) delete process.env[key];
process.env.FI_HIDE_INPUTS = 'true';
const { BasicTracerProvider, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { FISimpleSpanProcessor, FIBatchSpanProcessor, isFISpan } = require('@traceai/vercel');
const Processor = process.argv[3] === 'batch' ? FIBatchSpanProcessor : FISimpleSpanProcessor;
const manager = new AsyncLocalStorageContextManager().enable();
const provider = new BasicTracerProvider();
provider.register({ contextManager: manager });
const raw = [];
provider.addSpanProcessor({ onStart() {}, onEnd(span) { raw.push({ name: span.name, attrs: { ...span.attributes } }); }, async forceFlush() {}, async shutdown() {} });
const exporter = new InMemorySpanExporter();
provider.addSpanProcessor(new Processor({ exporter, spanFilter: isFISpan }));
const tracer = provider.getTracer('independent-review');
if (major >= 7) {
  const { OpenTelemetry } = await load('@ai-sdk/otel');
  ai.registerTelemetry(new OpenTelemetry({ tracer, reranking: true }));
}
const telemetry = major >= 7 ? { telemetry: { recordInputs: true, recordOutputs: true } }
  : { experimental_telemetry: { isEnabled: true, recordInputs: true, recordOutputs: true, tracer } };
const completion = { id: 'synthetic', object: 'chat.completion', created: 1, model: 'test-model',
  choices: [{ index: 0, message: { role: 'assistant', content: 'safe-output' }, finish_reason: 'stop' }],
  usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 } };
const model = createOpenAI({ apiKey: 'synthetic-key', fetch: async (_url, init) => {
  if (JSON.parse(init.body).stream) {
    const chunks = [
      { choices: [{ index: 0, delta: { role: 'assistant', content: 'safe-output' }, finish_reason: null }] },
      { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: completion.usage },
    ].map(chunk => `data: ${JSON.stringify({ id: 'synthetic', object: 'chat.completion.chunk', created: 1, model: 'test-model', ...chunk })}\n\n`).join('') + 'data: [DONE]\n\n';
    return new Response(chunks, { headers: { 'content-type': 'text/event-stream' } });
  }
  return Response.json(completion);
} }).chat('test-model');
async function collect() {
  await provider.forceFlush();
  const out = { raw: raw.splice(0), exported: exporter.getFinishedSpans().map(s => ({ name: s.name, attrs: { ...s.attributes } })) };
  exporter.reset();
  return out;
}
const report = { sdk, processor: Processor.name, inputHiding: true, cases: {} };
try {
  for (const operation of ['generateText', 'streamText']) {
    const params = { model, prompt: 'private-prompt-marker', tools: { lookup: ai.tool({
      description: 'private-tool-description-marker',
      inputSchema: ai.jsonSchema({ type: 'object', properties: { account: { type: 'string', enum: ['private-tool-schema-marker'] } }, required: ['account'], additionalProperties: false }),
      execute: async () => 'safe-result',
    }) }, ...telemetry };
    if (operation === 'generateText') await ai.generateText(params);
    else await ai.streamText(params).consumeStream();
    const result = await collect();
    assert.ok(result.raw.some(s => JSON.stringify(s.attrs).includes('private-tool-description-marker')), 'native SDK actually recorded the marked tool input');
    assert.ok(result.raw.some(s => JSON.stringify(s.attrs).includes('private-tool-schema-marker')), 'native SDK actually recorded the marked schema');
    result.promptLeaked = JSON.stringify(result.exported).includes('private-prompt-marker');
    result.toolDefinitionLeaked = JSON.stringify(result.exported).includes('private-tool-description-marker');
    result.schemaLeaked = JSON.stringify(result.exported).includes('private-tool-schema-marker');
    result.leakingKeys = result.exported.flatMap(s => Object.entries(s.attrs).filter(([,v]) => JSON.stringify(v).includes('private-tool-')).map(([k]) => k));
    report.cases[operation] = result;
  }
  const ranking = await ai.rerank({ model: {
    specificationVersion: major >= 7 ? 'v4' : 'v3', provider: 'synthetic.rerank', modelId: 'test-reranker',
    async doRerank() { return { ranking: [{ index: 0, relevanceScore: 0.9 }], warnings: [] }; },
  }, documents: ['public-document-one', 'public-document-two'], query: 'public-query', topN: 1, ...telemetry });
  assert.equal(ranking.ranking.length, 1);
  report.cases.rerank = await collect();
  assert.equal(report.cases.rerank.raw.length, 2, 'both native reranking spans actually ended');
  report.cases.rerank.dropped = report.cases.rerank.raw.length - report.cases.rerank.exported.length;
  console.log(JSON.stringify(report, null, 2));
  for (const operation of ['generateText', 'streamText']) {
    const result = report.cases[operation];
    assert.equal(result.exported.length, result.raw.length, `${operation}: all native spans exported`);
    assert.ok(JSON.stringify(result.exported).includes('safe-output'), 'input hiding retains output');
    assert.equal(result.promptLeaked, false, `${operation}: prompt hidden`);
    assert.equal(result.toolDefinitionLeaked, false, `${operation}: tool description hidden`);
    assert.equal(result.schemaLeaked, false, `${operation}: tool schema hidden`);
  }
  assert.equal(report.cases.rerank.dropped, 0, 'all native reranking spans exported');
  assert.ok(report.cases.rerank.exported.every(s => s.attrs['fi.span.kind'] === 'RERANKER'));

} finally { await provider.shutdown(); manager.disable(); }
