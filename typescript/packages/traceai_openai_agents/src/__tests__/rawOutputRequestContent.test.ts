/**
 * Loopback test with the real @openai/agents SDK against an in-process fake Responses server.
 * The returned Response object carries the request's system instructions and tool definition in the
 * SDK's own `instructions` / `tools` fields (OpenAI Response.instructions, Response.tools).
 */
import * as http from "http";
import { AddressInfo } from "net";
import type * as AgentsSDK from "@openai/agents/dist/index";
import { BatchSpanProcessor, InMemorySpanExporter, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import type { TraceConfigOptions } from "@traceai/fi-core";
import { OpenAIAgentsInstrumentation, _resetPatchedStateForTesting } from "../instrumentation";

// eslint-disable-next-line @typescript-eslint/no-require-imports, @typescript-eslint/no-var-requires
const agents: typeof AgentsSDK = require("@openai/agents");

const INSTRUCTIONS_SENTINEL = "SYNTH-REQUEST-INSTRUCTIONS-a83c";
const TOOL_SENTINEL = "SYNTH-REQUEST-TOOL-DESCRIPTION-17de";
const OUTPUT_SENTINEL = "SYNTH-RESPONSE-TEXT-5f20";

const responseBody = () => ({
  id: "resp_1", object: "response", created_at: 1, status: "completed", model: "gpt-4.1-mini",
  instructions: INSTRUCTIONS_SENTINEL,
  tools: [{ type: "function", name: "lookup", description: TOOL_SENTINEL, parameters: { type: "object", properties: {} }, strict: false }],
  output: [{ type: "message", id: "msg_1", role: "assistant", status: "completed",
    content: [{ type: "output_text", text: OUTPUT_SENTINEL, annotations: [] }] }],
  usage: { input_tokens: 11, output_tokens: 7, total_tokens: 18,
    input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } },
});

describe("OpenAI Agents response span raw output and request content", () => {
  let server: http.Server;
  let baseURL: string;
  let exporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  let instrumentation: OpenAIAgentsInstrumentation;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      req.resume();
      req.on("end", () => {
        res.writeHead(200, { "content-type": "application/json", connection: "close" });
        res.end(JSON.stringify(responseBody()));
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
    agents.setTracingDisabled(false);
  });
  afterAll(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  afterEach(async () => {
    instrumentation?.disable();
    await provider?.shutdown();
  });

  async function run(traceConfig?: TraceConfigOptions): Promise<ReadableSpan[]> {
    exporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new BatchSpanProcessor(exporter, { scheduledDelayMillis: 60_000 })],
    });
    _resetPatchedStateForTesting();
    instrumentation = new OpenAIAgentsInstrumentation({ traceConfig });
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
    instrumentation.manuallyInstrument(
      { ...agents } as unknown as Parameters<OpenAIAgentsInstrumentation["manuallyInstrument"]>[0],
    );
    const modelProvider = new agents.OpenAIProvider({ apiKey: "sk-test-not-a-real-key", baseURL, useResponses: true });
    const runner = new agents.Runner({ modelProvider, workflowName: "Echo workflow" });
    const agent = new agents.Agent({ name: "Echo", instructions: INSTRUCTIONS_SENTINEL, model: "gpt-4.1-mini" });
    await runner.run(agent, "hello");
    await agents.getGlobalTraceProvider().forceFlush();
    await provider.forceFlush();
    return exporter.getFinishedSpans();
  }

  const dump = (spans: ReadableSpan[]) =>
    JSON.stringify(spans.map((s) => ({ attributes: s.attributes, events: s.events, links: s.links })));

  it("records the response when nothing is hidden (control)", async () => {
    const spans = await run();
    expect(spans.length).toBeGreaterThan(0);
    expect(dump(spans)).toContain(OUTPUT_SENTINEL);
    expect(spans.some((s) => typeof s.attributes["raw.output"] === "string")).toBe(true);
  });

  it("rawOutputOmitsRequestInstructionsUnderInputSettings (hideInputs)", async () => {
    const spans = await run({ hideInputs: true });
    expect(spans.length).toBeGreaterThan(0);
    const all = dump(spans);
    expect(all).not.toContain(INSTRUCTIONS_SENTINEL);
    expect(all).not.toContain(TOOL_SENTINEL);
  });
});
