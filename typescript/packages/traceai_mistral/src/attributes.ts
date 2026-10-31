import { Attributes } from "@opentelemetry/api";
import { safelyJSONStringify, TraceConfig } from "@traceai/fi-core";
import { SemanticConventions as S, FISpanKind, MimeType, LLMProvider } from "@traceai/fi-semantic-conventions";

type Data = Record<string, unknown>;
export function record(value: unknown): Data {
  return value !== null && (typeof value === "object" || typeof value === "function") ? value as Data : {};
}
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function json(value: unknown): string { return safelyJSONStringify(value) ?? ""; }
function text(value: unknown): string | undefined { return typeof value === "string" ? value : undefined; }
function number(value: unknown): number | undefined { return typeof value === "number" ? value : undefined; }

export function inputAttributes(value: unknown, operation: string, config: TraceConfig): Attributes {
  const body = record(value);
  const attrs: Attributes = {
    [S.FI_SPAN_KIND]: operation === "embeddings" ? FISpanKind.EMBEDDING : FISpanKind.LLM,
    [S.LLM_PROVIDER]: LLMProvider.MISTRALAI,
    [S.GEN_AI_OPERATION_NAME]: operation === "conversation" ? "chat" : operation,
    [operation === "embeddings" ? S.EMBEDDING_MODEL_NAME : S.LLM_MODEL_NAME]: text(body.model),
  };
  // Never duplicate hidden messages, tool arguments or images into unmasked blobs.
  if (!config.hideInputs && !config.hideInputMessages && !config.hideInputText && !config.hideInputImages) {
    attrs[S.INPUT_VALUE] = json(value);
    attrs[S.INPUT_MIME_TYPE] = MimeType.JSON;
    if (body.tools) attrs[S.LLM_TOOLS] = json(body.tools);
  }
  const messages = body.messages ?? body.inputs ?? record(body.conversationAppendRequest).inputs;
  if (operation !== "embeddings" && messages !== undefined) {
    attrs[S.LLM_INPUT_MESSAGES] = json(Array.isArray(messages) ? messages.map(normalizeMessage) : [{ role: "user", content: messages }]);
  }
  const parameters: Data = {};
  for (const key of ["temperature", "topP", "top_p", "maxTokens", "max_tokens", "randomSeed", "random_seed", "safePrompt", "safe_prompt"]) {
    if (["number", "boolean"].includes(typeof body[key])) parameters[key] = body[key];
  }
  attrs[S.LLM_INVOCATION_PARAMETERS] = json(parameters);
  if (operation === "embeddings" && !config.hideInputs && !config.hideInputText) {
    const input = body.inputs ?? body.input;
    (typeof input === "string" ? [input] : array(input)).forEach((item, index) => {
      if (typeof item === "string") attrs[`${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_TEXT}`] = item;
    });
  }
  return attrs;
}

function normalizeMessage(value: unknown): Data {
  const message = record(value);
  if (message.type === "function.call") {
    return { role: "assistant", tool_calls: [{ id: message.toolCallId, type: "function", function: { name: message.name, arguments: message.arguments } }] };
  }
  return {
    ...message,
    ...(message.toolCalls ? { tool_calls: message.toolCalls } : {}),
    ...(message.toolCallId ? { tool_call_id: message.toolCallId } : {}),
  };
}

function metadata(value: Data): Attributes {
  const usage = record(value.usage);
  return {
    [S.LLM_MODEL_NAME]: text(value.model),
    [S.GEN_AI_RESPONSE_MODEL]: text(value.model),
    [S.GEN_AI_RESPONSE_ID]: text(value.conversationId ?? value.id),
    [S.LLM_TOKEN_COUNT_PROMPT]: number(usage.promptTokens ?? usage.prompt_tokens),
    [S.LLM_TOKEN_COUNT_COMPLETION]: number(usage.completionTokens ?? usage.completion_tokens),
    [S.LLM_TOKEN_COUNT_TOTAL]: number(usage.totalTokens ?? usage.total_tokens),
  };
}

export function outputAttributes(value: unknown, operation: string, config: TraceConfig): Attributes {
  const response = record(value);
  const attrs = metadata(response);
  if (!config.hideOutputs && !config.hideOutputMessages && !config.hideOutputText && !(operation === "embeddings" && config.hideEmbeddingVectors)) {
    attrs[S.OUTPUT_VALUE] = json(value);
    attrs[S.OUTPUT_MIME_TYPE] = MimeType.JSON;
  }
  const choices = array(response.choices).map(record);
  if (choices.length) {
    attrs[S.LLM_OUTPUT_MESSAGES] = json(choices.map(c => normalizeMessage(c.message)));
    attrs[S.GEN_AI_RESPONSE_FINISH_REASONS] = json(choices.map(c => c.finishReason ?? c.finish_reason).filter(x => x != null));
  } else if (response.outputs) {
    attrs[S.LLM_OUTPUT_MESSAGES] = json(array(response.outputs).map(normalizeMessage));
  }
  if (operation === "embeddings" && !config.hideOutputs && !config.hideEmbeddingVectors) {
    array(response.data).forEach((item, index) => {
      const vector = record(item).embedding;
      if (Array.isArray(vector) && vector.every(v => typeof v === "number")) {
        attrs[`${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_VECTOR}`] = vector;
      }
    });
  }
  return attrs;
}

/** Bound retained output independently of stream duration. Usage is always read. */
export class StreamOutput {
  private remaining = 64 * 1024;
  private readonly events: string[] = [];
  private readonly choices = new Map<number, Data>();
  private readonly info: Data = {};
  private truncated = false;
  public error?: Error;
  constructor(private readonly config: TraceConfig) {}

  add(event: unknown): void {
    const data = record(record(event).data ?? event);
    for (const key of ["model", "id", "usage", "conversationId"]) {
      if (data[key] != null) this.info[key] = data[key];
    }
    if (data.type === "conversation.response.error") {
      this.error = new Error(text(data.message) ?? "Mistral conversation failed");
      this.error.name = "MistralConversationError";
      return; // Error text belongs only on the privacy-aware exception/status path.
    }
    if (this.config.hideOutputs || this.config.hideOutputMessages || this.config.hideOutputText) return;
    const serialized = json(event) || "null";
    // Budget both raw events and their assembled representation.
    if (serialized.length * 2 > this.remaining) { this.truncated = true; return; }
    this.remaining -= serialized.length * 2;
    this.events.push(serialized);
    let deltas = array(data.choices);
    if (data.type === "message.output.delta") {
      deltas = [{ index: data.outputIndex, delta: { role: "assistant", content: data.content } }];
    } else if (data.type === "function.call.delta") {
      deltas = [{ index: data.outputIndex, delta: { toolCalls: [{ index: 0, id: data.toolCallId, type: "function", function: { name: data.name, arguments: data.arguments } }] } }];
    }
    for (const item of deltas) {
      const choice = record(item);
      const index = number(choice.index) ?? 0;
      const previous = this.choices.get(index) ?? { index, message: { role: "assistant", content: "", tool_calls: [] } };
      const message = record(previous.message);
      const delta = record(choice.delta);
      if (typeof delta.role === "string") message.role = delta.role;
      if (typeof delta.content === "string") message.content = String(message.content) + delta.content;
      for (const entry of array(delta.toolCalls ?? delta.tool_calls)) {
        const tool = record(entry);
        const tools = array(message.tool_calls) as Data[];
        const toolIndex = number(tool.index) ?? 0;
        // Avoid sparse arrays from arbitrary provider indices.
        let accumulated = tools.find(t => t.index === toolIndex);
        if (!accumulated) { accumulated = { index: toolIndex, function: { name: "", arguments: "" } }; tools.push(accumulated); }
        if (tool.id) accumulated.id = tool.id;
        if (tool.type) accumulated.type = tool.type;
        const fn = record(accumulated.function);
        for (const key of ["name", "arguments"]) {
          const fragment = record(tool.function)[key];
          if (typeof fragment === "string") {
            fn[key] = data.type === "function.call.delta" && key === "name" ? fragment : String(fn[key]) + fragment;
          }
        }
      }
      previous.finishReason = choice.finishReason ?? choice.finish_reason ?? previous.finishReason;
      this.choices.set(index, previous);
    }
  }

  attributes(): Attributes {
    const attrs = outputAttributes({ ...this.info, choices: [...this.choices.values()] }, "chat", this.config);
    if (!this.config.hideOutputs && !this.config.hideOutputMessages && !this.config.hideOutputText) {
      attrs[S.OUTPUT_VALUE] = `[${this.events.join(",")}]`;
    }
    if (this.truncated) attrs["mistral.output.truncated"] = true;
    return attrs;
  }
}
