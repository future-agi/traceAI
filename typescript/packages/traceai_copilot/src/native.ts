import type { Attributes, HrTime, SpanContext } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import {
  redact,
  TRANSPORT_CONTRACT,
  type ResolvedConfig,
} from "@traceai/agent-core";

export const MAX_LINE_BYTES = 1_048_576;
export const CONTENT_LIMIT = 20_000;
const operations = new Set([
  "invoke_agent",
  "chat",
  "execute_tool",
  "execute_hook",
  "invoke_workflow",
  "compact",
  "compaction",
]);
const stringFields = new Set([
  "gen_ai.operation.name",
  "gen_ai.provider.name",
  "gen_ai.request.model",
  "gen_ai.response.model",
  "gen_ai.conversation.id",
  "gen_ai.agent.id",
  "gen_ai.agent.name",
  "gen_ai.tool.name",
  "gen_ai.tool.type",
  "gen_ai.tool.call.id",
  "gen_ai.response.id",
  "gen_ai.response.finish_reasons",
  "github.copilot.session.id",
  "github.copilot.agent.id",
  "github.copilot.parent_session.id",
  "github.copilot.interaction_id",
  "github.copilot.turn_id",
  "github.copilot.agent.type",
  "github.copilot.initiator",
  "github.copilot.hook.type",
  "github.copilot.hook.name",
  "gen_ai.hook.name",
]);
const numericFields = new Set([
  "gen_ai.usage.input_tokens",
  "gen_ai.usage.output_tokens",
  "gen_ai.usage.total_tokens",
  "gen_ai.usage.cache_read.input_tokens",
  "gen_ai.usage.cache_write.input_tokens",
  "gen_ai.usage.reasoning.output_tokens",
  "gen_ai.usage.cost",
  "gen_ai.usage.cost_usd",
  "gen_ai.cost.total",
  "gen_ai.request.temperature",
  "gen_ai.request.max_tokens",
  "gen_ai.response.time_to_first_chunk",
  "process.exit.code",
  "github.copilot.agent.turn_count",
  "github.copilot.turn_count",
  "github.copilot.cost",
  "github.copilot.server_duration",
]);
const inputFields = new Set([
  "gen_ai.input.messages",
  "gen_ai.system_instructions",
  "gen_ai.tool.definitions",
  "gen_ai.tool.call.arguments",
]);
const outputFields = new Set([
  "gen_ai.output.messages",
  "gen_ai.tool.call.result",
]);
const id = (v: unknown, size: number): v is string =>
  typeof v === "string" &&
  new RegExp(`^[0-9a-f]{${size}}$`).test(v) &&
  !/^0+$/.test(v);
const object = (v: unknown): Record<string, any> | undefined =>
  v && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, any>)
    : undefined;
function time(v: unknown): HrTime | undefined {
  return Array.isArray(v) &&
    v.length === 2 &&
    v.every(Number.isSafeInteger) &&
    v[0] >= 0 &&
    v[1] >= 0 &&
    v[1] < 1e9
    ? [v[0], v[1]]
    : undefined;
}
/** Redact before truncating: a credential crossing the boundary must not leave a prefix. */
export function cleanText(
  value: unknown,
  config: ResolvedConfig,
): string | undefined {
  if (typeof value !== "string") return undefined;
  let text = value;
  for (const secret of [
    config.credentials?.apiKey,
    config.credentials?.secretKey,
  ])
    if (secret) text = text.split(secret).join("[REDACTED]");
  return redact(text).slice(0, CONTENT_LIMIT);
}
function label(value: unknown, config: ResolvedConfig): string | undefined {
  if (
    typeof value !== "string" ||
    !/^[a-zA-Z0-9_.:/-]{1,128}$/.test(value) ||
    value.includes("://")
  )
    return undefined;
  const clean = cleanText(value, config);
  return clean === value ? clean : undefined;
}
/** Native inputs contain previous outputs, not just prompts. Never export hidden
 * tool responses again through history or FI aliases. Failed-response IDs are not
 * reliably available on every record, so strict policies conservatively retain
 * only recognized user/system text. Unknown/opaque history is omitted. */
function cleanHistory(
  value: unknown,
  config: ResolvedConfig,
): string | undefined {
  if (!config.hideOutputs && !config.hideErrorDetails)
    return cleanText(value, config);
  if (typeof value !== "string") return undefined;
  let messages: unknown;
  try {
    messages = JSON.parse(value);
  } catch {
    return undefined;
  }
  if (!Array.isArray(messages)) return undefined;
  const safe = [];
  for (const message of messages) {
    const m = object(message);
    if (
      !m ||
      !["user", "system", "developer"].includes(m.role) ||
      !Array.isArray(m.parts)
    )
      continue;
    const parts = m.parts
      .filter((part: unknown) => {
        const p = object(part);
        return p?.type === "text" && typeof p.content === "string";
      })
      .map((p: { content: string }) => ({ type: "text", content: p.content }));
    if (parts.length) safe.push({ role: m.role, parts });
  }
  return safe.length ? cleanText(JSON.stringify(safe), config) : undefined;
}

/** Tools can return an explicit error envelope without a failed native status. */
function failedResult(value: unknown): boolean {
  if (typeof value !== "string" || value.length > 256 * 1024) return false;
  try {
    const result = object(JSON.parse(value));
    return !!result && (
      result.isError === true || result.is_error === true ||
      result.success === false || result.type === "error" ||
      result.status === "error" || result.status === "failed"
    );
  } catch {
    return false;
  }
}

/** Rebuild native spans. No second tracer, invented ancestry, usage estimates, or durable content. */
export function nativeSpan(
  value: unknown,
  config: ResolvedConfig,
): ReadableSpan | undefined {
  const raw = object(value);
  if (
    !raw ||
    raw.type !== "span" ||
    !id(raw.traceId, 32) ||
    !id(raw.spanId, 16)
  )
    return undefined;
  const startTime = time(raw.startTime),
    endTime = time(raw.endTime),
    attrs = object(raw.attributes) ?? {};
  if (
    !startTime ||
    !endTime ||
    endTime[0] < startTime[0] ||
    (endTime[0] === startTime[0] && endTime[1] < startTime[1])
  )
    return undefined;
  const operation = attrs["gen_ai.operation.name"];
  if (!operations.has(operation)) return undefined; // Metrics, notifications and unknown schemas are not agent turns.
  const isTool = operation === "execute_tool" || operation === "execute_hook";
  const exitCode = attrs["process.exit.code"];
  const failed =
    raw.status?.code === 2 ||
    (isTool && Number.isSafeInteger(exitCode) && exitCode !== 0) ||
    (isTool && failedResult(attrs["gen_ai.tool.call.result"]));
  const attributes: Attributes = {};
  for (const [key, val] of Object.entries(attrs)) {
    if (stringFields.has(key)) {
      const text = label(val, config);
      if (text !== undefined) attributes[key] = text;
      else if (key === "gen_ai.response.finish_reasons" && Array.isArray(val))
        attributes[key] = val
          .slice(0, 16)
          .map((v) => label(v, config))
          .filter((v): v is string => !!v);
    } else if (
      numericFields.has(key) &&
      typeof val === "number" &&
      Number.isFinite(val) &&
      (val >= 0 || (key === "process.exit.code" && Number.isSafeInteger(val)))
    ) {
      // Native parent totals use the same canonical keys as request usage.
      // Move them out of the collector's additive namespace, including costs.
      const aggregate =
        operation !== "chat" &&
        (key.startsWith("gen_ai.usage.") ||
          key.startsWith("gen_ai.cost.") ||
          key === "github.copilot.cost");
      attributes[aggregate ? `fi.agent.aggregate.${key}` : key] = val;
    } else if (
      config.captureContent &&
      ((inputFields.has(key) && !config.hideInputs) ||
        (outputFields.has(key) &&
          !config.hideOutputs &&
          !(config.hideErrorDetails && failed)))
    ) {
      const text =
        key === "gen_ai.input.messages"
          ? cleanHistory(val, config)
          : cleanText(val, config);
      if (text !== undefined) attributes[key] = text;
    }
  }
  if (isTool && attributes["gen_ai.tool.name"])
    attributes[SC.TOOL_NAME] = attributes["gen_ai.tool.name"];
  if (isTool && attributes["gen_ai.tool.call.id"])
    attributes[SC.TOOL_CALL_ID] = attributes["gen_ai.tool.call.id"];
  // Only explicitly dollar-denominated request costs enter the canonical cost
  // field. github.copilot.cost is a billing multiplier, not a dollar amount.
  if (
    operation === "chat" &&
    attributes["gen_ai.cost.total"] === undefined &&
    attributes["gen_ai.usage.cost_usd"] !== undefined
  )
    attributes["gen_ai.cost.total"] = attributes["gen_ai.usage.cost_usd"];
  if (operation === "chat" && attributes[SC.GEN_AI_COST_TOTAL] !== undefined) {
    attributes[SC.FI_COST_KIND] = "provider_reported";
    attributes[SC.FI_AGENT_COST_SOURCE] = "reported";
  }
  // Native Copilot reports measured request latency in seconds; FI consumes ms.
  const firstChunk = attributes["gen_ai.response.time_to_first_chunk"];
  if (
    operation === "chat" &&
    typeof firstChunk === "number" &&
    Number.isFinite(firstChunk * 1000)
  )
    attributes[SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN] = firstChunk * 1000;
  const session =
    attributes["gen_ai.conversation.id"] ??
    attributes["github.copilot.session.id"];
  if (session) attributes["session.id"] = session;
  if (config.userId) attributes["user.id"] = cleanText(config.userId, config);
  attributes[SC.FI_SPAN_KIND] =
    operation === "chat"
      ? "LLM"
      : operation === "execute_tool" || operation === "execute_hook"
        ? "TOOL"
        : "AGENT";
  attributes["fi.agent.name"] = "copilot";
  attributes["fi.agent.surface"] = "cli";
  attributes["fi.agent.source"] = "native";
  attributes[SC.FI_USAGE_CONTRIBUTION] =
    operation === "chat" ? "additive" : "none";
  attributes[SC.FI_USAGE_SCOPE] = operation === "chat" ? "request" : "turn";
  attributes["fi.agent.usage.additive"] = operation === "chat";
  attributes["fi.agent.usage.plan"] = "request";
  attributes["fi.agent.transport"] = TRANSPORT_CONTRACT;
  attributes["fi.agent.fidelity"] =
    operation === "invoke_agent" ? "aggregate-turn" : "native-request";
  attributes["fi.agent.timing"] = "source";
  if (attributes["gen_ai.request.model"])
    attributes[SC.LLM_MODEL_NAME] = attributes["gen_ai.request.model"];
  if (attributes["gen_ai.provider.name"])
    attributes[SC.LLM_PROVIDER] = attributes["gen_ai.provider.name"];
  const input =
    attributes[isTool ? "gen_ai.tool.call.arguments" : "gen_ai.input.messages"];
  const output =
    attributes[isTool ? "gen_ai.tool.call.result" : "gen_ai.output.messages"];
  if (input !== undefined) attributes[SC.INPUT_VALUE] = input;
  if (output !== undefined) attributes[SC.OUTPUT_VALUE] = output;
  // Only model requests contribute canonical FI usage counters.
  if (operation === "chat") {
    for (const [from, to] of [
      ["gen_ai.usage.input_tokens", SC.LLM_TOKEN_COUNT_PROMPT],
      ["gen_ai.usage.output_tokens", SC.LLM_TOKEN_COUNT_COMPLETION],
      [
        "gen_ai.usage.cache_read.input_tokens",
        SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
      ],
      [
        "gen_ai.usage.cache_write.input_tokens",
        SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE,
      ],
      [
        "gen_ai.usage.reasoning.output_tokens",
        SC.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
      ],
    ])
      if (attributes[from] !== undefined) attributes[to] = attributes[from];
    const inputTokens = attributes["gen_ai.usage.input_tokens"],
      outputTokens = attributes["gen_ai.usage.output_tokens"];
    if (typeof inputTokens === "number" && typeof outputTokens === "number")
      attributes[SC.LLM_TOKEN_COUNT_TOTAL] = inputTokens + outputTokens;
  }
  const context: SpanContext = {
    traceId: raw.traceId,
    spanId: raw.spanId,
    traceFlags: 1,
  };
  let seconds = endTime[0] - startTime[0],
    nanos = endTime[1] - startTime[1];
  if (nanos < 0) {
    seconds--;
    nanos += 1e9;
  }
  const nativeEvents = Array.isArray(raw.events) ? raw.events : [];
  const events: ReadableSpan["events"] = [];
  // Structural cancellation survives every privacy mode. Do not infer it from a
  // generic error finish reason, signal exit, or arbitrary event text.
  for (const e of nativeEvents) {
    if (!object(e)) continue;
    const name = [
      "gen_ai.compaction",
      "github.copilot.compaction",
      "github.copilot.session.abort",
      "exception",
    ].includes(e.name)
      ? e.name
      : undefined;
    if (!name) continue;
    const eventAttrs: Attributes = {};
    if (
      name === "github.copilot.session.abort" &&
      e.attributes?.["github.copilot.abort_reason"] === "user_initiated"
    ) {
      eventAttrs["github.copilot.abort_reason"] = "user_initiated";
      if (operation === "chat" || operation === "invoke_agent")
        attributes[SC.FI_AGENT_OUTCOME] = "interrupted";
    }
    if (
      name === "exception" &&
      config.captureContent &&
      !config.hideInputs &&
      !config.hideOutputs &&
      !config.hideErrorDetails
    )
      for (const [key, val] of Object.entries(object(e.attributes) ?? {}).slice(
        0,
        16,
      )) {
        if (
          ![
            "exception.type",
            "exception.message",
            "exception.stacktrace",
          ].includes(key)
        )
          continue;
        const text = cleanText(val, config);
        if (text !== undefined) eventAttrs[key] = text;
      }
    const event = {
      name,
      time: time(e.time) ?? endTime,
      attributes: eventAttrs,
    };
    if (events.length < 32) events.push(event);
    else if (eventAttrs["github.copilot.abort_reason"] === "user_initiated")
      events[31] = event;
  }
  const tool = label(attrs["gen_ai.tool.name"], config),
    model = label(attrs["gen_ai.request.model"], config);
  const status = {
    code: failed ? 2 : [0, 1].includes(raw.status?.code) ? raw.status.code : 0,
    ...(config.captureContent &&
    !config.hideErrorDetails &&
    !config.hideInputs &&
    !config.hideOutputs &&
    typeof raw.status?.message === "string"
      ? { message: cleanText(raw.status.message, config) }
      : {}),
  };
  return {
    name: `${operation}${operation === "chat" && model ? ` ${model}` : operation === "execute_tool" && tool ? ` ${tool}` : ""}`,
    kind:
      Number.isInteger(raw.kind) && raw.kind >= 0 && raw.kind <= 4
        ? raw.kind
        : 0,
    spanContext: () => context,
    parentSpanContext: id(raw.parentSpanId, 16)
      ? { traceId: raw.traceId, spanId: raw.parentSpanId, traceFlags: 1 }
      : undefined,
    startTime,
    endTime,
    duration: [seconds, nanos],
    status,
    attributes,
    events,
    links: (Array.isArray(raw.links) ? raw.links : [])
      .slice(0, 16)
      .filter(
        (link: any) =>
          id(link?.context?.traceId, 32) && id(link?.context?.spanId, 16),
      )
      .map((link: any) => ({
        context: {
          traceId: link.context.traceId,
          spanId: link.context.spanId,
          traceFlags: 1,
        },
        attributes: {},
      })),
    resource: resourceFromAttributes({
      "service.name": "github-copilot",
      project_name: config.projectName,
      project_type: "observe",
    }),
    instrumentationScope: {
      name: "github.copilot",
      version: label(raw.instrumentationScope?.version, config),
    },
    droppedAttributesCount: 0,
    droppedEventsCount: 0,
    droppedLinksCount: 0,
    ended: true,
  };
}

/** Native requests end before their enclosing turn. Keep only bounded structural
 * parent IDs so a request's verified cancellation also labels that exact turn. */
export class NativeSpans {
  private readonly interruptedTurns = new Set<string>();
  private readonly finalRequests = new Map<string, { startTime: HrTime; failed: boolean }>();
  constructor(private readonly config: ResolvedConfig) {}
  accept(record: unknown): ReadableSpan | undefined {
    const span = nativeSpan(record, this.config);
    if (!span) return undefined;
    const context = span.spanContext();
    const key = `${context.traceId}:${context.spanId}`;
    const operation = span.attributes["gen_ai.operation.name"];
    if (operation === "chat" && span.parentSpanContext) {
      const parent = `${context.traceId}:${span.parentSpanContext.spanId}`;
      const previous = this.finalRequests.get(parent);
      // Source request ordering, not arrival order, determines whether a retry recovered.
      if (!previous || span.startTime[0] > previous.startTime[0] ||
          (span.startTime[0] === previous.startTime[0] && span.startTime[1] >= previous.startTime[1])) {
        this.finalRequests.set(parent, { startTime: span.startTime, failed: span.status.code === 2 });
      }
      if (this.finalRequests.size > 1024)
        this.finalRequests.delete(this.finalRequests.keys().next().value!);
    } else if (operation === "invoke_agent") {
      const final = this.finalRequests.get(key);
      this.finalRequests.delete(key);
      if (final?.failed) {
        span.status.code = 2;
        span.attributes[SC.FI_AGENT_OUTCOME] = "error";
        if (this.config.hideErrorDetails) {
          for (const field of outputFields) delete span.attributes[field];
          delete span.attributes[SC.OUTPUT_VALUE];
        }
      }
    }
    if (
      span.attributes["gen_ai.operation.name"] === "invoke_agent" &&
      this.interruptedTurns.delete(key)
    )
      span.attributes[SC.FI_AGENT_OUTCOME] = "interrupted";
    if (
      span.attributes["gen_ai.operation.name"] === "chat" &&
      span.attributes[SC.FI_AGENT_OUTCOME] === "interrupted" &&
      span.parentSpanContext
    ) {
      this.interruptedTurns.add(
        `${context.traceId}:${span.parentSpanContext.spanId}`,
      );
      if (this.interruptedTurns.size > 1024)
        this.interruptedTurns.delete(
          this.interruptedTurns.values().next().value!,
        );
    }
    return span;
  }
}

/** A line exceeding the ingress limit is discarded through the next newline, never accumulated. */
export class NativeLines {
  private buffer = Buffer.alloc(0);
  private discarding = false;
  constructor(
    private readonly accept: (record: unknown) => void,
    private readonly dropped: () => void,
  ) {}
  push(chunk: Buffer): void {
    let from = 0;
    while (from < chunk.length) {
      const newline = chunk.indexOf(10, from),
        end = newline < 0 ? chunk.length : newline;
      const part = chunk.subarray(from, end);
      if (!this.discarding) {
        if (this.buffer.length + part.length > MAX_LINE_BYTES) {
          this.discarding = true;
          this.buffer = Buffer.alloc(0);
          this.dropped();
        } else this.buffer = Buffer.concat([this.buffer, part]);
      }
      if (newline >= 0) {
        if (!this.discarding && this.buffer.length) {
          try {
            this.accept(JSON.parse(this.buffer.toString("utf8")));
          } catch {
            this.dropped();
          }
        }
        this.discarding = false;
        this.buffer = Buffer.alloc(0);
      }
      from = end + 1;
    }
  }
  end(): void {
    if (this.buffer.length || this.discarding) this.dropped();
    this.buffer = Buffer.alloc(0);
    this.discarding = false;
  }
}
