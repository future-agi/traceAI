import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  Attributes, Span, SpanKind, SpanStatusCode, TracerProvider, context, diag, trace,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions as S, FISpanKind } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface VertexAIInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

type SDKObject = Record<string, any>;
type Method = (...args: any[]) => any;
type Patch = { target: SDKObject; method: string; original: Method; wrapped: Method };
const owners = new WeakMap<object, VertexAIInstrumentation>();
const wrappedMethods = new WeakSet<Method>();
let patchedModules = 0;
const methods: Record<string, string[]> = {
  GenerativeModel: ["generateContent", "generateContentStream", "countTokens"],
  GenerativeModelPreview: ["generateContent", "generateContentStream", "countTokens"],
  ChatSession: ["sendMessage", "sendMessageStream"],
  ChatSessionPreview: ["sendMessage", "sendMessageStream"],
};

/** Compatibility instrumentation for the legacy @google-cloud/vertexai 1.x SDK. */
export class VertexAIInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private traceConfig?: TraceConfigOptions;
  private patches = new Map<object, Patch[]>();

  constructor(config: VertexAIInstrumentationConfig = {}) {
    super("@traceai/fi-instrumentation-vertexai", VERSION, config.instrumentationConfig || {});
    this.traceConfig = config.traceConfig;
    // InstrumentationBase calls enable() before subclass fields are initialized.
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig });
  }

  override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  override enable(): void {
    super.enable();
    this.refreshTracer();
  }

  override disable(): void {
    super.disable();
    // OTel only knows about automatic hooks; explicitly restore manual patches too.
    for (const module of this.patches?.keys() || []) this.unpatch(module);
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@google-cloud/vertexai", [">=1.0.0 <2.0.0"],
      this.patch.bind(this), this.unpatch.bind(this),
    );
  }

  manuallyInstrument(vertexModule: any): void {
    if (vertexModule != null && this.isEnabled()) this.patch(vertexModule);
  }

  private patch(module: SDKObject): SDKObject {
    if (owners.has(module)) return module;
    const patches: Patch[] = [];
    for (const [name, names] of Object.entries(methods)) {
      const target = module[name]?.prototype;
      if (!target) continue;
      for (const method of names) {
        const original = target[method];
        if (typeof original !== "function" || wrappedMethods.has(original)) continue;
        const instrumentation = this;
        const wrapped = function(this: SDKObject, ...args: any[]) {
          return instrumentation.invoke(original, this, args, method);
        };
        target[method] = wrapped;
        wrappedMethods.add(wrapped);
        patches.push({ target, method, original, wrapped });
      }
    }
    if (patches.length) {
      owners.set(module, this);
      this.patches.set(module, patches);
      patchedModules++;
    }
    return module;
  }

  private unpatch(module: object): void {
    if (owners.get(module) !== this) return;
    for (const patch of this.patches.get(module) || []) {
      // Do not remove another instrumentor's later wrapper.
      if (patch.target[patch.method] === patch.wrapped) patch.target[patch.method] = patch.original;
    }
    this.patches.delete(module);
    owners.delete(module);
    patchedModules--;
  }

  private async invoke(original: Method, instance: SDKObject, args: any[], method: string): Promise<any> {
    if (!this.isEnabled()) return original.apply(instance, args);
    const streaming = method.endsWith("Stream");
    const counting = method === "countTokens";
    let span: Span | undefined;
    try {
      const model = instance.model || (instance.resourcePath ?? instance.publisherModelEndpoint)?.split("/models/")[1] || "unknown";
      span = this.fiTracer.startSpan(`Vertex AI ${method}`, {
        kind: SpanKind.CLIENT,
        attributes: {
          [S.FI_SPAN_KIND]: FISpanKind.LLM,
          [S.LLM_PROVIDER]: "google",
          [S.LLM_MODEL_NAME]: model,
          [S.GEN_AI_OPERATION_NAME]: counting ? "count_tokens" : "chat",
          "vertexai.streaming": streaming,
          ...inputAttributes(instance, args[0], method),
        },
      });
    } catch {
      diag.warn("Vertex AI input telemetry failed");
    }
    const activeSpan = span;
    const executionContext = activeSpan ? trace.setSpan(context.active(), activeSpan) : context.active();
    let result: any;
    try {
      result = await context.with(executionContext, () => original.apply(instance, args));
    } catch (error) {
      if (activeSpan) finish(activeSpan, undefined, true, counting);
      throw error;
    }
    if (activeSpan) {
      if (streaming && result?.response?.then) {
        // The SDK eagerly reads an independent tee into response, even if the caller
        // never iterates (or breaks early). Observe that existing promise, not the
        // caller's iterator: no extra buffering, reads, promises or history mutation.
        void result.response.then(
          (response: SDKObject) => finish(activeSpan, response, false, counting),
          () => finish(activeSpan, undefined, true, counting),
        );
      } else {
        finish(activeSpan, result?.response || result, false, counting);
      }
    }
    return result;
  }
}

function finish(span: Span, response: SDKObject | undefined, failed: boolean, counting: boolean): void {
  try {
    if (failed) {
      // Provider errors can contain prompts, signed URLs and credentials. Never
      // copy their message, stack or response body, including in default capture.
      span.setAttribute("error.type", "vertexai.request_failed");
      span.setStatus({ code: SpanStatusCode.ERROR });
    } else {
      if (response) span.setAttributes(outputAttributes(response, counting));
      span.setStatus({ code: SpanStatusCode.OK });
    }
  } catch {
    diag.warn("Vertex AI output telemetry failed");
  } finally {
    try { span.end(); } catch { diag.warn("Vertex AI span completion failed"); }
  }
}

function inputAttributes(instance: SDKObject, request: any, method: string): Attributes {
  const chat = method.startsWith("sendMessage");
  const contents = chat
    ? [...(instance.historyInternal || []).slice(-32), { role: "user", parts: typeof request === "string" ? [{ text: request }] : request }]
    : typeof request === "string" ? [{ role: "user", parts: [{ text: request }] }] : request?.contents || [];
  const attributes: Attributes = {};
  if (contents.length > 32 || (chat && instance.historyInternal?.length > 31)) attributes["vertexai.capture.truncated"] = true;
  const messages = contents.slice(-32).map((content: SDKObject) => message(content, attributes));
  const instruction = request?.systemInstruction ?? instance.systemInstruction;
  if (instruction) messages.unshift(message(typeof instruction === "string" ? { role: "system", parts: [{ text: instruction }] } : { ...instruction, role: "system" }, attributes));
  const tools = request?.tools ?? instance.tools;
  // Keep all content-bearing values inside message.content so FI's granular
  // text/messages privacy controls also hide tool arguments and declarations.
  if (Array.isArray(tools)) {
    if (tools.length > 32) attributes["vertexai.capture.truncated"] = true;
    messages.unshift({ role: "system", content: tools.slice(0, 32).map((tool: SDKObject) => ({ functionDeclarations: tool.functionDeclarations })) });
  }
  if (messages.length > 32) {
    const prefix = (instruction ? 1 : 0) + (Array.isArray(tools) ? 1 : 0);
    messages.splice(prefix, messages.length - 32);
    attributes["vertexai.capture.truncated"] = true;
  }
  capture(attributes, S.LLM_INPUT_MESSAGES, messages);
  const config = request?.generationConfig ?? instance.generationConfig;
  for (const [name, key] of Object.entries({ temperature: "gen_ai.request.temperature", topP: "gen_ai.request.top_p", topK: "gen_ai.request.top_k", maxOutputTokens: "gen_ai.request.max_tokens" })) {
    if (typeof config?.[name] === "number" && Number.isFinite(config[name])) attributes[key] = config[name];
  }
  return attributes;
}

function message(content: SDKObject, attributes: Attributes): SDKObject {
  if (Array.isArray(content?.parts) && content.parts.length > 32) attributes["vertexai.capture.truncated"] = true;
  const parts = Array.isArray(content?.parts) ? content.parts.slice(0, 32) : [];
  return {
    role: ["user", "model", "system", "function"].includes(content?.role) ? content.role : "user",
    content: parts.map((part: SDKObject) => {
      if (typeof part?.text === "string") return { type: "text", text: part.text };
      if (part?.functionCall) return { type: "tool_call", name: part.functionCall.name, arguments: part.functionCall.args };
      if (part?.functionResponse) return { type: "tool_result", name: part.functionResponse.name, response: part.functionResponse.response };
      // Never copy inlineData.data, fileUri, arbitrary response fields or media.
      return { type: "media" };
    }),
  };
}

function outputAttributes(response: SDKObject, counting: boolean): Attributes {
  const attributes: Attributes = {};
  if (counting) {
    if (typeof response.totalTokens === "number") attributes["vertexai.total_tokens"] = response.totalTokens;
    return attributes;
  }
  if (Array.isArray(response.candidates)) {
    if (response.candidates.length > 32) attributes["vertexai.capture.truncated"] = true;
    capture(attributes, S.LLM_OUTPUT_MESSAGES, response.candidates.slice(0, 32).map((candidate: SDKObject) => message(candidate.content, attributes)));
    attributes[S.GEN_AI_RESPONSE_FINISH_REASONS] = response.candidates.slice(0, 32).map((candidate: SDKObject) => candidate.finishReason).filter((reason: unknown) => typeof reason === "string").map((reason: string) => reason.slice(0, 128));
  }
  const usage = response.usageMetadata;
  for (const [name, key] of Object.entries({ promptTokenCount: S.LLM_TOKEN_COUNT_PROMPT, candidatesTokenCount: S.LLM_TOKEN_COUNT_COMPLETION, totalTokenCount: S.LLM_TOKEN_COUNT_TOTAL, cachedContentTokenCount: "gen_ai.usage.cache_read.input_tokens", thoughtsTokenCount: "gen_ai.usage.reasoning.output_tokens" })) {
    if (typeof usage?.[name] === "number" && Number.isFinite(usage[name]) && usage[name] >= 0) attributes[key] = usage[name];
  }
  return attributes;
}

/** Bound content before serialization; do not retain chunks or serialize whole SDK objects. */
function capture(attributes: Attributes, key: string, value: unknown): void {
  let budget = 24_000;
  let truncated = false;
  const bound = (item: any, depth = 0): any => {
    if (budget <= 0 || depth > 10) { truncated = true; return "[truncated]"; }
    budget -= 16;
    if (typeof item === "string") {
      const limit = Math.min(4096, Math.max(0, budget));
      truncated ||= item.length > limit;
      const text = item.slice(0, limit);
      budget -= text.length;
      return text;
    }
    if (item === null || typeof item === "boolean" || typeof item === "number") return item;
    if (Array.isArray(item)) {
      truncated ||= item.length > 32;
      return item.slice(0, 32).map(entry => bound(entry, depth + 1));
    }
    if (item && typeof item === "object") {
      const result: Record<string, unknown> = Object.create(null);
      const keys = Object.keys(item);
      truncated ||= keys.length > 32;
      for (const name of keys.slice(0, 32)) result[name.slice(0, 128)] = bound(item[name], depth + 1);
      return result;
    }
    return undefined;
  };
  attributes[key] = JSON.stringify(bound(value));
  if (truncated) attributes["vertexai.capture.truncated"] = true;
}

export function isPatched(): boolean { return patchedModules > 0; }

/** @deprecated Test compatibility only. Disable the owning instance to restore patches. */
export function _resetPatchedStateForTesting(): void { /* No global ownership reset. */ }
