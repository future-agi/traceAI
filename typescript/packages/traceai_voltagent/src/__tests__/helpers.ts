/* Shared fixtures for the jest tests: a scripted AI SDK mock model and VoltAgent wiring. */
import { context, propagation, trace } from "@opentelemetry/api";
import { resourceFromAttributes } from "@opentelemetry/resources";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  type ReadableSpan,
  SimpleSpanProcessor,
  type SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import {
  Agent,
  InMemoryStorageAdapter,
  Memory,
  VoltAgent,
  VoltAgentObservability,
  createOutputGuardrail,
  createTool,
  createWorkflowChain,
} from "@voltagent/core";
import { MockLanguageModelV3, convertArrayToReadableStream } from "ai/test";
import { z } from "zod";

export const MARKERS = {
  prompt: "SECRET_PROMPT_MARKER",
  instructions: "SECRET_INSTRUCTIONS_MARKER",
  toolArg: "SECRET_TOOL_ARG_MARKER",
  toolResult: "SECRET_TOOL_RESULT_MARKER",
  answer: "SECRET_ANSWER_MARKER",
  subtask: "SECRET_SUBTASK_MARKER",
  suspendData: "SECRET_SUSPEND_DATA_MARKER",
  resumeData: "SECRET_RESUME_DATA_MARKER",
} as const;

export type Step =
  | { kind: "text"; text: string; input: number; output: number }
  | { kind: "tools"; calls: Array<{ id: string; name: string; args: unknown }>; input: number; output: number }
  | { kind: "error"; message: string };

/**
 * An AI SDK V3 mock model that plays `steps` in order (generate or stream) and records the
 * usage it reported. VoltAgent subagents call streamText, so both paths are scripted.
 */
export function scriptedModel(modelId: string, steps: Step[]) {
  const reported: Array<{ input: number; output: number }> = [];
  let index = 0;
  const next = () => {
    const step = steps[Math.min(index, steps.length - 1)];
    index += 1;
    if (step.kind === "error") throw new Error(step.message);
    reported.push({ input: step.input, output: step.output });
    const usage = {
      inputTokens: { total: step.input, noCache: step.input, cacheRead: undefined, cacheWrite: undefined },
      outputTokens: { total: step.output, text: step.output, reasoning: undefined },
    };
    return { step, usage };
  };
  const model = new MockLanguageModelV3({
    provider: "mock-provider",
    modelId,
    doStream: async () => {
      const { step, usage } = next();
      const parts: unknown[] = [{ type: "stream-start", warnings: [] }];
      if (step.kind === "text") {
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
      return { stream: convertArrayToReadableStream(parts) as never };
    },
    doGenerate: async () => {
      const { step, usage } = next();
      if (step.kind === "text") {
        return {
          content: [{ type: "text" as const, text: step.text }],
          finishReason: { unified: "stop" as const, raw: "stop" },
          usage,
          warnings: [],
        };
      }
      return {
        content: step.calls.map((call) => ({
          type: "tool-call" as const,
          toolCallId: call.id,
          toolName: call.name,
          input: JSON.stringify(call.args),
        })),
        finishReason: { unified: "tool-calls" as const, raw: "tool_calls" },
        usage,
        warnings: [],
      };
    },
  });
  return { model, reported };
}

export const weatherTool = createTool({
  name: "get_weather",
  description: "Look up the weather for a city",
  parameters: z.object({ city: z.string() }),
  execute: async ({ city }: { city: string }) => ({ forecast: `${MARKERS.toolResult} sunny in ${city}` }),
});

export const explodingTool = createTool({
  name: "explode",
  description: "Always fails",
  parameters: z.object({ x: z.number() }),
  execute: async () => {
    throw new Error("tool exploded");
  },
});

/** Two model calls: the first asks for both tools (one fails), the second answers. */
export function toolCallingSteps(): Step[] {
  return [
    {
      kind: "tools",
      calls: [
        { id: "call-weather", name: "get_weather", args: { city: MARKERS.toolArg } },
        { id: "call-explode", name: "explode", args: { x: 1 } },
      ],
      input: 11,
      output: 7,
    },
    { kind: "text", text: MARKERS.answer, input: 13, output: 5 },
  ];
}

/** Records every span VoltAgent hands to it, as VoltOps or any other user processor would. */
export class RecordingProcessor implements SpanProcessor {
  readonly spans: ReadableSpan[] = [];
  onStart(): void {}
  onEnd(span: ReadableSpan): void {
    this.spans.push(span);
  }
  async forceFlush(): Promise<void> {}
  async shutdown(): Promise<void> {}
}

/** Wait until no new span has reached `recorder` for `quietMs`. */
export async function settle(recorder: RecordingProcessor, quietMs = 100, maxMs = 5_000): Promise<void> {
  const deadline = Date.now() + maxMs;
  let seen = -1;
  while (seen !== recorder.spans.length && Date.now() < deadline) {
    seen = recorder.spans.length;
    await new Promise((resolve) => setTimeout(resolve, quietMs));
  }
}

export const FI_RESOURCE_ATTRIBUTES = {
  "service.name": "th8239-jest",
  project_name: "th8239-jest",
  project_type: "observe",
};

/** A stand-in for the provider register() returns: SDK 2.x BasicTracerProvider + in-memory exporter. */
export function fiProvider() {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes(FI_RESOURCE_ATTRIBUTES),
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  return { exporter, provider };
}

/** Wire agents (and workflows) into a fresh VoltAgentObservability whose spanProcessors are `processors`. */
export function voltagent(
  agents: Record<string, Agent>,
  processors: SpanProcessor[],
  workflows?: Record<string, ReturnType<typeof approvalWorkflow>>,
) {
  const observability = new VoltAgentObservability({ spanProcessors: processors });
  new VoltAgent({ agents, workflows, observability, checkDependencies: false });
  return observability;
}

/**
 * An output guardrail with a streaming handler that passes every chunk through. VoltAgent 2.11.0
 * then adds a `guardrail.stream.process` event per chunk carrying the chunk text
 * (output-guardrail-stream-runner.ts, `guardrail.chunk.text`).
 */
export function passThroughGuardrail() {
  return createOutputGuardrail({
    name: "pass-through",
    handler: async () => ({ pass: true }),
    streamHandler: ({ part }) => part,
  });
}

/**
 * A one-step workflow that suspends with `question` from its input and completes on resume.
 * VoltAgent 2.11.0 adds `workflow.suspended` (suspension.data, suspension.checkpoint) and
 * `workflow.resumed` (resume.data) events to the workflow root (workflow/open-telemetry/trace-context.ts).
 * The payloads come from the input, never from the step source, because the step source is
 * serialized into `workflow.stateSnapshot`.
 */
export function approvalWorkflow() {
  return createWorkflowChain({
    id: "approval",
    name: "approval",
    input: z.object({ amount: z.number(), question: z.string() }),
    result: z.object({ approved: z.boolean() }),
    memory: new Memory({ storage: new InMemoryStorageAdapter() }),
  }).andThen({
    id: "ask",
    suspendSchema: z.object({ question: z.string() }),
    resumeSchema: z.object({ approved: z.boolean(), note: z.string() }),
    execute: async ({ data, suspend, resumeData }) => {
      if (!resumeData) await suspend("needs approval", { question: data.question });
      return { approved: resumeData?.approved === true };
    },
  });
}

/** VoltAgentObservability registers its provider globally; reset between tests. */
export async function resetOtelGlobals(observability?: { shutdown(): Promise<void> }) {
  if (observability) await observability.shutdown().catch(() => undefined);
  trace.disable();
  context.disable();
  propagation.disable();
}

export const attrs = (span: ReadableSpan) => span.attributes as Record<string, unknown>;
export const byName = (spans: ReadableSpan[], name: string) => spans.filter((span) => span.name === name);
export function one(spans: ReadableSpan[], name: string): ReadableSpan {
  const found = byName(spans, name);
  if (found.length !== 1) {
    throw new Error(`expected one ${name}, got ${spans.map((span) => span.name).join(", ")}`);
  }
  return found[0];
}
export const parentId = (span: ReadableSpan) => span.parentSpanContext?.spanId;

/** Sum of the promoted input-token keys over every exported span (what Observe adds up). */
export function promotedInputTokens(spans: ReadableSpan[]): number {
  let total = 0;
  for (const span of spans) {
    const a = attrs(span);
    // One hot column per span: the collector reads one value per span, whichever key it prefers.
    const values = ["gen_ai.usage.input_tokens", "llm.usage.prompt_tokens", "llm.token_count.prompt"]
      .map((key) => a[key])
      .filter((value): value is number => typeof value === "number");
    if (values.length > 0) {
      if (new Set(values).size !== 1) throw new Error(`conflicting promoted input tokens on ${span.name}: ${values}`);
      total += values[0];
    }
  }
  return total;
}
