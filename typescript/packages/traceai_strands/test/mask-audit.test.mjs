// Offline audit: content keys that fi-core now masks must not leave through the
// Strands export-time adapter (NativePrivacy -> fi-core maskAttributes).
// The installed @strands-agents/sdk (0.1.6) emits no OTel spans, so native spans
// are produced by a real tracer under the scope the adapter filters on, and
// the REAL StrandsSpanExporter / NativePrivacy projects them.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { after, beforeEach, afterEach, test } from 'node:test';
const require = createRequire(import.meta.url);
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { StrandsSpanExporter } = await import('../dist/src/native.js');
const SECRET = 'synthetic-mask-audit-secret';
const memory = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
const tracer = provider.getTracer('strands-agents');
const flags = ['FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_OUTPUT_TEXT', 'FI_HIDE_INPUT_IMAGES', 'FI_PII_REDACTION'];
const saved = new Map(flags.map(f => [f, process.env[f]]));
beforeEach(() => { memory.reset(); for (const f of flags) delete process.env[f]; });
afterEach(() => { for (const f of flags) { if (saved.get(f) === undefined) delete process.env[f]; else process.env[f] = saved.get(f); } });
after(() => provider.shutdown());

const INPUT_KEYS = ['tool.parameters', 'tool.description', 'tool_call.function.arguments',
  'message.function_call_arguments_json', 'tool.json_schema', 'gen_ai.agent.description', 'gen_ai.prompts',
  'gen_ai.retrieval.query', 'reranker.query', 'gen_ai.tool.parameters', 'gen_ai.voice.transcript'];
const OUTPUT_KEYS = ['gen_ai.retrieval.documents.0.document.content', 'reranker.output_documents.0.document.content',
  'gen_ai.guardrail.modified_output', 'gen_ai.evaluation.explanation', 'gen_ai.reranker.output_documents.0.document.content'];

function emit(operation, attrs) {
  const span = tracer.startSpan(operation, { attributes: { 'gen_ai.operation.name': operation, ...attrs } });
  span.end();
}
function project(options) {
  let result;
  new StrandsSpanExporter({ export(spans, cb) { result = spans; cb({ code: 0 }); }, async shutdown() {} }, options)
    .export(memory.getFinishedSpans(), () => {});
  return result;
}
const all = Object.fromEntries([...INPUT_KEYS, ...OUTPUT_KEYS].map(k => [k, SECRET]));

for (const [flag, keys] of [['FI_HIDE_INPUTS', INPUT_KEYS], ['FI_HIDE_OUTPUTS', OUTPUT_KEYS]]) {
  for (const op of ['execute_tool', 'chat', 'memory.search']) {
    test(`${flag} ${op}: newly-masked keys are absent or redacted on exported spans`, () => {
      process.env[flag] = 'true';
      emit(op, all);
      const spans = project({ captureContent: true });
      assert.equal(spans.length, 1);
      for (const key of keys) {
        const v = spans[0].attributes[key];
        assert.ok(v === undefined || v === '__REDACTED__', `${key} exported unmasked`);
      }
    });
  }
}

test('hideInputs and hideOutputs together: secret appears nowhere in exported JSON', () => {
  process.env.FI_HIDE_INPUTS = 'true'; process.env.FI_HIDE_OUTPUTS = 'true';
  emit('execute_tool', { ...all, 'gen_ai.tool.name': 'echo', 'gen_ai.tool.call.arguments': SECRET, 'gen_ai.tool.call.result': SECRET });
  const spans = project({ captureContent: true });
  const json = JSON.stringify(spans.map(s => ({ a: s.attributes, e: s.events, l: s.links, s: s.status })));
  assert.ok(!json.includes(SECRET), 'secret present in exported span JSON');
  assert.equal(spans[0].attributes['gen_ai.tool.name'], 'echo');
});
