import { InMemorySpanExporter, type ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { privacyFilter } from "@traceai/fi-core";

// Fully offline: fetch is stubbed, spans go to an in-memory exporter, secrets are synthetic.
// The wire layer writes `gen_ai.tool.definitions.N.tool.json_schema` (a key fi-core only
// masks by prefix since 0e0018d3) through FITracer and exports through init()'s privacy boundary.
const TOOL_SECRET = "quokka lookup rationale for confidential oncology intake";
const PROMPT_SECRET = "please summarize the confidential merger memo";
const ANSWER_SECRET = "the merger closes in march at a premium";
const url = "https://api.openai.com/v1/chat/completions";
const request = {
  model: "gpt-test",
  messages: [{ role: "user", content: PROMPT_SECRET }],
  tools: [{ type: "function", function: { name: "lookup", description: TOOL_SECRET, parameters: { type: "object", properties: { field_name: { type: "string", description: TOOL_SECRET } } } } }],
};
const answer = JSON.stringify({ id: "r1", model: "gpt-test", choices: [{ index: 0, message: { role: "assistant", content: ANSWER_SECRET }, finish_reason: "stop" }], usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } });

const saved = { ...process.env };
const originalFetch = globalThis.fetch;
let auto: typeof import("../src/index");

beforeAll(() => { context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()); });
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  jest.resetModules();
  auto = require("../src/index");
});
afterEach(async () => {
  try { await auto.shutdown(); } catch { /* already down */ }
  delete (globalThis as any)[Symbol.for("traceai.auto.agent.v1")];
  globalThis.fetch = originalFetch;
  process.env = { ...saved };
  trace.disable();
});
afterAll(() => { context.disable(); });

async function exportedWireSpans(traceConfig: Record<string, boolean>): Promise<ReadableSpan[]> {
  globalThis.fetch = jest.fn(async () => new Response(answer, { headers: { "content-type": "application/json" } })) as any;
  const exporter = new InMemorySpanExporter();
  auto.init({ exporter, apiKey: "", secretKey: "", configFile: "/does-not-exist/wire-tool-schema", instrumentations: ["wire"], traceConfig } as any);
  const response = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(request) });
  expect(await response.text()).toBe(answer);
  await new Promise(resolve => setTimeout(resolve, 50));
  await auto.forceFlush();
  return exporter.getFinishedSpans().filter(span => span.attributes["traceai.capture.layer"] === "wire");
}

test("control: with no hiding the wire span carries the tool schema (the key under test is really written)", async () => {
  const spans = await exportedWireSpans({});
  expect(spans).toHaveLength(1);
  expect(Object.keys(spans[0].attributes).some(key => /^gen_ai\.tool\.definitions\.\d+\.tool\.json_schema$/.test(key))).toBe(true);
  expect(JSON.stringify(spans[0].attributes)).toContain(TOOL_SECRET);
});

test("hideInputs: gen_ai.tool.definitions.N.tool.json_schema is absent or __REDACTED__ on the exported wire span", async () => {
  const spans = await exportedWireSpans({ hideInputs: true });
  expect(spans).toHaveLength(1);
  for (const [key, value] of Object.entries(spans[0].attributes)) {
    if (/tool\.json_schema$|^gen_ai\.tool\.definitions/.test(key)) expect([undefined, "__REDACTED__"]).toContain(value);
  }
  const json = JSON.stringify(spans.map(span => ({ name: span.name, attributes: span.attributes, events: span.events })));
  expect(json).not.toContain(TOOL_SECRET);
  expect(json).not.toContain(PROMPT_SECRET);
  // hideInputs alone leaves the output side visible.
  expect(json).toContain(ANSWER_SECRET);
});

test("hideInputs and hideOutputs: no synthetic secret appears anywhere in the exported wire span JSON", async () => {
  const spans = await exportedWireSpans({ hideInputs: true, hideOutputs: true });
  expect(spans).toHaveLength(1);
  const json = JSON.stringify(spans.map(span => ({ name: span.name, attributes: span.attributes, events: span.events })));
  for (const secret of [TOOL_SECRET, PROMPT_SECRET, ANSWER_SECRET]) expect(json).not.toContain(secret);
});

test("the export boundary alone (privacyFilter) masks a descendant tool.json_schema key", () => {
  const attrs = privacyFilter({ hideInputs: true }).attributes({ "gen_ai.tool.definitions.0.tool.json_schema": TOOL_SECRET, "gen_ai.tool.definitions.1.tool.json_schema": TOOL_SECRET });
  expect(JSON.stringify(attrs)).not.toContain(TOOL_SECRET);
});

// Forwarded application spans (forwardApplicationSpans) are written by other tracers with raw
// OTel span.setAttribute and leave through the same export boundary. forwarded-content.ts only
// knows a fixed key list, so the keys fi-core masks by prefix are protected by privacyFilter alone.
const INPUT_SIDE: Record<string, string> = {
  "tool.json_schema": "fwd input tool schema rationale",
  "tool.parameters": "fwd input tool parameters rationale",
  "tool.description": "fwd input tool description rationale",
  "tool_call.function.arguments": "fwd input function arguments rationale",
  "message.function_call_arguments_json": "fwd input legacy arguments rationale",
  "gen_ai.tool.parameters": "fwd input genai tool parameters rationale",
  "gen_ai.agent.description": "fwd input agent description rationale",
  "gen_ai.prompts.0.content": "fwd input prompts rationale",
  "gen_ai.image.prompt": "fwd input image prompt rationale",
  "gen_ai.computer_use.text": "fwd input computer text rationale",
  "gen_ai.retrieval.query": "fwd input retrieval query rationale",
  "gen_ai.reranker.query": "fwd input genai reranker query rationale",
  "reranker.query": "fwd input reranker query rationale",
  "reranker.input_documents.0.document.content": "fwd input reranker docs rationale",
};
const OUTPUT_SIDE: Record<string, string> = {
  "gen_ai.retrieval.documents.0.document.content": "fwd output retrieval docs rationale",
  "reranker.output_documents.0.document.content": "fwd output reranker docs rationale",
  "gen_ai.reranker.output_documents.0.document.content": "fwd output genai reranker docs rationale",
  "gen_ai.guardrail.modified_output": "fwd output guardrail rationale",
  "gen_ai.evaluation.explanation": "fwd output evaluation rationale",
  "gen_ai.image.output_urls.0": "fwd output image url rationale",
  "gen_ai.computer_use.result": "fwd output computer result rationale",
};
const EITHER_SIDE: Record<string, string> = {
  "gen_ai.voice.transcript": "fwd either voice transcript rationale",
  "gen_ai.audio.url": "fwd either audio url rationale",
  "gen_ai.voice.recording.url": "fwd either recording url rationale",
  "gen_ai.computer_use.screenshot": "fwd either screenshot rationale",
};

async function forwardedSpan(traceConfig: Record<string, boolean>, content: Record<string, string>): Promise<ReadableSpan> {
  const { BasicTracerProvider } = require("@opentelemetry/sdk-trace-base");
  globalThis.fetch = jest.fn(async () => new Response("{}")) as any;
  const exporter = new InMemorySpanExporter();
  const app = new BasicTracerProvider();
  auto.init({ provider: app, exporter, apiKey: "", secretKey: "", configFile: "/does-not-exist/wire-tool-schema", instrumentations: ["wire"], forwardApplicationSpans: true, traceConfig } as any);
  const span = app.getTracer("foreign-ai-tracer").startSpan("foreign-ai-call");
  span.setAttribute("app.order", "order-1");
  for (const [key, value] of Object.entries(content)) span.setAttribute(key, value);
  span.end();
  await new Promise(resolve => setTimeout(resolve, 50));
  await auto.forceFlush();
  const found = exporter.getFinishedSpans().find(candidate => candidate.name === "foreign-ai-call");
  expect(found).toBeDefined();
  return found!;
}
function expectHidden(span: ReadableSpan, content: Record<string, string>) {
  for (const key of Object.keys(content)) expect([undefined, "__REDACTED__", "[REDACTED]"]).toContain(span.attributes[key]);
  const json = JSON.stringify({ name: span.name, attributes: span.attributes, events: span.events });
  for (const value of Object.values(content)) expect(json).not.toContain(value);
}

test("control: a forwarded foreign span carries the content keys when nothing is hidden", async () => {
  const content = { ...INPUT_SIDE, ...OUTPUT_SIDE, ...EITHER_SIDE };
  const span = await forwardedSpan({}, content);
  for (const [key, value] of Object.entries(content)) expect(span.attributes[key]).toBe(value);
});

test("forwarded foreign span, hideInputs: tool.json_schema, tool.parameters, tool_call.function.arguments, prompts and the other input keys are masked", async () => {
  const span = await forwardedSpan({ hideInputs: true }, { ...INPUT_SIDE, ...EITHER_SIDE });
  expectHidden(span, { ...INPUT_SIDE, ...EITHER_SIDE });
  expect(span.attributes["app.order"]).toBe("order-1");
});

test("forwarded foreign span, hideOutputs: retrieval/reranker documents, guardrail and evaluation output are masked", async () => {
  const span = await forwardedSpan({ hideOutputs: true }, { ...OUTPUT_SIDE, ...EITHER_SIDE });
  expectHidden(span, { ...OUTPUT_SIDE, ...EITHER_SIDE });
  expect(span.attributes["app.order"]).toBe("order-1");
});
