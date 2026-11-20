/**
 * Tool parameters / arguments and reranker content under hideInputs / hideOutputs, through the
 * real cohere-ai v2 client with a stubbed fetcher (no network). cohere writes tool data as
 * llm.tools, raw.input/raw.output, llm.output_messages JSON, and reranker.query /
 * reranker.input_documents / reranker.output_documents; all go through FITracer.
 */
import { InMemorySpanExporter, SimpleSpanProcessor, BasicTracerProvider, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { TraceConfigOptions } from "@traceai/fi-core";
import { CohereInstrumentation } from "../instrumentation";
// eslint-disable-next-line @typescript-eslint/no-var-requires
const Cohere = require("cohere-ai");

const IN_SCHEMA = "SYNTH-TOOL-PARAM-SCHEMA-4410";
const IN_DESC = "SYNTH-TOOL-DESCRIPTION-9902";
const IN_ARGS = "SYNTH-INPUT-TOOL-ARGS-7731";
const RERANK_QUERY = "SYNTH-RERANK-QUERY-1204";
const RERANK_DOC = "SYNTH-RERANK-DOC-8836";
const RERANK_OUT_DOC = "SYNTH-RERANK-OUTPUT-DOC-3377";
const OUT_ARGS = "SYNTH-OUTPUT-TOOL-ARGS-5518";

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation: CohereInstrumentation;

const body = (url: string) => url.includes("rerank")
  ? { id: "r1", results: [{ index: 0, relevance_score: 0.9, document: { text: RERANK_OUT_DOC } }] }
  : { id: "c1", finish_reason: "TOOL_CALL", message: { role: "assistant", content: [],
      tool_plan: "plan",
      tool_calls: [{ id: "call_1", type: "function", function: { name: "lookup", arguments: JSON.stringify({ q: OUT_ARGS }) } }] } };
const fetcher = async (args: { url: string }) => ({ ok: true, rawResponse: { status: 200 }, body: body(args.url) });

function setup(traceConfig: TraceConfigOptions) {
  exporter.reset();
  instrumentation = new CohereInstrumentation({ traceConfig });
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(Cohere);
  return new Cohere.CohereClientV2({ token: "offline-placeholder", fetcher });
}
afterEach(() => instrumentation?.disable());
afterAll(() => provider.shutdown());
const dump = (s: ReadableSpan) => JSON.stringify({ a: s.attributes, e: s.events, s: s.status });

async function run(client: any) {
  await client.chat({
    model: "command-test",
    messages: [
      { role: "user", content: "hi" },
      { role: "assistant", toolCalls: [{ id: "call_0", type: "function", function: { name: "lookup", arguments: JSON.stringify({ q: IN_ARGS }) } }] },
      { role: "tool", toolCallId: "call_0", content: "ok" },
    ],
    tools: [{ type: "function", function: { name: "lookup", description: IN_DESC,
      parameters: { type: "object", properties: { q: { type: "string", description: IN_SCHEMA } } } } }],
  });
  await client.rerank({ model: "rerank-test", query: RERANK_QUERY, documents: [RERANK_DOC], topN: 1 });
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(2);
  return spans;
}

it("control: unhidden export contains the synthetic secrets", async () => {
  const all = (await run(setup({}))).map(dump).join("\n");
  for (const s of [IN_SCHEMA, IN_DESC, IN_ARGS, RERANK_QUERY, RERANK_DOC, RERANK_OUT_DOC, OUT_ARGS]) expect(all).toContain(s);
});

it("hideInputs: no tool schema, description, input arguments or rerank query/input documents are exported", async () => {
  for (const span of await run(setup({ hideInputs: true }))) {
    const text = dump(span);
    for (const s of [IN_SCHEMA, IN_DESC, IN_ARGS, RERANK_QUERY, RERANK_DOC]) expect(text).not.toContain(s);
  }
});

it("hideOutputs: no output tool call arguments or reranked documents are exported", async () => {
  for (const span of await run(setup({ hideOutputs: true }))) {
    const text = dump(span);
    for (const s of [OUT_ARGS, RERANK_OUT_DOC]) expect(text).not.toContain(s);
  }
});
