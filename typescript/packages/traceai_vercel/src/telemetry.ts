import { Attributes, Context, Span, SpanKind, SpanStatusCode, Tracer, context, diag, trace, INVALID_SPAN_CONTEXT } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { getAttributesFromContext } from "@traceai/fi-core";
import type { FrameworkAttachmentOptions } from "@traceai/framework-attachments";
import { captureVercelInput, captureVercelOutput, captureVercelToolOutput } from "./media";

// Structural types deliberately avoid a runtime dependency on a particular AI SDK major.
type Event = Record<string, any>;
type Callback = (event: Event) => void;
export interface TelemetryIntegration {
  [name: string]: any;
  onStart: Callback;
  onEnd: Callback;
  executeLanguageModelCall: (event: Event & { execute: () => any }) => any;
  executeTool: (event: Event & { execute: () => any }) => any;
  withOperationContext: <T>(execute: () => T) => T;
  finishStream: (error?: unknown) => void;
}
interface Call {
  span: Span;
  ctx: Context;
  operation: string;
  inputs: boolean;
  outputs: boolean;
  models: Map<string, Span>;
  tools: Map<string, Span>;
}
const MAX_CALLS = 1024;

function guard<T>(fn: () => T): T | undefined {
  try { return fn(); } catch {
    try { diag.debug("@traceai/vercel: telemetry callback unavailable"); } catch { /* Diagnostics must also fail open. */ }
    return undefined;
  }
}

/** Bounded data-only serialization: no getters, toJSON, media bytes or embedding vectors. */
function json(value: unknown): string | undefined {
  let budget = 2048;
  let characters = 65536;
  function copy(item: unknown, depth: number): unknown {
    if (--budget < 0 || depth > 12 || characters <= 0) return "<truncated>";
    if (typeof item === "string") {
      const text = item.slice(0, characters);
      characters -= text.length;
      return text;
    }
    if (item == null || typeof item === "number" || typeof item === "boolean") return item;
    if (typeof item !== "object") return `<${typeof item}>`;
    if (ArrayBuffer.isView(item) || item instanceof ArrayBuffer) return "<binary omitted>";
    const result: Record<string, unknown> | unknown[] = Array.isArray(item) ? [] : Object.create(null);
    for (const key of Object.keys(item).slice(0, 128)) {
      if (budget <= 0 || characters <= 0) break;
      if (key.length > 256 || /^(data|bytes|image|audio|file|embedding|embeddings)$/i.test(key)) continue;
      const descriptor = Object.getOwnPropertyDescriptor(item, key);
      if (!descriptor || !("value" in descriptor)) continue;
      if (Array.isArray(result)) {
        if (/^\d+$/.test(key)) result.push(copy(descriptor.value, depth + 1));
      } else result[key] = copy(descriptor.value, depth + 1);
    }
    return result;
  }
  return JSON.stringify(copy(value, 0));
}

function attributes(event: Event, call?: Call): Attributes {
  const attrs: Attributes = {};
  if (typeof event.modelId === "string") attrs["gen_ai.request.model"] = event.modelId;
  if (typeof event.provider === "string") attrs["gen_ai.provider.name"] = event.provider;
  if (typeof event.responseId === "string") attrs["gen_ai.response.id"] = event.responseId;
  if (typeof event.finishReason === "string") attrs["gen_ai.response.finish_reasons"] = [event.finishReason];
  const inputs = call?.inputs ?? event.recordInputs !== false;
  const outputs = call?.outputs ?? event.recordOutputs !== false;
  if (inputs) {
    const input = event.promptMessages ?? event.messages ?? event.prompt ?? event.values ?? event.value;
    if (input !== undefined) {
      attrs["input.value"] = typeof input === "string" ? input : json(input);
      attrs["input.mime_type"] = typeof input === "string" ? "text/plain" : "application/json";
    }
    if (Array.isArray(event.messages)) attrs["gen_ai.input.messages"] = json(event.messages);
    if (Array.isArray(event.tools)) event.tools.slice(0, 128).forEach((tool: unknown, i: number) => {
      attrs[`gen_ai.tool.definitions.${i}.tool.json_schema`] = json(tool);
    });
  }
  if (outputs) {
    const text = event.text ?? event.objectText ?? (Array.isArray(event.content)
      ? event.content.filter((part: Event) => part.type === "text").map((part: Event) => part.text).join("") : undefined);
    if (typeof text === "string") { attrs["output.value"] = text; attrs["output.mime_type"] = "text/plain"; }
    else if (event.object !== undefined) { attrs["output.value"] = json(event.object); attrs["output.mime_type"] = "application/json"; }
    if (Array.isArray(event.content)) {
      const tools = event.content.filter((part: Event) => part.type === "tool-call");
      if (tools.length) attrs["gen_ai.output.messages"] = json([{ role: "assistant", tool_calls: tools.map((tool: Event) => ({
        id: tool.toolCallId, function: { name: tool.toolName, arguments: json(tool.input ?? tool.args) },
      })) }]);
    }
  }
  // Only model spans get token counts; never double-count the enclosing chain.
  const usage = event.usage;
  if (usage && call) {
    const tokens: Record<string, unknown> = {
      input_tokens: usage.inputTokens ?? usage.tokens,
      output_tokens: usage.outputTokens,
      total_tokens: usage.totalTokens ?? usage.tokens,
      cache_read_tokens: usage.inputTokenDetails?.cacheReadTokens,
      cache_write_tokens: usage.inputTokenDetails?.cacheWriteTokens,
      "output_tokens.reasoning": usage.outputTokenDetails?.reasoningTokens,
    };
    for (const [key, value] of Object.entries(tokens)) {
      if (typeof value === "number" && Number.isFinite(value)) attrs[`gen_ai.usage.${key}`] = value;
    }
  }
  const first = event.embeddings?.[0] ?? event.embedding;
  if (Array.isArray(first)) attrs["gen_ai.embeddings.dimension.count"] = first.length;
  return Object.fromEntries(Object.entries(attrs).filter(([, value]) => value !== undefined));
}

function run<T>(ctx: Context | undefined, execute: () => T): T {
  if (!ctx) return execute();
  let entered = false;
  try { return context.with(ctx, () => { entered = true; return execute(); }); }
  catch (error) { if (entered) throw error; return execute(); }
}

/** v7 dispatcher callbacks, shared by the global registry and isolated per-call adapters. */
export function createTelemetryIntegration(getTracer: () => Tracer, operation?: string, media: FrameworkAttachmentOptions = {}): TelemetryIntegration {
  const calls = new Map<string, Call>();
  const binding = (call: Call): FrameworkAttachmentOptions => ({ ...media, capture: { ...media.capture,
    recordInputs: call.inputs && media.capture?.recordInputs !== false,
    recordOutputs: call.outputs && media.capture?.recordOutputs !== false } });
  let operationParent: Context | undefined;
  let operationSpan: Span | undefined;
  const modelKey = (event: Event) => typeof event.embedCallId === "string" ? event.embedCallId : "model";
  const rootKind = (name: string) => name.startsWith("ai.embed") ? "EMBEDDING" : name.endsWith("Object") ? "LLM" : "CHAIN";
  const start = (id: string, event: Event): Call => {
    const parent = operationParent ?? context.active();
    const kind = rootKind(event.operationId);
    const span = getTracer().startSpan(event.operationId, { kind: kind === "CHAIN" ? SpanKind.INTERNAL : SpanKind.CLIENT, attributes: {
      ...getAttributesFromContext(parent), ...attributes(event), "gen_ai.span.kind": kind, "fi.span.kind": kind,
      "gen_ai.operation.name": event.operationId.slice(3), "traceai.capture.layer": "library",
    } }, parent);
    const call: Call = { span, ctx: trace.setSpan(parent, span), operation: event.operationId,
      inputs: event.recordInputs !== false, outputs: event.recordOutputs !== false, models: new Map(), tools: new Map() };
    calls.set(id, call);
    return call;
  };
  const fail = (span: Span, error: unknown) => {
    span.setStatus({ code: SpanStatusCode.ERROR });
    // The export boundary handles credential scrubbing; do not invoke arbitrary error getters here.
    const type = error instanceof Error ? Object.getPrototypeOf(error)?.constructor?.name : typeof error;
    if (typeof type === "string") span.setAttribute("error.type", type);
  };
  const finish = (id: string, event: Event = {}, incomplete = false) => {
    const call = calls.get(id);
    if (!call) return;
    calls.delete(id);
    for (const child of [...call.models.values(), ...call.tools.values()]) {
      guard(() => { if (event.error !== undefined) fail(child, event.error); if (incomplete) child.setAttribute("traceai.wire.incomplete", true); child.end(); });
    }
    guard(() => {
      const attrs = attributes(event, call);
      if (rootKind(call.operation) === "CHAIN") for (const key of Object.keys(attrs)) if (key.startsWith("gen_ai.usage.")) delete attrs[key];
      call.span.setAttributes(attrs);
      if (event.error !== undefined) fail(call.span, event.error);
      if (incomplete) call.span.setAttribute("traceai.wire.incomplete", true);
      call.span.end();
    });
  };
  const startModel = (event: Event) => {
    const call = calls.get(event.callId);
    if (!call) return;
    if (rootKind(call.operation) !== "CHAIN") {
      captureVercelInput(binding(call), call.span, event);
      call.span.setAttributes(attributes(event, call));
      return;
    }
    const key = modelKey(event);
    const prior = call.models.get(key);
    if (prior) { prior.setAttribute("traceai.wire.incomplete", true); prior.end(); }
    const name = `${call.operation}.${call.operation.includes("stream") ? "doStream" : "doGenerate"}`;
    const span = getTracer().startSpan(name, { kind: SpanKind.CLIENT, attributes: {
      ...getAttributesFromContext(call.ctx), ...attributes(event, call), "gen_ai.span.kind": "LLM", "fi.span.kind": "LLM", "traceai.capture.layer": "library",
    } }, call.ctx);
    captureVercelInput(binding(call), span, event);
    call.models.set(key, span);
  };
  const endModel = (event: Event) => {
    const call = calls.get(event.callId);
    if (call && rootKind(call.operation) !== "CHAIN") {
      captureVercelOutput(binding(call), call.span, event);
      call.span.setAttributes(attributes(event, call));
      return;
    }
    const span = call?.models.get(modelKey(event));
    if (!span || !call) return;
    call.models.delete(modelKey(event));
    // Capture the end before normalization/attribute serialization work in this adapter.
    const end = Date.now();
    captureVercelOutput(binding(call), span, event);
    span.setAttributes(attributes(event, call));
    span.end(end);
  };
  const integration: TelemetryIntegration = {
    onStart(event) { guard(() => {
      if (isTracingSuppressed(context.active()) || typeof event.callId !== "string" || !/^ai\.(generateText|streamText|generateObject|streamObject|embed|embedMany)$/.test(event.operationId)) return;
      if (calls.has(event.callId)) return;
      if (calls.size >= MAX_CALLS) finish(calls.keys().next().value!, {}, true);
      const call = start(event.callId, event);
      if (operationParent) operationSpan = call.span;
    }); },
    onLanguageModelCallStart(event: Event) { guard(() => startModel(event)); },
    onLanguageModelCallEnd(event: Event) { guard(() => endModel(event)); },
    onObjectStepStart(event: Event) { guard(() => startModel(event)); },
    onObjectStepEnd(event: Event) { guard(() => endModel(event)); },
    onToolExecutionStart(event: Event) { guard(() => {
      const call = calls.get(event.callId);
      if (!call) return;
      const tool = event.toolCall;
      if (typeof tool?.toolCallId !== "string") return;
      const span = getTracer().startSpan(`ai.toolCall ${tool.toolName}`, { attributes: {
        ...getAttributesFromContext(call.ctx), "gen_ai.span.kind": "TOOL", "fi.span.kind": "TOOL", "traceai.capture.layer": "library", "gen_ai.tool.name": tool.toolName,
        "gen_ai.tool.call.id": tool.toolCallId,
        ...(call.inputs ? { "input.value": json(tool.input ?? tool.args), "input.mime_type": "application/json" } : {}),
      } }, call.ctx);
      call.tools.set(tool.toolCallId, span);
    }); },
    onToolExecutionEnd(event: Event) { guard(() => {
      const call = calls.get(event.callId);
      const span = call?.tools.get(event.toolCall?.toolCallId);
      if (!call || !span) return;
      call.tools.delete(event.toolCall.toolCallId);
      const end = Date.now();
      const output = event.toolOutput;
      if (output?.type === "tool-error" || event.success === false) fail(span, output?.error ?? event.error);
      else if (call.outputs) {
        captureVercelToolOutput(binding(call), span, output?.output ?? event.output);
        span.setAttributes({ "output.value": json(output?.output ?? event.output), "output.mime_type": "application/json" });
      }
      span.end(end);
    }); },
    onEnd(event) { guard(() => finish(event.callId, event)); },
    onError(event: Event) { guard(() => finish(event.callId, event)); },
    onAbort(event: Event) { guard(() => finish(event.callId, event, true)); },
    executeLanguageModelCall(event) {
      const call = calls.get(event.callId);
      const span = call?.models.get("model");
      return run(span && call ? trace.setSpan(call.ctx, span) : undefined, event.execute);
    },
    executeTool(event) {
      const call = calls.get(event.callId);
      const span = call?.tools.get(event.toolCallId);
      return run(span && call ? trace.setSpan(call.ctx, span) : undefined, event.execute);
    },
    finishStream(error) { guard(() => {
      for (const id of calls.keys()) finish(id, { error }, error === undefined);
    }); },
    withOperationContext(execute) {
      // Embed/object dispatchers don't expose an execution wrapper. Their async
      // work inherits this forwarding span, activated by onStart after validation.
      // Never observe the public promise: even a side-effect-only rejection
      // handler would change Node's unhandled-rejection events and exit status.
      if (!operation || rootKind(operation) === "CHAIN") return execute();
      const ctx = guard(() => {
        const parent = context.active();
        operationParent = parent;
        const span = new Proxy(trace.wrapSpanContext(INVALID_SPAN_CONTEXT), {
          get(target, key) {
            const current = operationSpan ?? trace.getSpan(parent) ?? target;
            const value = Reflect.get(current, key, current);
            return typeof value === "function" ? value.bind(current) : value;
          },
        });
        return trace.setSpan(parent, span);
      });
      return run(ctx, execute);
    },
  };
  return integration;
}
