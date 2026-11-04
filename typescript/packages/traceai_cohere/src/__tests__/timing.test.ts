import { performance } from "node:perf_hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CohereInstrumentation } from "../instrumentation";

jest.mock("node:perf_hooks", () => ({ performance: { now: jest.fn() } }));
const now = performance.now as jest.Mock;
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation: CohereInstrumentation;
let clock: number;
const timing = "cohere.stream.time_to_first_token_ms";
beforeEach(() => {
  clock = 1000;
  now.mockImplementation(() => clock);
  exporter.reset();
  instrumentation = new CohereInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
  instrumentation.setTracerProvider(provider);
});
afterEach(() => instrumentation.disable());
afterAll(() => provider.shutdown());
const metadata = { type: "message-start", id: "synthetic-id", delta: { message: { role: "assistant" } } };
const empty = { type: "content-start", index: 0, delta: { message: { content: { type: "text", text: "" } } } };
const text = { type: "content-delta", index: 0, delta: { message: { content: { text: "hello" } } } };
const terminal = { type: "message-end", delta: { finishReason: "COMPLETE" } };
const firstContent = [
  text,
  { type: "content-start", index: 0, delta: { message: { content: { type: "thinking", thinking: "reasoning" } } } },
  { type: "tool-plan-delta", delta: { message: { toolPlan: "plan" } } },
  { type: "tool-call-start", index: 0, delta: { message: { toolCalls: { function: { name: "lookup", arguments: "" } } } } },
  { type: "tool-call-delta", index: 0, delta: { message: { tool_calls: { function: { arguments: "{" } } } } },
  { eventType: "text-generation", text: "v1 text" },
  { event_type: "tool-calls-chunk", tool_call_delta: { index: 0, parameters: "{" } },
];

it.each(firstContent)("measures from request start to the first meaningful content only (%j)", async first => {
  class Client {
    async chatStream() {
      clock += 40; // Request/headers time must be included, not just iteration.
      return { async *[Symbol.asyncIterator]() {
        clock += 10; yield metadata;
        clock += 10; yield empty;
        yield { type: "tool-call-start", index: 0, delta: { message: { toolCalls: { id: "call-id", type: "function" } } } };
        yield { type: "citation-start", index: 0, delta: { message: { citations: { start: 0, end: 8, text: "Evidence" } } } };
        clock += 25; yield first;
        clock += 100; yield text;
        clock += 100; yield terminal;
      } };
    }
  }
  instrumentation.manuallyInstrument({ CohereClient: Client });
  for await (const _event of await new Client().chatStream()) { /* consume */ }
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[timing]).toBe(85);
});

it.each(["cancel-before", "cancel-after", "error-before", "error-after", "empty", "metadata-only"])("does not fabricate or lose timing on %s", async ending => {
  const failure = new Error("synthetic stream failure");
  class Client {
    async chatStream() {
      return { async *[Symbol.asyncIterator]() {
        clock += 30;
        if (ending !== "empty") yield metadata;
        if (ending === "error-before") throw failure;
        if (ending.endsWith("after")) { clock += 20; yield text; }
        if (ending === "error-after") throw failure;
        yield terminal;
      } };
    }
  }
  instrumentation.manuallyInstrument({ CohereClient: Client });
  const consume = async () => {
    for await (const event of await new Client().chatStream()) {
      if (ending === "cancel-before" || (ending === "cancel-after" && event === text)) break;
    }
  };
  if (ending.startsWith("error")) await expect(consume()).rejects.toBe(failure);
  else await consume();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[timing]).toBe(ending.endsWith("after") ? 50 : undefined);
});
