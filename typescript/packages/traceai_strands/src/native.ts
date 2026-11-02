import type { Attributes } from "@opentelemetry/api";
import type { ExportResult } from "@opentelemetry/core";
import type { ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";

export interface StrandsSpanExporterOptions {
  /** Defaults to OTEL_SERVICE_NAME or the SDK's default, strands-agents. */
  instrumentationScope?: string;
  /** Include prompts, responses, tool arguments/results and custom attributes. Default: false. */
  captureContent?: boolean;
  /** Include exception events and status messages. Default: false. */
  captureErrorDetails?: boolean;
  /** Optional model provider for a single-provider application (for example, openai).
   * Strands itself reports the service name, not the model provider. */
  modelProvider?: string;
}

const KINDS: Readonly<Record<string, string>> = {
  invoke_agent: "AGENT",
  chat: "LLM",
  execute_tool: "TOOL",
  invoke_graph: "CHAIN",
  invoke_swarm: "CHAIN",
  execute_node: "CHAIN",
  invoke_agent_loop: "CHAIN",
  "memory.search": "RETRIEVER",
  "memory.add": "CHAIN",
  "memory.inject": "CHAIN",
};

// Native telemetry records content even without a content opt-in. Use a metadata
// allowlist rather than trying to enumerate every possible sensitive attribute.
const METADATA = new Set([
  "gen_ai.operation.name", "gen_ai.system", "gen_ai.provider.name",
  "gen_ai.agent.name", "gen_ai.agent.id", "gen_ai.agent.node_type",
  "gen_ai.agent.status", "gen_ai.agent.execution_time",
  "gen_ai.request.model", "gen_ai.response.model", "gen_ai.response.finish_reasons",
  "gen_ai.tool.name", "gen_ai.tool.call.id", "gen_ai.tool.status",
  "gen_ai.event.start_time", "gen_ai.event.end_time", "gen_ai.server.request.duration",
  "memory.store.count", "memory.max_search_results", "memory.result.count",
  "memory.store.failure_count", "memory.max_entries",
]);
const USAGE = new Set([
  "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
  "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "gen_ai.usage.total_tokens",
  "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_creation.input_tokens",
  "gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cache_write_input_tokens",
]);

/**
 * Adapt native Strands spans for Future AGI, without creating or patching spans.
 * Wrap the ONE exporter attached to your provider; do not also export raw Strands
 * spans to the same destination. Other instrumentation scopes pass through.
 */
export class StrandsSpanExporter implements SpanExporter {
  private readonly scope: string;
  private readonly captureContent: boolean;
  private readonly captureErrorDetails: boolean;
  private readonly modelProvider?: string;

  constructor(
    private readonly exporter: SpanExporter,
    options: StrandsSpanExporterOptions = {},
  ) {
    this.scope = options.instrumentationScope || process.env.OTEL_SERVICE_NAME || "strands-agents";
    this.captureContent = options.captureContent ?? false;
    this.captureErrorDetails = options.captureErrorDetails ?? false;
    this.modelProvider = options.modelProvider;
  }

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    this.exporter.export(spans.map((span) => this.adapt(span)), resultCallback);
  }

  shutdown(): Promise<void> {
    return this.exporter.shutdown();
  }

  forceFlush(): Promise<void> {
    return this.exporter.forceFlush?.() ?? Promise.resolve();
  }

  private adapt(span: ReadableSpan): ReadableSpan {
    if (span.instrumentationScope.name !== this.scope) return span;
    // Loop-cycle spans have no gen_ai.operation.name, but still carry errors.
    const operation = String(span.attributes["gen_ai.operation.name"] ?? span.name);
    const attributes: Attributes = {};
    for (const [key, value] of Object.entries(span.attributes)) {
      // Agent, graph and node usage is aggregated from child model calls. Keep
      // billable counts ONLY on model spans, in both native and FI namespaces.
      if ((key.startsWith("gen_ai.usage.") || key.startsWith("llm.token_count.")) && operation !== "chat") continue;
      if (!this.captureErrorDetails && key.startsWith("exception.")) continue;
      if (this.captureContent || METADATA.has(key) || (operation === "chat" && USAGE.has(key))) {
        attributes[key] = value;
      }
    }
    attributes[SC.FI_SPAN_KIND] = KINDS[operation] ?? "CHAIN";
    // Strands' provider/system is the SERVICE name, not a model provider. Keep
    // that metadata separate rather than guessing a billable provider.
    attributes["strands.service.name"] = this.scope;
    delete attributes["gen_ai.system"];
    delete attributes[SC.LLM_PROVIDER];
    if (operation === "chat") {
      copy(attributes, SC.LLM_PROVIDER, this.modelProvider);
      copy(attributes, SC.LLM_TOKEN_COUNT_PROMPT, attributes["gen_ai.usage.input_tokens"] ?? attributes["gen_ai.usage.prompt_tokens"]);
      copy(attributes, SC.LLM_TOKEN_COUNT_COMPLETION, attributes["gen_ai.usage.output_tokens"] ?? attributes["gen_ai.usage.completion_tokens"]);
      copy(attributes, SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ, attributes["gen_ai.usage.cache_read.input_tokens"] ?? attributes["gen_ai.usage.cache_read_input_tokens"]);
      copy(attributes, SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE, attributes["gen_ai.usage.cache_creation.input_tokens"] ?? attributes["gen_ai.usage.cache_write_input_tokens"]);
    }
    if (operation === "execute_tool") copy(attributes, SC.TOOL_NAME, attributes["gen_ai.tool.name"]);
    if (this.captureContent) mapContent(span, attributes);

    // Create an export-only view. Never mutate SDK spans or other exporters'
    // copies. Explicitly preserve prototype-backed fields and spanContext().
    return {
      name: span.name, kind: span.kind, spanContext: () => span.spanContext(),
      parentSpanContext: span.parentSpanContext,
      startTime: span.startTime, endTime: span.endTime, duration: span.duration,
      ended: span.ended, resource: span.resource, instrumentationScope: span.instrumentationScope,
      attributes,
      status: this.captureErrorDetails ? { ...span.status } : { code: span.status.code },
      events: span.events.filter((event) => event.name === "exception"
        ? this.captureErrorDetails : this.captureContent),
      links: span.links.map((link) => this.captureContent ? link : { context: link.context }),
      droppedAttributesCount: span.droppedAttributesCount,
      droppedEventsCount: span.droppedEventsCount,
      droppedLinksCount: span.droppedLinksCount,
    };
  }
}

function copy(attributes: Attributes, key: string, value: Attributes[string]): void {
  if (value !== undefined) attributes[key] = value;
}

function mapContent(span: ReadableSpan, attributes: Attributes): void {
  // Canonical attributes are used when enabled upstream. Otherwise lift native
  // events into the FI input/output fields; no semconv environment flags needed.
  let input = attributes["gen_ai.tool.call.arguments"] ?? attributes["gen_ai.input.messages"] ?? attributes["gen_ai.agent.input"];
  let output = attributes["gen_ai.tool.call.result"] ?? attributes["gen_ai.output.messages"];
  const messages: unknown[] = [];
  for (const event of span.events) {
    input ??= event.attributes?.[SC.LLM_INPUT_MESSAGES];
    output ??= event.attributes?.[SC.LLM_OUTPUT_MESSAGES];
    if (attributes[SC.LLM_INPUT_MESSAGES] === undefined) copy(attributes, SC.LLM_INPUT_MESSAGES, event.attributes?.[SC.LLM_INPUT_MESSAGES]);
    if (attributes[SC.LLM_OUTPUT_MESSAGES] === undefined) copy(attributes, SC.LLM_OUTPUT_MESSAGES, event.attributes?.[SC.LLM_OUTPUT_MESSAGES]);
    if (/^gen_ai\.(user|assistant|system|tool|message)\.message$/.test(event.name) || event.name === "gen_ai.message") {
      messages.push({ role: event.name.split(".")[1], content: event.attributes?.content });
    }
    if (event.name === "gen_ai.choice") output ??= event.attributes?.message;
  }
  input ??= messages.length ? JSON.stringify(messages) : undefined;
  if (input !== undefined) {
    attributes[SC.INPUT_VALUE] = jsonValue(input);
    attributes[SC.INPUT_MIME_TYPE] = "application/json";
  }
  if (output !== undefined) {
    attributes[SC.OUTPUT_VALUE] = jsonValue(output);
    attributes[SC.OUTPUT_MIME_TYPE] = "application/json";
  }
}

function jsonValue(value: Attributes[string]): string {
  if (typeof value === "string") {
    try { JSON.parse(value); return value; } catch { /* Native choices may be plain text. */ }
  }
  return JSON.stringify(value);
}
