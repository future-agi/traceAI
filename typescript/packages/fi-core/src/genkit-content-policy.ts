import type { Attributes } from "@opentelemetry/api";
import type { TraceConfigOptions } from "./trace/trace-config/types";

type ContentPolicy = TraceConfigOptions & { hideLLMInvocationParameters?: boolean };

/**
 * Exact non-content keys, not prefix exemptions. In particular usage.*, tool.*,
 * agent.* and request.* are NOT safe namespaces: a new/flattened child can carry
 * content. Add an exemption only when its complete key has a metadata contract.
 * Values still pass through fi-core and the ordinary path/error scrubbing.
 */
const METADATA_ATTRIBUTES: ReadonlySet<string> = new Set([
  "gen_ai.span.kind", "gen_ai.operation.name", "gen_ai.system", "gen_ai.provider.name",
  "gen_ai.request.model", "gen_ai.response.model", "gen_ai.response.id",
  "gen_ai.response.finish_reasons", "gen_ai.conversation.id",
  "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens",
  "gen_ai.usage.input_tokens.audio", "gen_ai.usage.output_tokens.audio",
  "gen_ai.usage.output_tokens.reasoning", "gen_ai.usage.cache_read_tokens", "gen_ai.usage.cache_write_tokens",
  "gen_ai.usage.cache_creation_tokens", "gen_ai.usage.cache_read_tokens.audio", "gen_ai.usage.provider_total_tokens",
  "gen_ai.cost.input", "gen_ai.cost.output", "gen_ai.cost.total",
  "gen_ai.response.latency", "gen_ai.response.time_to_first_chunk", "gen_ai.response.time_to_first_text",
  "gen_ai.response.time_to_first_token", "gen_ai.server.time_to_first_token",
  "gen_ai.server.time_per_output_token", "gen_ai.server.request.duration",
  "gen_ai.tool.name", "gen_ai.tool.type", "gen_ai.tool.call.id",
  "gen_ai.agent.id", "gen_ai.agent.name", "gen_ai.agent.version", "gen_ai.agent.status",
  "gen_ai.agent.step_count", "gen_ai.agent.iteration_count",
  "llm.model_name", "llm.provider", "llm.system", "llm.prompt_template.version",
  "llm.token_count.prompt", "llm.token_count.completion", "llm.token_count.total",
  "llm.token_count.prompt_details.cache_read", "llm.token_count.prompt_details.cache_write",
  "llm.token_count.prompt_details.audio", "llm.token_count.completion_details.audio",
  "llm.token_count.completion_details.reasoning",
  "tool.name", "tool.type", "tool.id", "tool_call.id", "tool_call.function.name",
  "agent.id", "agent.name", "agent.version", "agent.status", "agent.step_count", "agent.iteration_count",
  "message.role", "message.name", "message.tool_call_id", "message.function_call_name", "message_content.type",
  "embedding.model_name", "reranker.model_name", "reranker.top_k", "document.id", "document.score",
  "prompt.vendor", "prompt.id",
  "fi.agent.fidelity", "fi.agent.timing", "fi.agent.outcome", "fi.agent.usage.additive", "fi.agent.usage.plan",
  "fi.agent.usage.cache_in_input", "fi.agent.usage.reasoning_in_output", "fi.agent.cost.source",
  "fi.agent.cost.estimator", "fi.agent.gap", "fi.agent.transport", "fi.agent.source_call_id",
  "fi.agent.summary.input", "fi.agent.summary.output", "fi.agent.summary.total", "fi.agent.summary.cache_read",
  "fi.agent.summary.cache_write", "fi.agent.summary.reasoning", "fi.agent.summary.cost_usd", "fi.agent.event",
  "fi.agent.input.completeness", "fi.agent.content.source",
]);

type ContentSide = "input" | "output" | "both";

function family(key: string, root: string): boolean {
  // Flattened arrays may use either dots or brackets. Never accept partial words.
  return key === root || key.startsWith(root + ".") || key.startsWith(root + "[");
}

/**
 * A closed metadata allowlist and an open-ended content namespace policy.
 * Unknown content has BOTH sides: hiding either side must fail closed, rather
 * than treating an unrecognised spelling as metadata. Audio transcripts and
 * unqualified messages can be inputs or outputs; without a verifiable envelope
 * on every surface they deliberately use that same conservative default.
 */
export function semanticContentSide(key: string): ContentSide | undefined {
  if (METADATA_ATTRIBUTES.has(key)) return undefined;
  if (family(key, "db.vector.query.filter")) return "input";
  if (![
    "gen_ai", "llm", "input", "output", "raw.input", "raw.output", "tool", "tool_call", "agent", "fi.agent",
    "message", "messages", "message_content", "audio", "image", "prompt", "embedding", "retrieval", "reranker", "document",
  ].some(root => family(key, root))) return undefined;

  // Specific call payloads precede their ambiguous/root families. Arguments on
  // a tool invocation are input; a returned result is output. A model's whole
  // function-call record (legacy llm.function_call/tool_call) is output.
  if (["gen_ai.tool.call.arguments", "tool.input", "tool.arguments", "agent.input", "gen_ai.agent.input",
    "gen_ai.agent.instructions", "agent.instructions", "reranker.input_documents", "reranker.query",
  ].some(root => family(key, root))) return "input";
  if (["gen_ai.tool.call.result", "tool.output", "tool.result", "agent.output", "gen_ai.agent.output",
    "gen_ai.tool.call", "tool_call", "llm.function_call", "reranker.output_documents", "retrieval", "gen_ai.retrieval",
  ].some(root => family(key, root))) return "output";
  if (/^(?:gen_ai\.)?embedding(?:s)?(?:\.|\[|$)/.test(key)) {
    // Flattened semconv entries use embedding.embeddings.N.embedding.text:
    // the inner embedding namespace does not turn its input text into a vector.
    if (/(?:^|\.)text(?:\.|\[|$)/.test(key)) return "input";
    const field = key.replace(/^embedding(?:\.|(?=\[)|$)/, "");
    if (/(?:^|\.)vector(?:\.|\[|$)/.test(field) || /(?:^|\.)embedding(?:\.\d+|\[\d+\])*$/.test(field)) return "output";
    return "both";
  }
  if (["input", "raw.input", "gen_ai.input", "gen_ai.prompt", "gen_ai.prompts", "gen_ai.system_instructions",
    "gen_ai.request", "gen_ai.tool.definitions", "gen_ai.tool.description", "llm.input_messages", "llm.prompts",
    "llm.prompt_template", "llm.invocation_parameters", "llm.tools", "tool.description", "tool.parameters",
    "tool.json_schema", "prompt", "image",
  ].some(root => family(key, root))) return "input";
  if (["output", "raw.output", "gen_ai.output", "gen_ai.completion", "gen_ai.completions", "gen_ai.response",
    "llm.output_messages",
  ].some(root => family(key, root))) return "output";
  return "both";
}

export function hideSemanticContent(key: string, policy: ContentPolicy): boolean {
  const side = semanticContentSide(key);
  if (side === undefined) return false;
  const input = policy.hideInputs || policy.hideInputMessages || policy.hideInputText || policy.hideInputImages;
  const output = policy.hideOutputs || policy.hideOutputMessages || policy.hideOutputText;
  const invocation = policy.hideLLMInvocationParameters && [
    "gen_ai.request", "gen_ai.tool.definitions", "llm.invocation_parameters", "llm.tools", "tool.parameters", "tool.json_schema",
  ].some(root => family(key, root));
  const vectors = policy.hideEmbeddingVectors && /^(?:gen_ai\.)?embedding(?:s)?(?:\.|\[|$)/.test(key) && side !== "input";
  return !!(invocation || vectors || (side !== "output" && input) || (side !== "input" && output));
}

// Field-masked native envelopes are not opaque semantic aliases. Keep a private,
// value-bound receipt so a later core boundary does not replace their safe roles,
// usage and operation classification with a whole-value marker. No exported
// attribute can grant this exception, and changed values lose the receipt.
type AliasReceipt = { policy: ContentPolicy; values: Map<string, unknown> };
const ALIASES = Symbol.for("traceai.genkit.mapped-aliases.v1");
const host = globalThis as typeof globalThis & { [ALIASES]?: WeakMap<object, AliasReceipt> };
const aliases = host[ALIASES] ??= new WeakMap<object, AliasReceipt>();
export function rememberGenkitAliases(attributes: Attributes, policy: ContentPolicy, keys: readonly string[]): void {
  aliases.set(attributes, { policy: { ...policy }, values: new Map(keys.map(key => [key, attributes[key]])) });
}
export function isGenkitMappedAlias(attributes: object, key: string, value: unknown, policy: ContentPolicy): boolean {
  const receipt = aliases.get(attributes);
  if (!receipt || !receipt.values.has(key) || receipt.values.get(key) !== value) return false;
  const flags = key.startsWith("input.")
    ? ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"] as const
    : ["hideOutputs", "hideOutputMessages", "hideOutputText", "hideEmbeddingVectors"] as const;
  // The export owner may request stronger hiding than the mapper. Never use a
  // weaker receipt to override that owner, including an environment-only switch.
  return flags.every(flag => !policy[flag] || receipt.policy[flag]);
}
export function carryGenkitAliases(source: object, target: Attributes): void {
  const receipt = aliases.get(source);
  if (receipt) rememberGenkitAliases(target, receipt.policy, [...receipt.values.keys()].filter(key => key in target));
}
