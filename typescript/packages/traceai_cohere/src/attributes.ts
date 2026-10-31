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
  if (!hideInput(config)) attrs[S.RAW_INPUT] = json(request);
  return attrs;
}

export function outputAttributes(operation: Operation, response: Data, config: TraceConfig): Attributes {
  const attrs: Attributes = usageAttributes(response);
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
    if (calls) normalized.tool_calls = list(calls).map((item) => {
      const call = object(item);
      return call.function ? call : { function: { name: call.name, arguments: json(call.parameters) } };
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
  // Actual tokens, when available, are not the same as discounted billed units.
  const tokens = object(usage.tokens ?? field(usage, "billedUnits", "billed_units"));
  const input = field(tokens, "inputTokens", "input_tokens");
  const output = field(tokens, "outputTokens", "output_tokens");
  const attrs: Attributes = {};
  if (typeof input === "number") attrs[S.LLM_TOKEN_COUNT_PROMPT] = input;
  if (typeof output === "number") attrs[S.LLM_TOKEN_COUNT_COMPLETION] = output;
  if (typeof input === "number" || typeof output === "number") attrs[S.LLM_TOKEN_COUNT_TOTAL] = (typeof input === "number" ? input : 0) + (typeof output === "number" ? output : 0);
  return attrs;
}
