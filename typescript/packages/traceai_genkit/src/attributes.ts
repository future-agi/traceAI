/**
 * Genkit span attribute inventory and the Future AGI keys this package writes.
 *
 * Every Genkit key below was read from the installed genkit 1.42.0 sources
 * (@genkit-ai/core and @genkit-ai/ai) and confirmed on spans dumped by
 * contract/inventory.mjs (Genkit's own `mockModel` from `genkit/testing`).
 * Nothing here is a guessed path.
 */
import { FISpanKind, LLMProvider, SemanticConventions } from "@traceai/fi-semantic-conventions";

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
 * Input-side content keys: hidden by `hideInputs` (FI_HIDE_INPUTS), and on model spans also by
 * `hideInputMessages` (the model request is the message list). `genkit:init` is a beta agent's
 * initial state; `genkit:metadata:resumed` is the data a resumed tool call was given.
 */
export const GENKIT_INPUT_KEYS: readonly string[] = [
  GenkitAttributes.INPUT,
  GenkitAttributes.INIT,
  GenkitAttributes.METADATA_RESUMED,
];

/**
 * Output-side content keys: hidden by `hideOutputs` (FI_HIDE_OUTPUTS), and on model spans also by
 * `hideOutputMessages`. `genkit:metadata:interrupt` is the payload a tool interrupt returned.
 */
export const GENKIT_OUTPUT_KEYS: readonly string[] = [GenkitAttributes.OUTPUT, GenkitAttributes.METADATA_INTERRUPT];

/** Every key that carries user content: {@link GENKIT_INPUT_KEYS} and {@link GENKIT_OUTPUT_KEYS}. */
export const GENKIT_CONTENT_KEYS: readonly string[] = [...GENKIT_INPUT_KEYS, ...GENKIT_OUTPUT_KEYS];

/** Request context can hold credentials or headers. Never exported. */
export const GENKIT_NEVER_EXPORTED_KEYS: readonly string[] = [GenkitAttributes.METADATA_CONTEXT];

/**
 * GenerationUsage fields (ai/src/model-types.ts:260-275) copied from a model
 * span's `genkit:output.usage`, and the Future AGI key each one goes to.
 *
 * inputTokens, outputTokens and totalTokens are on every inventoried model span
 * (contract/inventory.mjs, Genkit's own `mockModel`). thoughtsTokens and
 * cachedContentTokens are set by the Google AI / Vertex AI plugin
 * (@genkit-ai/google-genai 1.42.0 lib/googleai/gemini.js:642-646, from
 * usageMetadata.thoughtsTokenCount / cachedContentTokenCount) and were seen on
 * real gemini-3.8-flash spans. Gemini's outputTokens excludes the thinking
 * tokens, which only show up in totalTokens and thoughtsTokens.
 */
export const USAGE_TO_ATTRIBUTE: Readonly<Record<string, string>> = {
  inputTokens: SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
  outputTokens: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
  totalTokens: SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
  thoughtsTokens: SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
  cachedContentTokens: SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
};

/**
 * GenerationUsageSchema fields at 1.42.0 that neither the inventory nor the
 * google-genai plugin sets. They are unavailable: not copied to any Future AGI
 * key (they stay inside `genkit:output`, which is exported unless
 * `hideOutputs` / FI_HIDE_OUTPUTS hides it).
 */
export const GENKIT_USAGE_NOT_MAPPED: readonly string[] = [
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

/**
 * Model plugin namespace (the part of a model action name before the first
 * "/") -> `gen_ai.provider.name`. Only namespaces read from the published
 * plugins are listed: googleai / vertexai (@genkit-ai/google-genai), openai /
 * deepseek / xai (@genkit-ai/compat-oai), anthropic (genkitx-anthropic),
 * ollama (genkitx-ollama), mistral (genkitx-mistral), groq (genkitx-groq),
 * aws-bedrock (genkitx-aws-bedrock), azure-openai (genkitx-azure-openai). Any
 * other namespace leaves the provider unset. Values are the fi-semantic-conventions
 * LLMProvider values; "vertexai" is the Future AGI backend's provider value
 * for Vertex AI (FiLLMProviderValues.VERTEXAI), which LLMProvider lacks.
 */
export const MODEL_NAMESPACE_TO_PROVIDER: Readonly<Record<string, string>> = {
  googleai: LLMProvider.GOOGLE,
  vertexai: "vertexai",
  openai: LLMProvider.OPENAI,
  deepseek: LLMProvider.DEEPSEEK,
  xai: LLMProvider.XAI,
  anthropic: LLMProvider.ANTHROPIC,
  ollama: LLMProvider.OLLAMA,
  mistral: LLMProvider.MISTRALAI,
  groq: LLMProvider.GROQ,
  "aws-bedrock": LLMProvider.AWS,
  "azure-openai": LLMProvider.AZURE,
};

/**
 * Split a registered model action name ("googleai/gemini-3.5-flash") into the
 * plugin namespace and the vendor model id. fi-collector prices a span by an
 * exact model-name lookup in its litellm table, which has "gemini-3.5-flash"
 * and not "googleai/gemini-3.5-flash". A name with no "/" has no namespace.
 */
export function splitModelName(name: string): { namespace?: string; model: string } {
  const at = name.indexOf("/");
  if (at <= 0 || at === name.length - 1) return { model: name };
  return { namespace: name.slice(0, at), model: name.slice(at + 1) };
}

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
