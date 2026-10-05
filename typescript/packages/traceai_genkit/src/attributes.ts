/**
 * Genkit span attribute inventory and the Future AGI keys this package writes.
 *
 * Every Genkit key below was read from the installed genkit 1.42.0 sources
 * (@genkit-ai/core and @genkit-ai/ai) and confirmed on spans dumped by
 * contract/inventory.mjs (Genkit's own `mockModel` from `genkit/testing`).
 * Nothing here is a guessed path.
 */
import { FISpanKind, SemanticConventions } from "@traceai/fi-semantic-conventions";

/**
 * Attribute keys Genkit 1.42.0 writes on its spans.
 *
 * - `genkit:type`, `genkit:metadata:subtype`, `genkit:key` are labels set at span
 *   start (core/src/action.ts:538-543, core/src/flow.ts:219-221,
 *   ai/src/generate/action.ts:147-149, ai/src/prompt.ts:273-275 and 490-492).
 * - The rest come from `metadataToAttributes()` at span end
 *   (core/src/tracing/instrumentation.ts:176, 221-243): every SpanMetadata field
 *   becomes `genkit:<field>`, and `metadata.metadata` entries become
 *   `genkit:metadata:<key>`.
 */
export const GenkitAttributes = {
  TYPE: "genkit:type",
  SUBTYPE: "genkit:metadata:subtype",
  KEY: "genkit:key",
  NAME: "genkit:name",
  PATH: "genkit:path",
  STATE: "genkit:state",
  IS_ROOT: "genkit:isRoot",
  IS_FAILURE_SOURCE: "genkit:isFailureSource",
  INPUT: "genkit:input",
  OUTPUT: "genkit:output",
  INIT: "genkit:init",
  LAST_KNOWN_PARENT_SPAN_ID: "genkit:lastKnownParentSpanId",
  /** core/src/action.ts:557-559. JSON of the action context, `auth`/`secrets` redacted by Genkit. */
  METADATA_CONTEXT: "genkit:metadata:context",
  /** ai/src/tool.ts:538-540. Tool interrupt metadata (JSON). */
  METADATA_INTERRUPT: "genkit:metadata:interrupt",
  /** ai/src/tool.ts:569-571. Resumed-tool metadata (JSON). */
  METADATA_RESUMED: "genkit:metadata:resumed",
  /** ai/src/agent.ts:1061-1063 (beta agents). The agent session id. */
  METADATA_AGENT_SESSION_ID: "genkit:metadata:agent:sessionId",
  /** ai/src/prompt.ts:269-271. The prompt a `render` (promptTemplate) span rendered. */
  METADATA_PROMPT_NAME: "genkit:metadata:promptName",
  /** ai/src/agent.ts:486. The snapshot a server-managed agent turn persisted. */
  METADATA_AGENT_SNAPSHOT_ID: "genkit:metadata:agent:snapshotId",
} as const;

/** `genkit:type` values at 1.42.0. */
export const GenkitSpanType = {
  /** Every registered action (core/src/action.ts:539). The real kind is in `genkit:metadata:subtype`. */
  ACTION: "action",
  /** `ai.run()` steps inside a flow (core/src/flow.ts:220). */
  FLOW_STEP: "flowStep",
  /** The `generate` helper span around the whole tool loop (ai/src/generate/action.ts:148). */
  UTIL: "util",
  /** Prompt rendering (ai/src/prompt.ts:274). */
  PROMPT_TEMPLATE: "promptTemplate",
  /** Executable prompt call (ai/src/prompt.ts:491). */
  DOTPROMPT: "dotprompt",
} as const;

/**
 * `genkit:metadata:subtype` values on `genkit:type=action` spans: the
 * ActionType list in core/src/registry.ts:40-62 at 1.42.0.
 */
export const GENKIT_ACTION_SUBTYPES = [
  "custom",
  "dynamic-action-provider",
  "embedder",
  "evaluator",
  "executable-prompt",
  "flow",
  "indexer",
  "model",
  "background-model",
  "check-operation",
  "cancel-operation",
  "prompt",
  "reranker",
  "retriever",
  "tool",
  "tool.v2",
  "util",
  "resource",
  "agent",
  "agent-snapshot",
  "agent-abort",
] as const;

/** Action subtypes with a dedicated Future AGI kind. Every other action is CHAIN. */
export const SUBTYPE_TO_KIND: Readonly<Record<string, FISpanKind>> = {
  flow: FISpanKind.CHAIN,
  model: FISpanKind.LLM,
  "background-model": FISpanKind.LLM,
  tool: FISpanKind.TOOL,
  "tool.v2": FISpanKind.TOOL,
  retriever: FISpanKind.RETRIEVER,
  embedder: FISpanKind.EMBEDDING,
  reranker: FISpanKind.RERANKER,
  evaluator: FISpanKind.EVALUATOR,
  agent: FISpanKind.AGENT,
};

/** Non-action `genkit:type` values. All are orchestration spans. */
export const TYPE_TO_KIND: Readonly<Record<string, FISpanKind>> = {
  [GenkitSpanType.FLOW_STEP]: FISpanKind.CHAIN,
  [GenkitSpanType.UTIL]: FISpanKind.CHAIN,
  [GenkitSpanType.PROMPT_TEMPLATE]: FISpanKind.CHAIN,
  [GenkitSpanType.DOTPROMPT]: FISpanKind.CHAIN,
};

/**
 * Keys that carry user content or request context. Dropped unless
 * `captureContent: true`; `genkit:metadata:context` is dropped always.
 */
export const GENKIT_CONTENT_KEYS: readonly string[] = [
  GenkitAttributes.INPUT,
  GenkitAttributes.OUTPUT,
  GenkitAttributes.INIT,
  GenkitAttributes.METADATA_INTERRUPT,
  GenkitAttributes.METADATA_RESUMED,
];

/** Request context can hold credentials or headers. Never exported. */
export const GENKIT_NEVER_EXPORTED_KEYS: readonly string[] = [GenkitAttributes.METADATA_CONTEXT];

/**
 * GenerationUsage fields (ai/src/model-types.ts:260-275) copied from a model
 * span's `genkit:output.usage`, and the Future AGI key each one goes to.
 *
 * Only the fields the inventory saw on a dumped model span are mapped
 * (contract/inventory.mjs, Genkit's own `mockModel`): inputTokens,
 * outputTokens, totalTokens.
 */
export const USAGE_TO_ATTRIBUTE: Readonly<Record<string, string>> = {
  inputTokens: SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
  outputTokens: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
  totalTokens: SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
};

/**
 * GenerationUsageSchema fields at 1.42.0 that no inventoried span carried.
 * They are unavailable: not copied to any Future AGI key (they stay inside
 * `genkit:output`, which is exported only with `captureContent: true`).
 */
export const GENKIT_USAGE_NOT_MAPPED: readonly string[] = [
  "thoughtsTokens",
  "cachedContentTokens",
  "inputCharacters",
  "outputCharacters",
  "inputImages",
  "outputImages",
  "inputVideos",
  "outputVideos",
  "inputAudioFiles",
  "outputAudioFiles",
  "custom",
];

/** The Future AGI span-kind keys. fi-collector reads `fi.span.kind`, then `gen_ai.span.kind`. */
export const FI_SPAN_KIND = SemanticConventions.FI_SPAN_KIND;
export const GEN_AI_SPAN_KIND = "gen_ai.span.kind";
export const GEN_AI_TOOL_NAME = "gen_ai.tool.name";

/**
 * Prefix for usage/cost values found on a span that is not a model call. The
 * collector promotes the plain keys on any span kind and Observe sums them over
 * the trace, so they would double count.
 */
export const GENKIT_USAGE_PREFIX = "genkit.usage.";

/**
 * True for keys fi-collector promotes into token/cost columns on any span kind.
 * Cost is matched by prefix: with no total present the collector also promotes
 * the cost parts (`gen_ai.cost.input`/`output`, `llm.cost.prompt`/`completion`).
 */
export function isPromotedUsageKey(key: string): boolean {
  return (
    key === SemanticConventions.LLM_TOKEN_COUNT_PROMPT ||
    key === SemanticConventions.LLM_TOKEN_COUNT_COMPLETION ||
    key === SemanticConventions.LLM_TOKEN_COUNT_TOTAL ||
    key.startsWith("gen_ai.usage.") ||
    key.startsWith("llm.token_count.") ||
    key.startsWith("llm.usage.") ||
    key.startsWith("gen_ai.cost.") ||
    key.startsWith("llm.cost.")
  );
}
