import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

// Parity with Python's TraceConfig.mask (python/fi_instrumentation/instrumentation/config.py:
// _INPUT_CONTENT_PREFIXES, _OUTPUT_CONTENT_PREFIXES, _MIXED_CONTENT_PREFIXES and the
// gen_ai.tool.call rule). A key is masked when it equals the prefix or sits below it
// (`prefix.`). "alias" entries are names a TypeScript package still writes for the same field.
const INPUT_KEYS = [
  "gen_ai.system_instructions", "gen_ai.request.parameters", "gen_ai.request.stop_sequences",
  "gen_ai.agent.description", "gen_ai.image.prompt", "gen_ai.image.negative_prompt", "gen_ai.input.images",
  "gen_ai.prompts", "gen_ai.tool.definitions", "gen_ai.tool.description", "gen_ai.tool.call.arguments",
  "gen_ai.tool.parameters", "tool_call.function.arguments", "message.function_call_arguments_json",
  "gen_ai.computer_use.text", "gen_ai.computer_use.element_selector", "tool.json_schema",
  "gen_ai.retrieval.query", "gen_ai.reranker.query", "gen_ai.reranker.input_documents",
  "reranker.query", "reranker.input_documents",
  // aliases written by the TypeScript packages (SemanticConventions.TOOL_PARAMETERS / TOOL_DESCRIPTION)
  "tool.parameters", "tool.description",
];
const OUTPUT_KEYS = [
  "gen_ai.tool.call.result", "gen_ai.image.revised_prompt", "gen_ai.image.output_urls",
  "gen_ai.guardrail.modified_output", "gen_ai.evaluation.explanation", "gen_ai.computer_use.result",
  "retrieval.documents", "gen_ai.retrieval.documents", "gen_ai.reranker.output_documents",
  "reranker.output_documents",
];
const MIXED_KEYS = [
  "gen_ai.voice.transcript", "gen_ai.voice.recording.url", "gen_ai.voice.recording.stereo_url",
  "gen_ai.voice.recording.customer_url", "gen_ai.voice.recording.assistant_url", "gen_ai.audio.url",
  "gen_ai.audio.transcript", "gen_ai.computer_use.screenshot", "gen_ai.computer_use.current_url",
  "gen_ai.tool.call",
];
// Names, ids, counts, model and usage are never content.
const CONTROL_KEYS = [
  "gen_ai.tool.name", "gen_ai.tool.call.id", "gen_ai.tool.type", "tool.name", "gen_ai.request.model",
  "gen_ai.request.temperature", "gen_ai.usage.input_tokens", "gen_ai.response.model", "gen_ai.provider.name",
  "reranker.model_name", "reranker.top_k",
];

function export_(options: Record<string, boolean>, attributes: Record<string, string>) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("mask-parity"), traceConfig: options });
  const span = tracer.startSpan("parity");
  for (const [key, value] of Object.entries(attributes)) span.setAttribute(key, value);
  span.end();
  return exporter.getFinishedSpans()[0].attributes;
}

// One span per key: a scalar key and a key below it cannot live on the same span.
function exportOne(options: Record<string, boolean>, key: string) {
  const exported = export_(options, { [key]: `secret-${key}`, ...Object.fromEntries(CONTROL_KEYS.map((k) => [k, `control-${k}`])) });
  return exported[key];
}

const withDescendant = (keys: string[]) => keys.flatMap((key) => [key, `${key}.0.nested`]);

const cases: Array<[string, Record<string, boolean>, string[]]> = [
  ["hideInputs", { hideInputs: true }, [...INPUT_KEYS, ...MIXED_KEYS]],
  ["hideOutputs", { hideOutputs: true }, [...OUTPUT_KEYS, ...MIXED_KEYS]],
];

describe.each(cases)("%s masks the content keys Python masks", (_name, options, masked) => {
  it.each(withDescendant(masked))("masks %s", (key) => {
    // The mask puts the placeholder in; the privacy layer behind it may drop a key outright.
    // Either way the content is not exported.
    const exported = exportOne(options, key);
    expect([REDACTED_VALUE, undefined]).toContain(exported);
  });

  it("does not mask the non-content keys", () => {
    const exported = export_(options, Object.fromEntries(CONTROL_KEYS.map((k) => [k, `control-${k}`])));
    for (const key of CONTROL_KEYS) expect(exported[key]).toBe(`control-${key}`);
  });
});

it.each(withDescendant([...INPUT_KEYS, ...OUTPUT_KEYS, ...MIXED_KEYS]))("masks nothing without a hide flag: %s", (key) => {
  expect(exportOne({}, key)).toBe(`secret-${key}`);
});
