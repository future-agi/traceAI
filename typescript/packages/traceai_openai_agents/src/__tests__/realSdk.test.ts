/**
 * Compatibility test against the real @openai/agents SDK.
 *
 * Runs a two-agent workflow (tool call, then handoff, then final answer) with
 * the SDK's own Runner and OpenAI models. The models talk to an in-process fake
 * OpenAI server on 127.0.0.1, so the test needs no API key and no network.
 * Both model paths are covered: the Responses API (response spans) and Chat
 * Completions (generation spans).
 */

import * as http from "http";
import { AddressInfo } from "net";
// openai-agents.d.ts declares a minimal ambient "@openai/agents" module so the
// package builds without the optional peer installed. Take the SDK's own types
// from its dist entry point for this test, and load the module at runtime.
import type * as AgentsSDK from "@openai/agents/dist/index";
import {
  BatchSpanProcessor,
  InMemorySpanExporter,
  ReadableSpan,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  OpenAIAgentsInstrumentation,
  _resetPatchedStateForTesting,
} from "../instrumentation";

// eslint-disable-next-line @typescript-eslint/no-require-imports, @typescript-eslint/no-var-requires
const agents: typeof AgentsSDK = require("@openai/agents");

const USAGE = { input_tokens: 11, output_tokens: 7, total_tokens: 18 };

type Api = "responses" | "chat";

function responsesBody(output: unknown[]) {
  return {
    id: `resp_${Math.random().toString(36).slice(2)}`,
    object: "response",
    created_at: 1,
    status: "completed",
    model: "gpt-4.1-mini",
    output,
    usage: {
      ...USAGE,
      input_tokens_details: { cached_tokens: 0 },
      output_tokens_details: { reasoning_tokens: 0 },
    },
  };
}

function chatBody(message: Record<string, unknown>) {
  return {
    id: "chatcmpl_test",
    object: "chat.completion",
    created: 1,
    model: "gpt-4.1-mini",
    choices: [
      {
        index: 0,
        finish_reason: message.tool_calls ? "tool_calls" : "stop",
        message: { role: "assistant", content: null, ...message },
      },
    ],
    usage: {
      prompt_tokens: USAGE.input_tokens,
      completion_tokens: USAGE.output_tokens,
      total_tokens: USAGE.total_tokens,
    },
  };
}

/**
 * Scripted fake OpenAI server: 1st call -> get_weather tool call, 2nd call ->
 * handoff tool call, 3rd call -> final text answer.
 */
function startFakeOpenAI(): Promise<{ server: http.Server; baseURL: string; reset: () => void }> {
  let calls = 0;
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", () => {
      calls += 1;
      const body = JSON.parse(raw || "{}");
      const toolNames: string[] = (body.tools || []).map(
        (t: { name?: string; function?: { name?: string } }) => t.name || t.function?.name
      );
      const handoffTool =
        toolNames.find((name) => name && name.startsWith("transfer_to")) ||
        "transfer_to_Specialist";
      let payload: unknown;
      if (req.url?.endsWith("/responses")) {
        if (calls === 1) {
          payload = responsesBody([
            {
              type: "function_call",
              id: "fc_1",
              call_id: "call_1",
              name: "get_weather",
              arguments: '{"city":"Paris"}',
              status: "completed",
            },
          ]);
        } else if (calls === 2) {
          payload = responsesBody([
            {
              type: "function_call",
              id: "fc_2",
              call_id: "call_2",
              name: handoffTool,
              arguments: "{}",
              status: "completed",
            },
          ]);
        } else {
          payload = responsesBody([
            {
              type: "message",
              id: "msg_1",
              role: "assistant",
              status: "completed",
              content: [{ type: "output_text", text: "It is sunny in Paris.", annotations: [] }],
            },
          ]);
        }
      } else if (calls === 1) {
        payload = chatBody({
          tool_calls: [
            { id: "call_1", type: "function", function: { name: "get_weather", arguments: '{"city":"Paris"}' } },
          ],
        });
      } else if (calls === 2) {
        payload = chatBody({
          tool_calls: [{ id: "call_2", type: "function", function: { name: handoffTool, arguments: "{}" } }],
        });
      } else {
        payload = chatBody({ content: "It is sunny in Paris." });
      }
      // No keep-alive: pooled client sockets would outlive the test.
      res.writeHead(200, { "content-type": "application/json", connection: "close" });
      res.end(JSON.stringify(payload));
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({ server, baseURL: `http://127.0.0.1:${port}/v1`, reset: () => (calls = 0) });
    });
  });
}

function buildAgents() {
  // A plain JSON schema keeps the test free of a direct zod dependency.
  const getWeather = agents.tool({
    name: "get_weather",
    description: "Get the weather for a city",
    parameters: {
      type: "object",
      properties: { city: { type: "string" } },
      required: ["city"],
      additionalProperties: false,
    },
    strict: true,
    execute: async (input: unknown) => `Sunny in ${(input as { city: string }).city}`,
  });
  const specialist = new agents.Agent({
    name: "Specialist",
    instructions: "Answer the user.",
    model: "gpt-4.1-mini",
    modelSettings: { temperature: 0.2 },
  });
  const triage = new agents.Agent({
    name: "Triage",
    instructions: "Route the user.",
    model: "gpt-4.1-mini",
    tools: [getWeather],
    handoffs: [specialist],
    modelSettings: { temperature: 0.1 },
  });
  return { triage };
}

function byId(spans: ReadableSpan[]) {
  return new Map(spans.map((s) => [s.spanContext().spanId, s]));
}

function parentOf(span: ReadableSpan, index: Map<string, ReadableSpan>) {
  const parentId = span.parentSpanId;
  return parentId ? index.get(parentId) : undefined;
}

/** Nearest ancestor with the given fi.span.kind. */
function ancestorOfKind(span: ReadableSpan, kind: string, index: Map<string, ReadableSpan>) {
  let current = parentOf(span, index);
  while (current) {
    if (current.attributes["fi.span.kind"] === kind) return current;
    current = parentOf(current, index);
  }
  return undefined;
}

describe("@openai/agents real SDK compatibility", () => {
  let fake: Awaited<ReturnType<typeof startFakeOpenAI>>;
  let exporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  let instrumentation: OpenAIAgentsInstrumentation;

  beforeAll(async () => {
    fake = await startFakeOpenAI();
    // The SDK disables tracing when NODE_ENV is "test".
    agents.setTracingDisabled(false);
  });

  afterAll(async () => {
    fake.server.closeAllConnections();
    await new Promise((resolve) => fake.server.close(resolve));
  });

  beforeEach(() => {
    fake.reset();
    exporter = new InMemorySpanExporter();
    // A long batch delay: spans only reach the exporter on flush or shutdown.
    provider = new NodeTracerProvider({
      spanProcessors: [new BatchSpanProcessor(exporter, { scheduledDelayMillis: 60_000 })],
    });
    _resetPatchedStateForTesting();
    instrumentation = new OpenAIAgentsInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
    instrumentation.manuallyInstrument(
      { ...agents } as unknown as Parameters<OpenAIAgentsInstrumentation["manuallyInstrument"]>[0]
    );
  });

  afterEach(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  async function runWorkflow(api: Api) {
    const modelProvider = new agents.OpenAIProvider({
      apiKey: "sk-test-not-a-real-key",
      baseURL: fake.baseURL,
      useResponses: api === "responses",
    });
    const runner = new agents.Runner({ modelProvider, workflowName: "Weather workflow" });
    const { triage } = buildAgents();
    const result = await runner.run(triage, "What is the weather in Paris?");
    expect(result.finalOutput).toBe("It is sunny in Paris.");
    await agents.getGlobalTraceProvider().forceFlush();
  }

  function assertSharedTree(spans: ReadableSpan[]) {
    const index = byId(spans);

    // One trace, one root span named after the workflow.
    const traceIds = new Set(spans.map((s) => s.spanContext().traceId));
    expect(traceIds.size).toBe(1);
    const roots = spans.filter((s) => !s.parentSpanId);
    expect(roots).toHaveLength(1);
    const root = roots[0];
    expect(root.name).toBe("Weather workflow");
    expect(root.attributes["fi.span.kind"]).toBe("AGENT");
    expect(root.attributes["output.value"]).toBe("It is sunny in Paris.");

    // Agent spans for both agents; the handoff target records its parent node.
    const triage = spans.find((s) => s.name === "Triage");
    const specialist = spans.find((s) => s.name === "Specialist");
    expect(triage?.attributes["fi.span.kind"]).toBe("AGENT");
    expect(triage?.attributes["graph.node.id"]).toBe("Triage");
    expect(specialist?.attributes["graph.node.id"]).toBe("Specialist");
    expect(specialist?.attributes["graph.node.parent_id"]).toBe("Triage");

    // Tool span under the Triage agent, with input and output.
    const tool = spans.find((s) => s.name === "get_weather");
    expect(tool?.attributes["fi.span.kind"]).toBe("TOOL");
    expect(tool?.attributes["tool.name"]).toBe("get_weather");
    expect(String(tool?.attributes["input.value"])).toContain("Paris");
    expect(tool?.attributes["output.value"]).toBe("Sunny in Paris");
    expect(ancestorOfKind(tool!, "AGENT", index)?.name).toBe("Triage");

    // Handoff span under the Triage agent.
    const handoff = spans.find((s) => s.name === "handoff to Specialist");
    expect(handoff).toBeDefined();
    expect(ancestorOfKind(handoff!, "AGENT", index)?.name).toBe("Triage");

    // Every span ended cleanly and reaches the root.
    for (const span of spans) {
      expect(span.status.code).not.toBe(2);
      if (span !== root) {
        let top: ReadableSpan = span;
        while (parentOf(top, index)) top = parentOf(top, index)!;
        expect(top).toBe(root);
      }
    }
    return { root };
  }

  it("traces a Responses API run: agents, tool, handoff and response spans", async () => {
    await runWorkflow("responses");

    // Nothing is exported until the tracer provider flushes.
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await provider.forceFlush();
    const spans = exporter.getFinishedSpans();

    const { root } = assertSharedTree(spans);
    expect(root.attributes["input.value"]).toContain("What is the weather in Paris?");

    const llmSpans = spans.filter((s) => s.attributes["fi.span.kind"] === "LLM");
    expect(llmSpans).toHaveLength(3);
    for (const llm of llmSpans) {
      expect(llm.name).toBe("response");
      expect(llm.attributes["gen_ai.request.model"]).toBe("gpt-4.1-mini");
      expect(llm.attributes["gen_ai.usage.input_tokens"]).toBe(11);
      expect(llm.attributes["gen_ai.usage.output_tokens"]).toBe(7);
      expect(llm.attributes["gen_ai.usage.total_tokens"]).toBe(18);
      expect(String(llm.attributes["input.value"])).toContain("What is the weather in Paris?");
    }
    expect(llmSpans.map((s) => s.attributes["output.value"])).toContain("It is sunny in Paris.");
  });

  it("traces a Chat Completions run: generation spans carry model, params and tokens", async () => {
    await runWorkflow("chat");
    await provider.forceFlush();
    const spans = exporter.getFinishedSpans();

    assertSharedTree(spans);

    const llmSpans = spans.filter((s) => s.attributes["fi.span.kind"] === "LLM");
    expect(llmSpans).toHaveLength(3);
    for (const llm of llmSpans) {
      expect(llm.name).toBe("generation");
      expect(llm.attributes["gen_ai.request.model"]).toBe("gpt-4.1-mini");
      expect(String(llm.attributes["gen_ai.request.parameters"])).toContain("temperature");
      expect(llm.attributes["gen_ai.usage.input_tokens"]).toBe(11);
      expect(llm.attributes["gen_ai.usage.output_tokens"]).toBe(7);
    }
    expect(llmSpans.map((s) => s.attributes["output.value"])).toContain("It is sunny in Paris.");
  });

  it("delivers every span when the tracer provider shuts down", async () => {
    await runWorkflow("responses");
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    // Keep a reference: shutdown() resets the in-memory exporter afterwards.
    const exported: ReadableSpan[] = [];
    const originalExport = exporter.export.bind(exporter);
    exporter.export = (batch, done) => {
      exported.push(...batch);
      originalExport(batch, done);
    };
    await provider.shutdown();
    expect(exported.length).toBeGreaterThanOrEqual(7);
    expect(exported.filter((s) => !s.parentSpanId)).toHaveLength(1);
  });
  // Rebuild the instrumentation the way the README shows it: construct it,
  // call setTracerProvider(), then manuallyInstrument(), with no enable() call.
  function reinstrument(config: ConstructorParameters<typeof OpenAIAgentsInstrumentation>[0] = {}) {
    instrumentation.disable();
    _resetPatchedStateForTesting();
    instrumentation = new OpenAIAgentsInstrumentation(config);
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(
      { ...agents } as unknown as Parameters<OpenAIAgentsInstrumentation["manuallyInstrument"]>[0]
    );
  }

  function expectContentHidden(spans: ReadableSpan[]) {
    expect(spans.length).toBeGreaterThanOrEqual(7);
    let redacted = 0;
    for (const span of spans) {
      for (const key of ["input.value", "output.value"]) {
        const value = span.attributes[key];
        if (value === undefined) continue;
        expect(value).toBe("__REDACTED__");
        redacted += 1;
      }
    }
    // The root, response and tool spans all carried input or output.
    expect(redacted).toBeGreaterThanOrEqual(4);
  }

  it("exports to the provider set with setTracerProvider() after construction", async () => {
    reinstrument();
    await runWorkflow("responses");
    await provider.forceFlush();
    assertSharedTree(exporter.getFinishedSpans());
  });

  it("hides span input and output with traceConfig", async () => {
    reinstrument({ traceConfig: { hideInputs: true, hideOutputs: true } });
    await runWorkflow("responses");
    await provider.forceFlush();
    expectContentHidden(exporter.getFinishedSpans());
  });

  it("hides span input and output with FI_HIDE_INPUTS / FI_HIDE_OUTPUTS", async () => {
    process.env.FI_HIDE_INPUTS = "true";
    process.env.FI_HIDE_OUTPUTS = "true";
    try {
      reinstrument();
    } finally {
      delete process.env.FI_HIDE_INPUTS;
      delete process.env.FI_HIDE_OUTPUTS;
    }
    await runWorkflow("responses");
    await provider.forceFlush();
    expectContentHidden(exporter.getFinishedSpans());
  });
});
