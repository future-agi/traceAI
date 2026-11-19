/**
 * Tool parameters / function arguments under hideInputs / hideOutputs, through the real OpenAI SDK
 * with a stubbed fetch (no network). openai writes tool data only as llm.tools,
 * llm.invocation_parameters, raw.input/raw.output, input/output.value and message JSON (no
 * tool.parameters, tool_call.function.arguments, message.function_call_arguments_json keys).
 */
import { describe, it, expect, beforeEach } from "@jest/globals";
import OpenAI from "openai";
import { BasicTracerProvider, InMemorySpanExporter, ReadableSpan, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "../instrumentation";

const IN_ARGS = "SYNTH-INPUT-TOOL-ARGS-7731";
const IN_SCHEMA = "SYNTH-TOOL-PARAM-SCHEMA-4410";
const IN_DESC = "SYNTH-TOOL-DESCRIPTION-9902";
const OUT_ARGS = "SYNTH-OUTPUT-TOOL-ARGS-5518";

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const instrumentation = new OpenAIInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(OpenAI);

const reply = (url: string) => {
  const json = (b: unknown) => new Response(JSON.stringify(b), { headers: { "content-type": "application/json" } });
  if (url.endsWith("/responses")) {
    return json({
      id: "resp_1", object: "response", created_at: 1, status: "completed", model: "m",
      output: [{ type: "function_call", id: "fc_1", call_id: "call_1", name: "lookup", arguments: JSON.stringify({ q: OUT_ARGS }), status: "completed" }],
      usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
    });
  }
  return json({
    id: "c1", object: "chat.completion", created: 1, model: "m",
    choices: [{ index: 0, finish_reason: "tool_calls", message: { role: "assistant", content: null,
      tool_calls: [{ id: "call_1", type: "function", function: { name: "lookup", arguments: JSON.stringify({ q: OUT_ARGS }) } }] } }],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  });
};
const client = new OpenAI({ apiKey: "synthetic-key", baseURL: "http://offline.invalid/v1", maxRetries: 0,
  fetch: (async (input: unknown) => reply(String(input))) as unknown as typeof fetch });

const useConfig = (traceConfig: TraceConfigOptions) => {
  (instrumentation as unknown as { fiTracer: FITracer }).fiTracer = new FITracer({ tracer: provider.getTracer("t"), traceConfig });
};
const dump = (s: ReadableSpan) => JSON.stringify({ a: s.attributes, e: s.events, s: s.status });

const tools = [{ type: "function" as const, function: { name: "lookup", description: IN_DESC,
  parameters: { type: "object", properties: { q: { type: "string", description: IN_SCHEMA } } } } }];
const messages = [
  { role: "user" as const, content: "hi" },
  { role: "assistant" as const, content: null, tool_calls: [{ id: "call_0", type: "function" as const, function: { name: "lookup", arguments: JSON.stringify({ q: IN_ARGS }) } }] },
  { role: "tool" as const, tool_call_id: "call_0", content: "ok" },
];
async function run() {
  await client.chat.completions.create({ model: "m", messages, tools });
  await client.responses.create({
    model: "m", tools: [{ type: "function", name: "lookup", description: IN_DESC, strict: false,
      parameters: { type: "object", properties: { q: { type: "string", description: IN_SCHEMA } } } }],
    input: [{ type: "function_call", call_id: "call_0", name: "lookup", arguments: JSON.stringify({ q: IN_ARGS }) },
      { type: "function_call_output", call_id: "call_0", output: "ok" }],
  } as never);
  await new Promise((r) => setTimeout(r, 30));
  const spans = exporter.getFinishedSpans();
  expect(spans.length).toBe(2);
  return spans;
}

describe("openai tool parameters / arguments privacy", () => {
  beforeEach(() => exporter.reset());

  it("control: unhidden export contains the synthetic secrets", async () => {
    useConfig({});
    const all = (await run()).map(dump).join("\n");
    for (const s of [IN_ARGS, IN_SCHEMA, OUT_ARGS]) expect(all).toContain(s);
  });

  it("hideInputs: no input-side tool argument, schema or description is exported", async () => {
    useConfig({ hideInputs: true });
    for (const span of await run()) {
      const text = dump(span);
      for (const s of [IN_ARGS, IN_SCHEMA, IN_DESC]) expect(text).not.toContain(s);
    }
  });

  it("hideOutputs: no output-side tool call arguments are exported", async () => {
    useConfig({ hideOutputs: true });
    for (const span of await run()) expect(dump(span)).not.toContain(OUT_ARGS);
  });
});
