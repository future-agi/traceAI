import { Attributes } from "@opentelemetry/api";
import { safelyJSONStringify, TraceConfig } from "@traceai/fi-core";
import { FISpanKind, LLMProvider, MimeType, SemanticConventions as S } from "@traceai/fi-semantic-conventions";

export type Operation = "chat" | "chatStream" | "embed" | "rerank";
export type Data = Record<string, unknown>;
export const object = (value: unknown): Data => value !== null && typeof value === "object" ? value as Data : {};
export const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const json = (value: unknown) => safelyJSONStringify(value) ?? "";
const field = (data: Data, camel: string, snake: string) => data[camel] ?? data[snake];
export const hideInput = (config: TraceConfig) => config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages;
export const hideOutput = (config: TraceConfig) => config.hideOutputs || config.hideOutputMessages || config.hideOutputText;

/** Connector authentication is never telemetry content, even with input capture
 * enabled. Project only the telemetry copy; the SDK still needs these tokens.
 */
function withoutConnectorCredentials(request: Data): Data {
  if (!Array.isArray(request.connectors)) return request;
  return {
    ...request,
    connectors: request.connectors.map((connector) => {
      const { userAccessToken: _camel, user_access_token: _snake, ...content } = object(connector);
      return content;
    }),
  };
}

export function inputAttributes(operation: Operation, request: Data, config: TraceConfig): Attributes {
  const embedding = operation === "embed";
  const rerank = operation === "rerank";
  const attrs: Attributes = {
    [S.FI_SPAN_KIND]: embedding ? FISpanKind.EMBEDDING : rerank ? FISpanKind.RERANKER : FISpanKind.LLM,
    [S.LLM_PROVIDER]: LLMProvider.COHERE,
    [S.GEN_AI_OPERATION_NAME]: embedding ? "embeddings" : rerank ? "rerank" : "chat",
  };
  if (typeof request.model === "string") {
    attrs[embedding ? S.EMBEDDING_MODEL_NAME : S.LLM_MODEL_NAME] = request.model;
    if (rerank) attrs[S.RERANKER_MODEL_NAME] = request.model;
  }
  if (embedding) {
    if (!hideInput(config)) {
      attrs[S.INPUT_VALUE] = json(request.texts ?? request.inputs ?? request.images);
      attrs[S.INPUT_MIME_TYPE] = MimeType.JSON;
      list(request.texts).forEach((text, index) => {
        if (typeof text === "string") attrs[`${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_TEXT}`] = text;
      });
    }
  } else if (rerank) {
    attrs["cohere.rerank.documents_count"] = list(request.documents).length;
    const topN = field(request, "topN", "top_n");
    if (typeof topN === "number") {
      attrs[S.RERANKER_TOP_K] = topN;
      attrs["cohere.rerank.top_n"] = topN;
    }
    if (!hideInput(config)) {
      attrs[S.INPUT_VALUE] = typeof request.query === "string" ? request.query : "";
      attrs[S.INPUT_MIME_TYPE] = MimeType.TEXT;
      attrs[S.RERANKER_QUERY] = attrs[S.INPUT_VALUE];
      attrs["cohere.rerank.query"] = attrs[S.INPUT_VALUE];
      attrs[S.RERANKER_INPUT_DOCUMENTS] = json(request.documents);
    }
  } else {
    // Only generation settings belong here. Chat history, tools, documents and
    // arbitrary SDK fields must not bypass the content privacy controls.
    const params: Data = {};
    for (const key of ["temperature", "maxTokens", "max_tokens", "p", "k", "seed", "frequencyPenalty", "presencePenalty"]) {
      if (typeof request[key] === "number") params[key] = request[key];
    }
    attrs[S.LLM_INVOCATION_PARAMETERS] = json(params);
    if (!hideInput(config)) {
      const messages = Array.isArray(request.messages) ? request.messages : [
        ...(request.preamble ? [{ role: "system", content: request.preamble }] : []),
        ...list(field(request, "chatHistory", "chat_history")).map((entry) => {
          const item = object(entry);
          return { role: item.role === "CHATBOT" ? "assistant" : String(item.role).toLowerCase(), content: item.message };
        }),
        { role: "user", content: request.message },
      ];
      attrs[S.LLM_INPUT_MESSAGES] = json(messages);
      attrs[S.INPUT_VALUE] = typeof request.message === "string" ? request.message : json(messages);
      attrs[S.INPUT_MIME_TYPE] = typeof request.message === "string" ? MimeType.TEXT : MimeType.JSON;
      if (request.tools) attrs[S.LLM_TOOLS] = json(request.tools);
    }
  }
  if (!hideInput(config)) attrs[S.RAW_INPUT] = json(withoutConnectorCredentials(request));
  return attrs;
}

/** Citation provenance is generated output, but source bodies echo inputs. Use an
 * allowlist so document/tool payloads and future source fields cannot leak.
 */
function withoutCitationInputs(value: unknown): unknown {
  if (!Array.isArray(value)) return undefined;
  return value.map(item => {
    const citation = object(item);
    const projected: Data = {};
    for (const key of ["start", "end", "text", "type"]) {
      if (typeof citation[key] === "number" || typeof citation[key] === "string") projected[key] = citation[key];
    }
    if (Array.isArray(citation.sources)) projected.sources = citation.sources.map(item => {
      const source = object(item);
      return {
        ...(typeof source.type === "string" ? { type: source.type } : {}),
        ...(typeof source.id === "string" ? { id: source.id } : {}),
      };
    });
    const documentIds = field(citation, "documentIds", "document_ids");
    if (Array.isArray(documentIds)) projected.documentIds = documentIds.filter(id => typeof id === "string");
    return projected;
  });
}

/** Keep generated fields only when inputs are hidden. Responses can echo full
 * chat history, embedding texts, reranked documents and citation sources.
 * Project rather than mutate the SDK response or filter history by role: even
 * earlier assistant messages are input to the current generation.
 */
function withoutInputEchoes(operation: Operation, response: Data): Data {
  if (operation === "embed") return { embeddings: response.embeddings };
  if (operation === "rerank") return {
    results: list(response.results).map((item) => {
      const result = object(item);
      return { index: result.index, relevanceScore: field(result, "relevanceScore", "relevance_score") };
    }),
  };
  const message = object(response.message);
  return {
    text: response.text,
    toolCalls: field(response, "toolCalls", "tool_calls"),
    citations: withoutCitationInputs(response.citations),
    message: {
      role: message.role,
      content: Array.isArray(message.content) ? message.content.map((part) => {
        const content = object(part);
        return { type: content.type, text: content.text, thinking: content.thinking };
      }) : message.content,
      toolCalls: field(message, "toolCalls", "tool_calls"),
      toolPlan: field(message, "toolPlan", "tool_plan"),
      citations: withoutCitationInputs(message.citations),
    },
  };
}

export function outputAttributes(operation: Operation, response: Data, config: TraceConfig): Attributes {
  const attrs: Attributes = usageAttributes(response);
  const id = response.id ?? field(response, "generationId", "generation_id");
  if (typeof id === "string") attrs[S.GEN_AI_RESPONSE_ID] = id;
  if (hideInput(config)) response = withoutInputEchoes(operation, response);
  if (operation === "embed") {
    // raw.output would otherwise bypass hideEmbeddingVectors.
    if (!hideOutput(config) && !config.hideEmbeddingVectors) {
      const vectors = Array.isArray(response.embeddings) ? response.embeddings : object(response.embeddings).float;
      list(vectors).forEach((vector, index) => {
        if (Array.isArray(vector) && vector.every((n) => typeof n === "number")) {
          attrs[`${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_VECTOR}`] = vector;
        }
      });
      attrs[S.RAW_OUTPUT] = json(response);
    }
  } else if (operation === "rerank") {
    const results = list(response.results);
    attrs["cohere.rerank.results_count"] = results.length;
    results.forEach((item, index) => {
      const result = object(item);
      const score = field(result, "relevanceScore", "relevance_score");
      if (typeof result.index === "number") attrs[`cohere.rerank.results.${index}.index`] = result.index;
      if (typeof score === "number") attrs[`cohere.rerank.results.${index}.relevance_score`] = score;
      const text = object(result.document).text;
      if (!hideOutput(config) && typeof text === "string") attrs[`cohere.rerank.results.${index}.document`] = text;
    });
    if (!hideOutput(config)) {
      attrs[S.RERANKER_OUTPUT_DOCUMENTS] = json(results);
      attrs[S.RAW_OUTPUT] = json(response);
    }
  } else if (!hideOutput(config)) {
    const message = object(response.message);
    const text = typeof response.text === "string" ? response.text : list(message.content).map((part) => object(part).text ?? "").join("");
    const calls = field(message, "toolCalls", "tool_calls") ?? field(response, "toolCalls", "tool_calls");
    const normalized: Data = { role: "assistant", content: message.content ?? text };
    const citations = message.citations ?? response.citations;
    if (Array.isArray(citations)) normalized.citations = citations;
    const toolPlan = field(message, "toolPlan", "tool_plan");
    if (typeof toolPlan === "string") normalized.tool_plan = toolPlan;
    if (calls) normalized.tool_calls = list(calls).map((item) => {
      const call = object(item);
      return call.function ? call : { function: { name: call.name, arguments: typeof call.parameters === "string" ? call.parameters : json(call.parameters) } };
    });
    attrs[S.OUTPUT_VALUE] = text;
    attrs[S.OUTPUT_MIME_TYPE] = MimeType.TEXT;
    attrs[S.LLM_OUTPUT_MESSAGES] = json([normalized]);
    attrs[S.RAW_OUTPUT] = json(response);
  }
  return attrs;
}

function usageAttributes(response: Data): Attributes {
  const usage = object(response.usage ?? response.meta);
  // Keep the legacy billed-token fallback only when actual usage is absent;
  // discounted billed units must never replace or be added to actual tokens.
  const billed = object(field(usage, "billedUnits", "billed_units"));
  const tokens = object(usage.tokens ?? billed);
  const input = field(tokens, "inputTokens", "input_tokens");
  const output = field(tokens, "outputTokens", "output_tokens");
  const attrs: Attributes = {};
  const numeric = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
  if (numeric(input)) attrs[S.LLM_TOKEN_COUNT_PROMPT] = input;
  if (numeric(output)) attrs[S.LLM_TOKEN_COUNT_COMPLETION] = output;
  if (numeric(input) || numeric(output)) attrs[S.LLM_TOKEN_COUNT_TOTAL] = (numeric(input) ? input : 0) + (numeric(output) ? output : 0);
  const cached = field(usage, "cachedTokens", "cached_tokens");
  if (numeric(cached)) attrs[S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ] = cached;
  for (const [camel, snake] of [
    ["inputTokens", "input_tokens"], ["outputTokens", "output_tokens"],
    ["searchUnits", "search_units"], ["classifications", "classifications"],
    ["images", "images"], ["imageTokens", "image_tokens"],
  ]) {
    const value = field(billed, camel, snake);
    if (numeric(value)) attrs[`cohere.usage.billed_units.${snake}`] = value;
  }
  return attrs;
}
