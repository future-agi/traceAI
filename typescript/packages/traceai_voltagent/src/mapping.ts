import type { Attributes, AttributeValue } from "@opentelemetry/api";
import { FISpanKind } from "@traceai/fi-semantic-conventions";
import { FIAttributes, VoltAgentAttributes as VA, VoltAgentFIAttributes } from "./attributes";
import {
  type ContentPolicy,
  type ScrubContext,
  hidesFreeText,
  redactMessageText,
  scrubContent,
  scrubErrorText,
} from "./privacy";

export interface MapOptions {
  /** The resolved TraceConfig flags (`resolveContentPolicy`). Content is exported unless hidden. */
  policy: ContentPolicy;
  /** The working and home directories rewritten to `[cwd]` / `~` in exported strings. */
  scrub?: ScrubContext;
}

export interface MappedAttributes {
  attributes: Attributes;
  /** Future AGI span kind, or undefined when the span carries no VoltAgent type. */
  kind?: FISpanKind;
  /** True for VoltAgent `span.type=llm` spans: the only spans that keep promoted token keys. */
  isModelCall: boolean;
  /** True for the root span of an agent operation (`agent.state` set, no `span.type`). */
  isOperationRoot: boolean;
  operationId?: string;
}

/**
 * Token and cost keys the Future AGI collector promotes into hot columns on any span kind.
 * Observe sums them over every span of a trace, so they may appear only on per-model-call spans.
 * The cost-part keys count too: with no cost total, the collector stores input + output
 * (fi-collector pkg/adapter/adapter.go DeriveHotKeys, costInputKeys / costOutputKeys).
 */
const PROMOTED_EXACT = new Set<string>([
  "gen_ai.usage.input_tokens",
  "gen_ai.usage.output_tokens",
  "gen_ai.usage.total_tokens",
  "gen_ai.cost.total",
  "llm.cost.total",
  "gen_ai.cost.input",
  "gen_ai.cost.output",
  "llm.cost.prompt",
  "llm.cost.completion",
]);
const PROMOTED_PREFIXES = ["llm.token_count.", "llm.usage."];

export function isPromotedUsageKey(key: string): boolean {
  return PROMOTED_EXACT.has(key) || PROMOTED_PREFIXES.some((prefix) => key.startsWith(prefix));
}

/**
 * Content-bearing keys VoltAgent 2.11.0 writes, by side. Input-side keys are hidden by
 * `hideInputs` (FI_HIDE_INPUTS), output-side keys by `hideOutputs` (FI_HIDE_OUTPUTS), and keys that
 * hold both (state snapshots, eval metadata, ...) by either. Exported by default (TraceConfig).
 */
const INPUT_EXACT = new Set<string>([
  "input",
  "agent.instructions",
  "agent.context",
  "workflow.context",
  "workspace.sandbox.command",
  "workspace.sandbox.args",
  // Workspace tool arguments: model-chosen paths and glob patterns (also path-scrubbed when shown).
  "workspace.fs.path",
  "workspace.fs.pattern",
  "workspace.sandbox.cwd",
  // PlanAgent: the delegation brief.
  "planagent.task.description",
  // The workflow resume payload (workflow.resumed event).
  "resume.data",
  "input.value",
  "gen_ai.prompts",
  "gen_ai.system_instructions",
]);
/** Message lists sent to the model: hidden by hideInputs or hideInputMessages; text by hideInputText. */
const INPUT_MESSAGE_KEYS = new Set<string>(["llm.messages", "agent.messages", "agent.messages.ui", "gen_ai.input.messages"]);
const OUTPUT_EXACT = new Set<string>([
  "output",
  "output.value",
  // Model-written summary (agent/apply-summarization.ts, span.type=summary).
  "agent.summary.preview",
  "agent.summary.text",
  // PlanAgent: the plan and the subagent's answer.
  "planagent.todos",
  "planagent.task.response_preview",
  // Each streamed answer chunk an output guardrail sees (guardrail.stream.process event).
  "guardrail.chunk.text",
]);
const OUTPUT_MESSAGE_KEYS = new Set<string>(["gen_ai.output.messages"]);
const BOTH_EXACT = new Set<string>([
  "agent.stateSnapshot",
  // The workflow root's snapshot: step source code plus the run input (workflow/core.ts).
  "workflow.stateSnapshot",
  "suspension.checkpoint",
  "suspension.data",
  // The whole working-memory document (agent.ts, on the agent root).
  "agent.workingMemory.finalContent",
  // Evals: finalizeScorerSpan stores combineEvalMetadata(...) here, with the run's input and output.
  "eval.scorer.metadata",
  // The eval's reference answer.
  "eval.expected",
  // A guardrail decision's metadata: whatever the guardrail returns, often the matched text.
  // (guardrail.metadata is the guardrail's own static config and stays.)
  "guardrail.result.metadata",
]);
/** A final segment equal to one of these marks input content (tool.search.query, ...). */
const INPUT_LAST_SEGMENTS = new Set<string>(["messages", "instructions", "query", "context", "prompt", "prompts", "arguments", "args"]);
const OUTPUT_LAST_SEGMENTS = new Set<string>(["completion"]);
const BOTH_LAST_SEGMENTS = new Set<string>(["data", "checkpoint", "content"]);

export type ContentSide = "input" | "output" | "both";

/** Which side of the run a key's value comes from, or undefined when it is not content. */
export function contentSide(key: string, value: AttributeValue | undefined): ContentSide | undefined {
  if (INPUT_EXACT.has(key) || INPUT_MESSAGE_KEYS.has(key)) return "input";
  if (OUTPUT_EXACT.has(key) || OUTPUT_MESSAGE_KEYS.has(key)) return "output";
  if (BOTH_EXACT.has(key)) return "both";
  // Counts, flags and sizes are never content.
  if (typeof value === "number" || typeof value === "boolean") return undefined;
  const segments = key.split(".");
  // Any dotted segment input/output marks content (middleware.input.original, eval.output, ...).
  const isInput = segments.includes("input");
  const isOutput = segments.includes("output");
  if (isInput && isOutput) return "both";
  if (isInput) return "input";
  if (isOutput) return "output";
  const last = segments[segments.length - 1];
  if (INPUT_LAST_SEGMENTS.has(last)) return "input";
  if (OUTPUT_LAST_SEGMENTS.has(last)) return "output";
  if (BOTH_LAST_SEGMENTS.has(last)) return "both";
  return undefined;
}

export function isContentKey(key: string, value: AttributeValue | undefined): boolean {
  return contentSide(key, value) !== undefined;
}

/**
 * Free text written at run time: error messages and human-readable reasons, which can quote the
 * input or output (a tool error naming its argument, a custom guardrail's message). Exported with
 * paths, emails and local user/host names removed; hidden when inputs or outputs are hidden.
 */
const FREE_TEXT_EXACT = new Set<string>([
  "error.message",
  "exception.message",
  "eval.scorer.error_message",
  "workspace.error",
  "guardrail.message",
  "guardrail.reason",
  "suspension.reason",
  "cancellation.reason",
  "workflow.step.skipped_reason",
  "workflow.step.suspension_reason",
  "workflow.step.cancellation_reason",
]);

export function isFreeTextKey(key: string, value: AttributeValue | undefined): boolean {
  if (FREE_TEXT_EXACT.has(key)) return true;
  if (typeof value !== "string") return false;
  const last = key.split(".").pop() ?? "";
  if (last === "finish_reason") return false;
  return last === "message" || last === "error_message" || last === "reason" || last.endsWith("_reason");
}

/** Stack traces carry local paths and source lines: never exported. */
const STACK_SEGMENT = /^(stack|stacktrace|stack_trace|stacktraces)$/i;

export function isStackKey(key: string): boolean {
  return STACK_SEGMENT.test(key.split(".").pop() ?? "");
}

/** Credentials never belong on a span, whatever the content settings say. */
const SECRET_SEGMENT = /^(api[_-]?key|apikey|secret|secret[_-]?key|password|passwd|authorization|auth[_-]?token|access[_-]?token|bearer|cookie|set-cookie|x-api-key|x-secret-key|headers?)$/i;

export function isSecretKey(key: string): boolean {
  return key.split(".").some((segment) => SECRET_SEGMENT.test(segment));
}

function hiddenBySide(side: ContentSide, key: string, policy: ContentPolicy): boolean {
  if (side === "both") return policy.hideInputs || policy.hideOutputs;
  if (side === "input") {
    if (policy.hideInputs) return true;
    return INPUT_MESSAGE_KEYS.has(key) && policy.hideInputMessages;
  }
  if (policy.hideOutputs) return true;
  return OUTPUT_MESSAGE_KEYS.has(key) && policy.hideOutputMessages;
}

/**
 * The value one VoltAgent attribute gets on the exported copy, or undefined to drop it.
 * Order: credentials and stack traces are always dropped; free text and content follow the
 * TraceConfig flags; every string that is kept has its local paths and stack frames removed.
 */
export function exportValue(key: string, value: AttributeValue | undefined, options: MapOptions): AttributeValue | undefined {
  if (value === undefined || value === null) return undefined;
  if (isSecretKey(key) || isStackKey(key)) return undefined;
  const { policy, scrub } = options;
  if (isFreeTextKey(key, value)) {
    if (hidesFreeText(policy)) return undefined;
    return typeof value === "string" ? scrubErrorText(value, scrub) : value;
  }
  const side = contentSide(key, value);
  if (side !== undefined && hiddenBySide(side, key, policy)) return undefined;
  let out: AttributeValue = value;
  if (
    typeof out === "string" &&
    ((INPUT_MESSAGE_KEYS.has(key) && policy.hideInputText) || (OUTPUT_MESSAGE_KEYS.has(key) && policy.hideOutputText))
  ) {
    const redacted = redactMessageText(out);
    if (redacted === undefined) return undefined;
    out = redacted;
  }
  return scrubValue(out, scrub);
}

function scrubValue(value: AttributeValue, scrub?: ScrubContext): AttributeValue {
  if (typeof value === "string") return scrubContent(value, scrub);
  if (Array.isArray(value)) {
    return value.map((item) => (typeof item === "string" ? scrubContent(item, scrub) : item)) as AttributeValue;
  }
  return value;
}

/** A span event as `ReadableSpan.events` holds it. */
export interface SpanEventLike {
  name: string;
  attributes?: Attributes;
}

/**
 * Filter span event attributes the way span attributes are filtered (`exportValue`): credentials
 * and stack traces (`exception.stacktrace`) are always removed, content and error text follow the
 * TraceConfig flags, local paths are rewritten. Event names, times and every other field are kept.
 * Returns new event objects; the events of the span other processors hold are never mutated.
 */
export function mapSpanEvents<T extends SpanEventLike>(events: readonly T[] | undefined, options: MapOptions): T[] {
  if (!events) return [];
  return events.map((event) => {
    if (!event.attributes) return { ...event };
    const attributes: Attributes = {};
    for (const [key, value] of Object.entries(event.attributes)) {
      const exported = exportValue(key, value, options);
      if (exported !== undefined) attributes[key] = exported;
    }
    return { ...event, attributes };
  });
}

/**
 * The first segment of an AI SDK model's `provider` id ("openai.chat", "anthropic.messages",
 * "google.generative-ai", "amazon-bedrock", ...) -> gen_ai.provider.name. Ids read from the
 * published @ai-sdk/* providers; values are the fi-semantic-conventions LLMProvider values, plus
 * "vertexai" (the Future AGI backend's Vertex AI value). Any other id, including an
 * OpenAI-compatible provider's own name, is left unset.
 */
const AI_SDK_PROVIDER_PREFIXES: Readonly<Record<string, string>> = {
  openai: "openai",
  anthropic: "anthropic",
  google: "google",
  vertex: "vertexai",
  azure: "azure",
  "amazon-bedrock": "aws",
  bedrock: "aws",
  mistral: "mistralai",
  groq: "groq",
  xai: "xai",
  deepseek: "deepseek",
  cohere: "cohere",
  cerebras: "cerebras",
  togetherai: "together",
  fireworks: "fireworks",
  ollama: "ollama",
  huggingface: "huggingface",
};

export function providerFromAiSdk(providerId: string): string | undefined {
  const id = providerId.trim().toLowerCase();
  // @ai-sdk/google-vertex models report "google.vertex.<type>".
  if (id.startsWith("google.vertex")) return "vertexai";
  return AI_SDK_PROVIDER_PREFIXES[id.split(".")[0]];
}

const asString = (value: AttributeValue | undefined): string | undefined =>
  typeof value === "string" && value.length > 0 ? value : undefined;
const asNumber = (value: AttributeValue | undefined): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

/**
 * Kind map (architecture section 3, refined with the 2.11.0 inventory):
 * agent root or span.type=agent -> AGENT, llm -> LLM, tool -> TOOL, retriever/vector -> RETRIEVER,
 * embedding -> EMBEDDING, memory read -> RETRIEVER, memory write -> CHAIN,
 * guardrail/middleware/summary/workflow/unknown types -> CHAIN.
 */
export function resolveSpanKind(attributes: Attributes): FISpanKind | undefined {
  const spanType = asString(attributes[VA.SPAN_TYPE]);
  const entityType = asString(attributes[VA.ENTITY_TYPE]);
  if (spanType) {
    switch (spanType) {
      case "agent":
        return FISpanKind.AGENT;
      case "llm":
        return FISpanKind.LLM;
      case "tool":
        return FISpanKind.TOOL;
      case "retriever":
      case "vector":
        return FISpanKind.RETRIEVER;
      case "embedding":
        return FISpanKind.EMBEDDING;
      case "memory":
        return attributes[VA.MEMORY_OPERATION] === "read" ? FISpanKind.RETRIEVER : FISpanKind.CHAIN;
      default:
        // guardrail, middleware, summary, workflow-step, and any type added later.
        return FISpanKind.CHAIN;
    }
  }
  if (entityType === "agent") return FISpanKind.AGENT;
  if (entityType === "workflow") return FISpanKind.CHAIN;
  return undefined;
}

const OPERATION_NAMES: Partial<Record<FISpanKind, string>> = {
  [FISpanKind.AGENT]: "invoke_agent",
  [FISpanKind.LLM]: "chat",
  [FISpanKind.TOOL]: "execute_tool",
  [FISpanKind.EMBEDDING]: "embeddings",
};

/** VoltAgent usage key -> namespaced key used on non-model spans (agent root). */
const ROOT_USAGE_TO_NAMESPACED: Array<[string, string]> = [
  [VA.USAGE_PROMPT_TOKENS, "input_tokens"],
  [VA.USAGE_COMPLETION_TOKENS, "output_tokens"],
  [VA.USAGE_TOTAL_TOKENS, "total_tokens"],
  [VA.USAGE_CACHED_TOKENS, "cache_read_tokens"],
  [VA.USAGE_REASONING_TOKENS, "reasoning_tokens"],
];

/** Writes GenAI usage keys for a model-call span. */
export function setModelCallUsage(
  attributes: Attributes,
  usage: { input?: number; output?: number; total?: number; cached?: number; reasoning?: number },
): void {
  if (usage.input !== undefined) attributes[FIAttributes.GEN_AI_USAGE_INPUT_TOKENS] = usage.input;
  if (usage.output !== undefined) attributes[FIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = usage.output;
  const total =
    usage.total ??
    (usage.input !== undefined && usage.output !== undefined ? usage.input + usage.output : undefined);
  if (total !== undefined) attributes[FIAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = total;
  if (usage.cached !== undefined && usage.cached > 0) {
    attributes[FIAttributes.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] = usage.cached;
    attributes[FIAttributes.FI_USAGE_CACHE_READ_TOKENS] = usage.cached;
  }
  if (usage.reasoning !== undefined && usage.reasoning > 0) {
    attributes[FIAttributes.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS] = usage.reasoning;
    attributes[FIAttributes.FI_USAGE_REASONING_TOKENS] = usage.reasoning;
  }
}

export function readLLMSpanUsage(attributes: Attributes) {
  return {
    input: asNumber(attributes[VA.LLM_USAGE_PROMPT_TOKENS]),
    output: asNumber(attributes[VA.LLM_USAGE_COMPLETION_TOKENS]),
    total: asNumber(attributes[VA.LLM_USAGE_TOTAL_TOKENS]),
    cached: asNumber(attributes[VA.LLM_USAGE_CACHED_TOKENS]),
    reasoning: asNumber(attributes[VA.LLM_USAGE_REASONING_TOKENS]),
  };
}

export function readRootUsage(attributes: Attributes) {
  return {
    input: asNumber(attributes[VA.USAGE_PROMPT_TOKENS]),
    output: asNumber(attributes[VA.USAGE_COMPLETION_TOKENS]),
    total: asNumber(attributes[VA.USAGE_TOTAL_TOKENS]),
    cached: asNumber(attributes[VA.USAGE_CACHED_TOKENS]),
    reasoning: asNumber(attributes[VA.USAGE_REASONING_TOKENS]),
  };
}

function setIfAbsent(attributes: Attributes, key: string, value: AttributeValue | undefined): void {
  if (value === undefined || value === null) return;
  if (attributes[key] !== undefined) return;
  attributes[key] = value;
}

/**
 * Map one VoltAgent span's attributes onto Future AGI / GenAI keys.
 *
 * Copy, do not delete: VoltAgent's own keys are kept, except content the TraceConfig flags hide,
 * credentials, stack traces, and promoted token keys on spans that are not model calls (moved
 * under `voltagent.`). Kept strings have local paths and stack frames removed (`exportValue`).
 * The input object is never mutated; the result is a new object.
 */
export function mapVoltAgentAttributes(source: Attributes, options: MapOptions): MappedAttributes {
  const kind = resolveSpanKind(source);
  const isModelCall = kind === FISpanKind.LLM;
  const spanType = asString(source[VA.SPAN_TYPE]);
  const isOperationRoot =
    spanType === undefined &&
    asString(source[VA.ENTITY_TYPE]) === "agent" &&
    source[VA.AGENT_STATE] !== undefined;
  const operationId = asString(source[VA.OPERATION_ID]);

  const out: Attributes = {};
  for (const [key, raw] of Object.entries(source)) {
    const value = exportValue(key, raw, options);
    if (value === undefined) continue;
    if (!isModelCall && isPromotedUsageKey(key)) {
      // Summed or derived usage on a non-model span: keep it, but not on a promoted key.
      out[`voltagent.${key}`] = value;
      continue;
    }
    out[key] = value;
  }

  if (kind !== undefined && out[FIAttributes.FI_SPAN_KIND] === undefined) {
    out[FIAttributes.FI_SPAN_KIND] = kind;
    setIfAbsent(out, FIAttributes.GEN_AI_SPAN_KIND, kind);
    setIfAbsent(out, FIAttributes.OPENINFERENCE_SPAN_KIND, kind);
    setIfAbsent(out, FIAttributes.GEN_AI_OPERATION_NAME, OPERATION_NAMES[kind]);
    setIfAbsent(out, VoltAgentFIAttributes.SOURCE_SPAN_TYPE, spanType ?? asString(source[VA.ENTITY_TYPE]));
  }

  // Session and user.
  const conversationId = asString(source[VA.CONVERSATION_ID]);
  setIfAbsent(out, FIAttributes.SESSION_ID, conversationId);
  setIfAbsent(out, FIAttributes.GEN_AI_CONVERSATION_ID, conversationId);
  // user.id is already the Future AGI key (SemanticConventions.USER_ID); nothing to copy.

  // Model, provider, request parameters.
  const modelName = isModelCall
    ? asString(source[VA.LLM_MODEL]) ?? asString(source[VA.AI_MODEL_NAME])
    : asString(source[VA.AI_MODEL_NAME]);
  setIfAbsent(out, FIAttributes.GEN_AI_REQUEST_MODEL, modelName);
  setIfAbsent(out, FIAttributes.GEN_AI_RESPONSE_MODEL, modelName);
  setIfAbsent(
    out,
    FIAttributes.GEN_AI_PROVIDER_NAME,
    asString(source[VA.LLM_PROVIDER]) ?? asString(source[VA.AI_MODEL_PROVIDER]),
  );
  setIfAbsent(
    out,
    FIAttributes.GEN_AI_REQUEST_TEMPERATURE,
    asNumber(source[VA.LLM_TEMPERATURE]) ?? asNumber(source[VA.AI_MODEL_TEMPERATURE]),
  );
  setIfAbsent(
    out,
    FIAttributes.GEN_AI_REQUEST_MAX_TOKENS,
    asNumber(source[VA.LLM_MAX_OUTPUT_TOKENS]) ?? asNumber(source[VA.AI_MODEL_MAX_TOKENS]),
  );
  setIfAbsent(
    out,
    FIAttributes.GEN_AI_REQUEST_TOP_P,
    asNumber(source[VA.LLM_TOP_P]) ?? asNumber(source[VA.AI_MODEL_TOP_P]),
  );
  const finishReason =
    asString(source[VA.LLM_FINISH_REASON]) ?? asString(source[VA.AI_RESPONSE_FINISH_REASON]);
  if (finishReason) setIfAbsent(out, FIAttributes.GEN_AI_RESPONSE_FINISH_REASONS, [finishReason]);

  // Usage. Promoted keys only on model-call spans.
  if (isModelCall) {
    const llmUsage = readLLMSpanUsage(source);
    const hasLLMUsage = Object.values(llmUsage).some((value) => value !== undefined);
    setModelCallUsage(out, hasLLMUsage ? llmUsage : readRootUsage(source));
  } else {
    for (const [sourceKey, suffix] of ROOT_USAGE_TO_NAMESPACED) {
      setIfAbsent(out, `${VoltAgentFIAttributes.USAGE_PREFIX}${suffix}`, asNumber(source[sourceKey]));
    }
  }

  // Tools.
  if (kind === FISpanKind.TOOL) {
    setIfAbsent(out, FIAttributes.GEN_AI_TOOL_NAME, asString(source[VA.TOOL_NAME]));
    setIfAbsent(out, FIAttributes.GEN_AI_TOOL_CALL_ID, asString(source[VA.TOOL_CALL_ID]));
    setIfAbsent(out, FIAttributes.GEN_AI_TOOL_DESCRIPTION, asString(source[VA.TOOL_DESCRIPTION]));
  }

  // input.value / output.value (the collector's input and output columns), from the values kept
  // above: what the TraceConfig flags hide is already gone, and paths are already rewritten.
  // VoltAgent writes no `input` on llm spans; their prompt is only in `llm.messages`.
  const input =
    asString(out[VA.INPUT]) ??
    asString(out[VA.VECTOR_QUERY]) ??
    asString(out[VA.EMBEDDING_QUERY]) ??
    (isModelCall ? asString(out[VA.LLM_MESSAGES]) : undefined);
  if (input !== undefined && out[FIAttributes.INPUT_VALUE] === undefined) {
    out[FIAttributes.INPUT_VALUE] = input;
    setIfAbsent(out, FIAttributes.INPUT_MIME_TYPE, looksLikeJson(input) ? "application/json" : "text/plain");
  }
  const output = asString(out[VA.OUTPUT]);
  if (output !== undefined && out[FIAttributes.OUTPUT_VALUE] === undefined) {
    out[FIAttributes.OUTPUT_VALUE] = output;
    setIfAbsent(out, FIAttributes.OUTPUT_MIME_TYPE, looksLikeJson(output) ? "application/json" : "text/plain");
  }

  return { attributes: out, kind, isModelCall, isOperationRoot, operationId };
}

function looksLikeJson(value: string): boolean {
  const trimmed = value.trimStart();
  return trimmed.startsWith("{") || trimmed.startsWith("[");
}

/**
 * llm.operation values of the main model call, whose usage VoltAgent also rolls up onto the
 * operation root. In 2.11.0 the agent opens an llm span (createLLMSpan) only for generateText,
 * streamText, generateTitle (conversation title; not rolled up), a model-resolution failure (ERROR)
 * and runInternalGenerateText (a provider tool run through tool routing; also `generateText`).
 * generateObject / streamObject open no llm span: their usage is only on the root, where it is
 * exported as `voltagent.usage.*`.
 */
const ROOT_ROLLED_UP_OPERATIONS = new Set(["generateText", "streamText"]);

/** The one successful main-call llm span of an operation, or undefined when there are none or several. */
function soleMainCall<T extends { source: Attributes; isError: boolean }>(llmSpans: T[]): T | undefined {
  const candidates = llmSpans.filter(
    (span) =>
      !span.isError &&
      span.source[VA.LLM_MODEL_RESOLUTION_FAILED] !== true &&
      ROOT_ROLLED_UP_OPERATIONS.has(String(span.source[VA.LLM_OPERATION])),
  );
  return candidates.length === 1 ? candidates[0] : undefined;
}

/**
 * VoltAgent 2.11.0 records no response text on llm spans (agent.ts createLLMSpanFinalizer sets
 * usage, finish reason and status only). With exactly one successful main-call llm span, that
 * span's final answer is the operation's `output` on the root span, so the exported copy of the
 * llm span gets it as output.value. Pass the root's MAPPED attributes (`mapVoltAgentAttributes`):
 * their `output` is absent when outputs are hidden and already has paths rewritten. Returns the
 * updated span attributes, if any.
 */
export function copyOperationOutput(
  rootAttributes: Attributes,
  llmSpans: Array<{ source: Attributes; attributes: Attributes; isError: boolean }>,
): Attributes | undefined {
  const output = asString(rootAttributes[VA.OUTPUT]);
  if (output === undefined) return undefined;
  const target = soleMainCall(llmSpans);
  if (!target || target.attributes[FIAttributes.OUTPUT_VALUE] !== undefined) return undefined;
  target.attributes[FIAttributes.OUTPUT_VALUE] = output;
  target.attributes[FIAttributes.OUTPUT_MIME_TYPE] = looksLikeJson(output) ? "application/json" : "text/plain";
  return target.attributes;
}

/**
 * VoltAgent 2.11.0 wraps a whole multi-step AI SDK call in one `llm:<operation>` span and records
 * `response.usage` on it, which is the LAST step only (agent.ts finalizeLLMSpan), while the
 * operation root records `totalUsage` across all steps (usage-normalizer.ts resolveFinishUsage).
 *
 * When exactly one successful main-call llm span exists for an operation and the root total is
 * larger, the exported copy of that llm span carries the root total on the promoted keys, so the
 * trace-wide sum equals the tokens of every model step. The last-step values are kept under
 * `voltagent.llm.last_step_usage.*`. Returns the reconciled span attributes, if any.
 */
export function reconcileOperationUsage(
  rootSource: Attributes,
  llmSpans: Array<{ source: Attributes; attributes: Attributes; isError: boolean }>,
): Attributes | undefined {
  const rootUsage = readRootUsage(rootSource);
  if (rootUsage.input === undefined && rootUsage.output === undefined) return undefined;

  const target = soleMainCall(llmSpans);
  if (!target) return undefined;
  const stepUsage = readLLMSpanUsage(target.source);
  const grows = (total?: number, step?: number) => total !== undefined && (step === undefined || total > step);
  if (!grows(rootUsage.input, stepUsage.input) && !grows(rootUsage.output, stepUsage.output)) {
    return undefined;
  }

  const attributes = target.attributes;
  const keep: Array<[string, number | undefined]> = [
    ["prompt_tokens", stepUsage.input],
    ["completion_tokens", stepUsage.output],
    ["total_tokens", stepUsage.total],
    ["cached_tokens", stepUsage.cached],
    ["reasoning_tokens", stepUsage.reasoning],
  ];
  for (const [suffix, value] of keep) {
    if (value !== undefined) attributes[`${VoltAgentFIAttributes.LAST_STEP_USAGE_PREFIX}${suffix}`] = value;
  }
  // Every promoted key on this span must agree, or the collector could pick the stale one.
  for (const key of Object.keys(attributes)) {
    if (isPromotedUsageKey(key)) delete attributes[key];
  }
  for (const key of [
    FIAttributes.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    FIAttributes.FI_USAGE_CACHE_READ_TOKENS,
    FIAttributes.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
    FIAttributes.FI_USAGE_REASONING_TOKENS,
  ]) {
    delete attributes[key];
  }
  const total =
    rootUsage.total ??
    (rootUsage.input !== undefined && rootUsage.output !== undefined ? rootUsage.input + rootUsage.output : undefined);
  if (rootUsage.input !== undefined) attributes[VA.LLM_USAGE_PROMPT_TOKENS] = rootUsage.input;
  if (rootUsage.output !== undefined) attributes[VA.LLM_USAGE_COMPLETION_TOKENS] = rootUsage.output;
  if (total !== undefined) attributes[VA.LLM_USAGE_TOTAL_TOKENS] = total;
  if (rootUsage.cached !== undefined && rootUsage.cached > 0) attributes[VA.LLM_USAGE_CACHED_TOKENS] = rootUsage.cached;
  if (rootUsage.reasoning !== undefined && rootUsage.reasoning > 0) {
    attributes[VA.LLM_USAGE_REASONING_TOKENS] = rootUsage.reasoning;
  }
  setModelCallUsage(attributes, { ...rootUsage, total });
  attributes[VoltAgentFIAttributes.USAGE_RECONCILED] = true;
  return attributes;
}
