/**
 * Attribute sets copied from spans genkit 1.42.0 emitted in contract/inventory.mjs
 * (Genkit's own mockModel from `genkit/testing`, one tool). Only the prompt and
 * outputs were replaced with markers so content assertions can search for them.
 */
import type { Attributes, HrTime, SpanContext } from "@opentelemetry/api";
import type { GenkitReadableSpan, TimedEventLike } from "../processor";

export const PROMPT_MARKER = "SECRET_PROMPT_MARKER";
export const OUTPUT_MARKER = "SECRET_OUTPUT_MARKER";
export const TOOL_OUTPUT_MARKER = "SECRET_TOOL_OUTPUT_MARKER";
export const TRACE_ID = "0af7651916cd43dd8448eb211c80319c";

export const flowAttributes = (): Attributes => ({
  "genkit:type": "action",
  "genkit:metadata:subtype": "flow",
  "genkit:key": "/flow/qaFlow",
  "genkit:name": "qaFlow",
  "genkit:isRoot": true,
  "genkit:path": "/{qaFlow,t:flow}",
  "genkit:metadata:context": JSON.stringify({ auth: "<redacted>", headers: { "x-api-key": "SECRET_HEADER_MARKER" } }),
  "genkit:input": JSON.stringify(`  ${PROMPT_MARKER}  `),
  "genkit:output": JSON.stringify(`final ${OUTPUT_MARKER}`),
  "genkit:state": "success",
});

export const flowStepAttributes = (): Attributes => ({
  "genkit:type": "flowStep",
  "genkit:name": "prepare",
  "genkit:path": "/{qaFlow,t:flow}/{prepare,t:flowStep}",
  "genkit:output": JSON.stringify(PROMPT_MARKER),
  "genkit:state": "success",
});

export const generateAttributes = (): Attributes => ({
  "genkit:type": "util",
  "genkit:name": "generate",
  "genkit:path": "/{qaFlow,t:flow}/{generate,t:util}",
  "genkit:input": JSON.stringify({ model: "fixture/mock", messages: [{ role: "user", content: [{ text: PROMPT_MARKER }] }] }),
  // The generate span's output repeats the LAST turn's usage (inventory: 21/4/25 on both generate spans).
  "genkit:output": JSON.stringify({
    message: { role: "model", content: [{ text: OUTPUT_MARKER }] },
    finishReason: "stop",
    usage: { inputTokens: 21, outputTokens: 4, totalTokens: 25 },
  }),
  "genkit:state": "success",
});

export const modelAttributes = (usage: Record<string, number> | null = { inputTokens: 11, outputTokens: 3, totalTokens: 14 }): Attributes => ({
  "genkit:type": "action",
  "genkit:metadata:subtype": "model",
  "genkit:key": "/model/fixture/mock",
  "genkit:name": "fixture/mock",
  "genkit:path": "/{qaFlow,t:flow}/{generate,t:util}/{fixture/mock,t:action,s:model}",
  "genkit:input": JSON.stringify({ messages: [{ role: "user", content: [{ text: PROMPT_MARKER }] }], config: {} }),
  "genkit:output": JSON.stringify({
    message: { role: "model", content: [{ toolRequest: { name: "lookup", input: { id: 7 } } }] },
    finishReason: "stop",
    ...(usage ? { usage } : {}),
    latencyMs: 0.31,
  }),
  "genkit:state": "success",
});

export const toolAttributes = (): Attributes => ({
  "genkit:type": "action",
  "genkit:metadata:subtype": "tool",
  "genkit:key": "/tool/lookup",
  "genkit:name": "lookup",
  "genkit:path": "/{qaFlow,t:flow}/{generate,t:util}/{lookup,t:action,s:tool}",
  "genkit:input": JSON.stringify({ id: 7 }),
  "genkit:output": JSON.stringify(TOOL_OUTPUT_MARKER),
  "genkit:state": "success",
});

const T0: HrTime = [1_760_000_000, 0];
const T1: HrTime = [1_760_000_001, 500];

/** A span shaped like sdk-trace-base 1.25 (the version genkit 1.42.0's NodeSDK uses). */
export function v1Span(options: {
  name: string;
  attributes: Attributes;
  spanId: string;
  parentSpanId?: string;
  traceId?: string;
  status?: { code: number; message?: string };
  events?: TimedEventLike[];
}): GenkitReadableSpan & { resource: unknown } {
  const ctx: SpanContext = { traceId: options.traceId ?? TRACE_ID, spanId: options.spanId, traceFlags: 1 };
  return {
    name: options.name,
    kind: 0,
    spanContext: () => ctx,
    parentSpanId: options.parentSpanId,
    startTime: T0,
    endTime: T1,
    status: options.status ?? { code: 0 },
    attributes: options.attributes,
    links: [],
    events: options.events ?? [],
    duration: [1, 500],
    ended: true,
    resource: { attributes: { "service.name": "genkit-app" } },
    instrumentationLibrary: { name: "genkit-tracer", version: "v1" },
    droppedAttributesCount: 0,
    droppedEventsCount: 0,
    droppedLinksCount: 0,
  };
}
