import { Attributes, context, propagation, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { ExportResult, W3CTraceContextPropagator } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { isPatched } from "./instrumentation";
import { nativeTracingEnabled, enableNativeTracing } from "./native-state";

export interface GoogleADKNativeOptions {
  /** Include native request/response JSON. Off by default. */
  captureContent?: boolean;
  /** Include conversation/session identifiers. Off by default. */
  captureSessionId?: boolean;
}

export interface GoogleADKConfigureOptions extends GoogleADKNativeOptions {
  projectName: string;
  /** Full OTLP/HTTP traces URL, not a collector base URL. */
  endpoint?: string;
  apiKey?: string;
  secretKey?: string;
}

const SCOPE = "gcp.vertex.agent";
// Explicit allowlist: newly introduced native content fields must not silently
// bypass the default privacy policy. Non-ADK instrumentation is left alone.
const METADATA = new Set([
  "gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.system",
  "gen_ai.request.model", "gen_ai.request.top_p", "gen_ai.request.max_tokens",
  "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
  "gen_ai.response.finish_reasons", "gen_ai.tool.name", "gen_ai.tool.type",
  "adk.workflow.name", "adk.node.path", "adk.node.attempt", "adk.node.status",
  "adk.node.interrupt_count", "error.type",
]);

function objectFromJson(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown> : undefined;
  } catch { return undefined; }
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

/** Translate only native ADK spans; never create or reparent spans. */
function translate(span: ReadableSpan, options: GoogleADKNativeOptions): ReadableSpan {
  if (span.instrumentationScope.name !== SCOPE) return span;
  const source = span.attributes;
  const attributes: Attributes = {};
  for (const [key, value] of Object.entries(source)) {
    if (METADATA.has(key)) attributes[key] = value;
  }
  const operation = source["gen_ai.operation.name"];
  const kind = operation === "invoke_agent" || span.name.startsWith("agent_run [") ? "AGENT"
    : operation === "execute_tool" ? "TOOL"
    : source["gen_ai.request.model"] !== undefined || span.name === "call_llm" ? "LLM"
    : "CHAIN";
  attributes["fi.span.kind"] = kind;
  if (kind === "LLM") {
    // The native output count excludes thinking tokens. Prefer the original
    // usage metadata when present, even if content will be removed on export.
    const response = objectFromJson(source["gcp.vertex.agent.llm_response"]);
    const usage = response?.usageMetadata as Record<string, unknown> | undefined;
    const candidates = count(usage?.candidatesTokenCount);
    const thoughts = count(usage?.thoughtsTokenCount);
    const prompt = count(usage?.promptTokenCount);
    const total = count(usage?.totalTokenCount);
    if (prompt !== undefined) attributes["gen_ai.usage.input_tokens"] = prompt;
    if (candidates !== undefined) attributes["gen_ai.usage.output_tokens"] = candidates + (thoughts ?? 0);
    if (thoughts !== undefined) attributes["gen_ai.usage.output_tokens.reasoning"] = thoughts;
    // Never invent a total when upstream omitted it (e.g. content disabled).
    if (total !== undefined) attributes["gen_ai.usage.total_tokens"] = total;
  }
  if (options.captureSessionId) {
    const session = source["gen_ai.conversation.id"] ?? source["gcp.vertex.agent.session_id"];
    if (session !== undefined) {
      attributes["session.id"] = session;
      attributes["gen_ai.conversation.id"] = session;
    }
  }
  if (options.captureContent) {
    const input = kind === "TOOL" ? source["gcp.vertex.agent.tool_call_args"] : source["gcp.vertex.agent.llm_request"];
    const output = kind === "TOOL" ? source["gcp.vertex.agent.tool_response"] : source["gcp.vertex.agent.llm_response"];
    if (typeof input === "string" && input !== "{}") {
      attributes["input.value"] = input;
      attributes["input.mime_type"] = "application/json";
    }
    if (typeof output === "string" && output !== "{}") {
      attributes["output.value"] = output;
      attributes["output.mime_type"] = "application/json";
    }
  }
  // A new readable view avoids mutating spans seen by other processors. Native
  // error messages/events/links may contain prompts, even without content attrs.
  return {
    ...span,
    spanContext: () => span.spanContext(),
    attributes,
    status: { code: span.status.code },
    events: span.events.filter(event => event.name === "exception").map(event => ({
      ...event,
      attributes: Object.fromEntries(Object.entries(event.attributes ?? {}).filter(
        ([key]) => key === "exception.type" || key === "exception.escaped",
      )),
    })),
    links: span.links.map(link => ({ ...link, attributes: {} })),
  };
}

/** Wrap the sole export path in an existing OTel SDK. Does not install a provider. */
export class GoogleADKSpanExporter implements SpanExporter {
  constructor(private readonly exporter: SpanExporter, private readonly options: GoogleADKNativeOptions = {}) {}

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    this.exporter.export(spans.map(span => translate(span, this.options)), callback);
  }

  shutdown(): Promise<void> { return this.exporter.shutdown(); }
  forceFlush(): Promise<void> { return this.exporter.forceFlush?.() ?? Promise.resolve(); }
}

/** Configure native ADK tracing in a standalone Node process (traces only). */
export function configure(options: GoogleADKConfigureOptions): NodeTracerProvider {
  if (!options.projectName?.trim()) throw new Error("projectName is required");
  if (nativeTracingEnabled || isPatched()) {
    throw new Error("Google ADK tracing is already configured; do not combine native and legacy instrumentation");
  }
  const apiKey = options.apiKey ?? process.env.FI_API_KEY;
  const secretKey = options.secretKey ?? process.env.FI_SECRET_KEY;
  if (!apiKey || !secretKey) throw new Error("FI_API_KEY and FI_SECRET_KEY are required");
  const url = new URL(options.endpoint ?? "https://api.futureagi.com/tracer/v1/traces");
  if (url.protocol !== "https:" && !(url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))) {
    throw new Error("Use HTTPS for remote trace export (HTTP is allowed only on localhost)");
  }
  if (url.username || url.password) throw new Error("Exporter endpoint must not contain credentials");
  const exporter = new GoogleADKSpanExporter(new OTLPTraceExporter({
    url: url.href, headers: { "x-api-key": apiKey, "x-secret-key": secretKey },
  }), options);
  const provider = new NodeTracerProvider({
    resource: resourceFromAttributes({
      "service.name": options.projectName,
      project_name: options.projectName,
      project_type: "observe",
    }),
    spanProcessors: [new BatchSpanProcessor(exporter)],
  });
  if (!trace.setGlobalTracerProvider(provider)) {
    void provider.shutdown();
    throw new Error("An OTel provider is already registered; wrap its exporter with GoogleADKSpanExporter instead");
  }
  const manager = new AsyncLocalStorageContextManager().enable();
  if (!context.setGlobalContextManager(manager)) manager.disable();
  propagation.setGlobalPropagator(new W3CTraceContextPropagator());
  enableNativeTracing();
  return provider;
}
