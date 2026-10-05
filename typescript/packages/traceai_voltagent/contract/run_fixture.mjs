// Contract fixture for @traceai/voltagent (TH-8239).
//
// Runs a real VoltAgent 2.x agent against an AI SDK mock model (no vendor call), with the BUILT
// package's FIVoltAgentSpanProcessor appended to ObservabilityConfig.spanProcessors next to a
// recording processor (standing in for VoltOps / any user processor). The real @traceai/fi-core
// exporter posts to FI_BASE_URL + /tracer/v1/traces (the shared harness Receiver in tests).
//
// Env: FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME (placeholders in tests),
//      JOURNEY = tools | subagent | burst | collector-down, CAPTURE_CONTENT = "true" to opt in.
// Prints one line "RESULT_JSON:{...}" on stdout.
import { ProjectType, register } from "@traceai/fi-core";
import { FIVoltAgentSpanProcessor } from "@traceai/voltagent";
import { Agent, VoltAgent, VoltAgentObservability, createTool } from "@voltagent/core";
import { MockLanguageModelV3, convertArrayToReadableStream } from "ai/test";
import { z } from "zod";

const JOURNEY = process.env.JOURNEY ?? "tools";
const CAPTURE = process.env.CAPTURE_CONTENT === "true";
const BURST = 20;

export const MARKERS = {
  prompt: "SECRET_PROMPT_MARKER",
  instructions: "SECRET_INSTRUCTIONS_MARKER",
  toolArg: "SECRET_TOOL_ARG_MARKER",
  toolResult: "SECRET_TOOL_RESULT_MARKER",
  answer: "SECRET_ANSWER_MARKER",
  subtask: "SECRET_SUBTASK_MARKER",
};

const modelCalls = [];

function scriptedModel(modelId, steps) {
  let index = 0;
  const next = () => {
    const step = steps[index % steps.length];
    index += 1;
    modelCalls.push({ modelId, input: step.input, output: step.output });
    const usage = {
      inputTokens: { total: step.input, noCache: step.input, cacheRead: undefined, cacheWrite: undefined },
      outputTokens: { total: step.output, text: step.output, reasoning: undefined },
    };
    return { step, usage };
  };
  return new MockLanguageModelV3({
    provider: "mock-provider",
    modelId,
    doGenerate: async () => {
      const { step, usage } = next();
      if (step.text !== undefined) {
        return { content: [{ type: "text", text: step.text }], finishReason: { unified: "stop", raw: "stop" }, usage, warnings: [] };
      }
      return {
        content: step.calls.map((call) => ({ type: "tool-call", toolCallId: call.id, toolName: call.name, input: JSON.stringify(call.args) })),
        finishReason: { unified: "tool-calls", raw: "tool_calls" },
        usage,
        warnings: [],
      };
    },
    doStream: async () => {
      const { step, usage } = next();
      const parts = [{ type: "stream-start", warnings: [] }];
      if (step.text !== undefined) {
        parts.push(
          { type: "text-start", id: "t1" },
          { type: "text-delta", id: "t1", delta: step.text },
          { type: "text-end", id: "t1" },
          { type: "finish", finishReason: { unified: "stop", raw: "stop" }, usage },
        );
      } else {
        for (const call of step.calls) {
          parts.push({ type: "tool-call", toolCallId: call.id, toolName: call.name, input: JSON.stringify(call.args) });
        }
        parts.push({ type: "finish", finishReason: { unified: "tool-calls", raw: "tool_calls" }, usage });
      }
      return { stream: convertArrayToReadableStream(parts) };
    },
  });
}

const toolSteps = () => [
  {
    calls: [
      { id: "call-weather", name: "get_weather", args: { city: MARKERS.toolArg } },
      { id: "call-explode", name: "explode", args: { x: 1 } },
    ],
    input: 11,
    output: 7,
  },
  { text: MARKERS.answer, input: 13, output: 5 },
];

const weatherTool = createTool({
  name: "get_weather",
  description: "Look up the weather for a city",
  parameters: z.object({ city: z.string() }),
  execute: async ({ city }) => ({ forecast: `${MARKERS.toolResult} sunny in ${city}` }),
});
const explodingTool = createTool({
  name: "explode",
  description: "Always fails",
  parameters: z.object({ x: z.number() }),
  execute: async () => {
    throw new Error("tool exploded");
  },
});

// Stands in for VoltOps or any other processor the user already has in the array.
const recorded = [];
const recorder = {
  onStart() {},
  onEnd(span) {
    recorded.push({ name: span.name, spanId: span.spanContext().spanId, traceId: span.spanContext().traceId });
  },
  forceFlush: async () => {},
  shutdown: async () => {},
};

// setGlobalTracerProvider: false, so VoltAgent's own provider stays the global one and every
// VoltAgent span reaches ObservabilityConfig.spanProcessors.
const tracerProvider = register({
  projectName: process.env.FI_PROJECT_NAME,
  projectType: ProjectType.OBSERVE,
  setGlobalTracerProvider: false,
});
const processor = new FIVoltAgentSpanProcessor({ tracerProvider, captureContent: CAPTURE });
const observability = new VoltAgentObservability({ spanProcessors: [recorder, processor] });

const result = { journey: JOURNEY, captureContent: CAPTURE };
const started = Date.now();

if (JOURNEY === "subagent") {
  const researcher = new Agent({
    name: "researcher",
    instructions: MARKERS.instructions,
    model: scriptedModel("mockai/child-model", [{ text: "child answer", input: 5, output: 3 }]),
  });
  const supervisor = new Agent({
    name: "supervisor",
    instructions: MARKERS.instructions,
    model: scriptedModel("mockai/supervisor-model", [
      {
        calls: [{ id: "call-delegate", name: "delegate_task", args: { task: MARKERS.subtask, targetAgents: ["researcher"] } }],
        input: 20,
        output: 9,
      },
      { text: "final", input: 30, output: 4 },
    ]),
    subAgents: [researcher],
  });
  new VoltAgent({ agents: { supervisor, researcher }, observability, checkDependencies: false });
  const out = await supervisor.generateText(MARKERS.prompt, { conversationId: "conv-sub", userId: "user-1", maxSteps: 4 });
  result.text = out.text;
} else {
  const agent = new Agent({
    name: "assistant",
    instructions: MARKERS.instructions,
    model: scriptedModel("mockai/mock-model-1", toolSteps()),
    tools: [weatherTool, explodingTool],
  });
  new VoltAgent({ agents: { assistant: agent }, observability, checkDependencies: false });
  const runs = JOURNEY === "burst" ? BURST : 1;
  const texts = [];
  for (let i = 0; i < runs; i += 1) {
    const out = await agent.generateText(MARKERS.prompt, { conversationId: `conv-${i === 0 ? "123" : i}`, userId: "user-9", maxSteps: 4 });
    texts.push(out.text);
  }
  result.text = texts[0];
  result.texts = texts;
  result.runs = runs;
}
result.agentMs = Date.now() - started;

// VoltAgent ends some memory spans in the background after generateText resolves; a request
// handler that returns right away would flush before they end. Wait until the span stream is quiet.
for (let seen = -1; seen !== recorded.length; ) {
  seen = recorded.length;
  await new Promise((resolve) => setTimeout(resolve, 100));
}

// Serverless-style: flush before returning. forceFlush/shutdown never throw.
await processor.forceFlush();
result.flushMs = Date.now() - started;
const traceIds = [...new Set(recorded.map((span) => span.traceId))];
result.recorded = recorded;
result.traceIds = traceIds;
const storedCounts = [];
for (const traceId of traceIds) storedCounts.push((await observability.getTraceFromStorage(traceId)).length);
result.storedCount = storedCounts.reduce((sum, count) => sum + count, 0);
result.modelCalls = modelCalls;
await processor.shutdown();
await tracerProvider.shutdown().catch(() => undefined);
result.totalMs = Date.now() - started;
process.stdout.write(`\nRESULT_JSON:${JSON.stringify(result)}\n`);
process.exit(0);
