import type { Span } from "@opentelemetry/api";
import type { TraceConfig } from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";

type RecordValue = Record<string, unknown>;
export function record(value: unknown): RecordValue {
  return value !== null && typeof value === "object"
    ? (value as RecordValue)
    : {};
}
const list = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : value == null ? [] : [value];

/** Capture is deliberately allowlisted: never serialize SDK options, HTTP responses or binary. */
export class Capture {
  private remaining: number;
  private output: unknown[] = [];
  private firstContent = false;
  private truncated = false;
  private generation = false;
  private hops = 0;
  private capturedTools = false;
  private usageTotals = new Map<string, number>();
  private usageHops = new Map<string, number>();
  private started = performance.now();
  private allowInput: boolean;
  private allowOutput: boolean;
  constructor(
    private span: Span,
    config: TraceConfig,
    limit: number,
  ) {
    this.remaining = limit;
    // Suppress the whole content surface for text hiding, including tool arguments and schemas.
    this.allowInput = !(
      config.hideInputs ||
      config.hideInputMessages ||
      config.hideInputText
    );
    this.allowOutput = !(
      config.hideOutputs ||
      config.hideOutputMessages ||
      config.hideOutputText
    );
  }
  private bounded(value: unknown, size: number): unknown[] {
    const items = list(value);
    if (items.length > size) this.truncated = true;
    return items.slice(0, size);
  }
  private text(value: unknown): string | undefined {
    if (typeof value !== "string") return undefined;
    const result = value.slice(0, Math.max(0, Math.min(this.remaining, 4096)));
    if (result.length < value.length) this.truncated = true;
    if (this.remaining <= 0) return undefined;
    this.remaining -= result.length + 16;
    return result;
  }
  private json(value: unknown, depth = 0): unknown {
    if (typeof value === "function" || value === undefined) return undefined;
    if (this.remaining <= 0 || depth > 6) {
      this.truncated = true;
      return undefined;
    }
    this.remaining -= 16;
    if (value === null) return null;
    if (typeof value === "string") return this.text(value);
    if (typeof value === "boolean" || typeof value === "number") return value;
    if (Array.isArray(value))
      return this.bounded(value, 32).map((v) => this.json(v, depth + 1));
    const result: RecordValue = {};
    const keys = Object.keys(record(value));
    if (keys.length > 32) this.truncated = true;
    for (const key of keys.slice(0, 32)) {
      // Tool arguments and responses can carry credentials at any nesting level.
      if (
        /data|bytes|base64|headers|api.?key|secret|token|uri|url|authorization|password|passphrase|passwd|pwd|credential|private.?key|access.?key/i.test(
          key,
        )
      )
        continue;
      const boundedKey = this.text(key);
      if (boundedKey === undefined) break;
      result[boundedKey] = this.json(record(value)[key], depth + 1);
    }
    return result;
  }
  private parts(value: unknown): unknown[] {
    const result: unknown[] = [];
    for (const item of this.bounded(value, 64)) {
      if (this.remaining <= 0) break;
      const part = record(item);
      const text = this.text(typeof item === "string" ? item : part.text);
      if (text !== undefined) result.push({ type: "text", text });
      for (const key of ["functionCall", "functionResponse"] as const) {
        if (part[key])
          result.push({ type: key, ...record(this.json(part[key])) });
      }
      if (part.type === "function_call" || part.type === "function_result")
        result.push(this.json(part));
      // Only modality is retained for media, not raw bytes, signed URLs, or file URIs.
      for (const key of ["inlineData", "fileData", "image", "audio", "video"]) {
        if (part[key]) {
          result.push({ type: key });
          this.remaining -= 32;
        }
      }
    }
    return result;
  }
  input(params: RecordValue, operation: string): void {
    this.generation =
      operation === "generateContent" || operation === "generateContentStream";
    this.string(S.LLM_MODEL_NAME, params.model);
    if (operation === "embedContent") {
      this.string(S.EMBEDDING_MODEL_NAME, params.model);
      // Text batch semantics. Multimodal embedding models group parts differently;
      // omit a speculative input count for those shapes.
      if (typeof params.contents === "string")
        this.span.setAttribute("embedding.input_count", 1);
      else if (
        Array.isArray(params.contents) &&
        params.contents.every((v) => typeof v === "string") &&
        !String(params.model).includes("gemini-embedding-2")
      )
        this.span.setAttribute("embedding.input_count", params.contents.length);
    }
    this.string(
      "google.genai.previous_interaction_id",
      params.previous_interaction_id,
    );
    if (typeof params.background === "boolean")
      this.span.setAttribute("google.genai.background", params.background);
    const config = record(params.config ?? params.generation_config);
    const numeric: RecordValue = {};
    for (const key of [
      "temperature",
      "topP",
      "topK",
      "maxOutputTokens",
      "candidateCount",
      "seed",
      "top_p",
      "top_k",
      "max_output_tokens",
    ]) {
      if (typeof config[key] === "number") numeric[key] = config[key];
    }
    this.span.setAttribute(
      S.LLM_INVOCATION_PARAMETERS,
      JSON.stringify(numeric),
    );
    if (!this.allowInput) return;
    const messages: unknown[] = [];
    const system = params.system_instruction ?? config.systemInstruction;
    if (system)
      messages.push({
        role: "system",
        content: this.parts(record(system).parts ?? system),
      });
    for (const item of this.bounded(
      params.contents ?? params.input ?? params.prompt,
      64,
    )) {
      if (this.remaining <= 0) break;
      const content = record(item);
      messages.push({
        role: this.text(content.role) ?? "user",
        content: this.parts(content.parts ?? content.content ?? item),
      });
    }
    if (messages.length)
      this.span.setAttribute(S.LLM_INPUT_MESSAGES, JSON.stringify(messages));
    // Generation tools are observed only after the SDK resolves CallableTools.
    if (!this.generation) this.tools(config.tools ?? params.tools);
  }
  private tools(value: unknown): void {
    if (!this.allowInput || this.capturedTools || value == null) return;
    this.capturedTools = true;
    this.span.setAttribute(
      S.LLM_TOOLS,
      JSON.stringify(this.json(value)) ?? "[]",
    );
  }
  /** A hop gets its own cumulative usage snapshot. Sum independent hops, not SSE chunks. */
  generationHop(params: RecordValue): (value: unknown) => void {
    this.hops++;
    this.tools(record(params.config).tools);
    const previous = new Map<string, number>();
    return (value) => {
      for (const [key, count] of this.usage(
        record(record(value).usageMetadata),
      )) {
        const total =
          (this.usageTotals.get(key) ?? 0) + count - (previous.get(key) ?? 0);
        if (!previous.has(key))
          this.usageHops.set(key, (this.usageHops.get(key) ?? 0) + 1);
        previous.set(key, count);
        this.usageTotals.set(key, total);
        this.span.setAttribute(key, total);
      }
    };
  }
  string(key: string, value: unknown): void {
    if (typeof value === "string") {
      if (value.length > 256) this.truncated = true;
      this.span.setAttribute(key, value.slice(0, 256));
    }
  }
  private usage(usage: RecordValue): Map<string, number> {
    const result = new Map<string, number>();
    for (const [key, names] of [
      [
        S.LLM_TOKEN_COUNT_PROMPT,
        ["promptTokenCount", "total_input_tokens", "input_tokens"],
      ],
      [
        S.LLM_TOKEN_COUNT_COMPLETION,
        ["candidatesTokenCount", "total_output_tokens", "output_tokens"],
      ],
      [S.LLM_TOKEN_COUNT_TOTAL, ["totalTokenCount", "total_tokens"]],
      [
        S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
        ["cachedContentTokenCount", "total_cached_tokens"],
      ],
      [
        S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
        [
          "thoughtsTokenCount",
          "total_thought_tokens",
          "total_reasoning_tokens",
        ],
      ],
    ] as const) {
      const count = names
        .map((name) => usage[name])
        .find((v) => typeof v === "number" && Number.isFinite(v));
      if (typeof count === "number") result.set(key, count);
    }
    return result;
  }
  response(value: unknown): void {
    const response = record(value);
    const interaction = record(response.interaction);
    this.string(
      S.GEN_AI_RESPONSE_ID,
      response.responseId ?? interaction.id ?? response.id,
    );
    this.string(
      S.GEN_AI_RESPONSE_MODEL,
      response.modelVersion ?? interaction.model ?? response.model,
    );
    this.string("google.genai.status", interaction.status ?? response.status);
    this.string("google.genai.operation_id", response.name);
    if (typeof response.done === "boolean")
      this.span.setAttribute("google.genai.operation_done", response.done);
    if (Array.isArray(response.embeddings)) {
      this.span.setAttribute("embedding.count", response.embeddings.length);
      const values = record(response.embeddings[0]).values;
      if (Array.isArray(values))
        this.span.setAttribute("embedding.dimensions", values.length);
    }
    if (!this.generation) {
      const usage = record(
        response.usageMetadata ??
          interaction.usage ??
          response.usage ??
          record(response.metadata).total_usage,
      );
      for (const [key, count] of this.usage(usage))
        this.span.setAttribute(key, count);
    }
    const candidates = list(response.candidates);
    const reasons = candidates
      .map((c) => record(c).finishReason)
      .filter((x): x is string => typeof x === "string");
    if (reasons.length > 16 || reasons.some((reason) => reason.length > 128))
      this.truncated = true;
    if (reasons.length)
      this.span.setAttribute(
        S.GEN_AI_RESPONSE_FINISH_REASONS,
        reasons.slice(0, 16).map((reason) => reason.slice(0, 128)),
      );
    if (
      !this.firstContent &&
      (candidates
        .slice(0, 16)
        .some(
          (candidate) =>
            list(record(record(candidate).content).parts).length > 0,
        ) ||
        response.event_type === "content.delta" ||
        response.event_type === "step.delta" ||
        list(
          response.steps ??
            interaction.steps ??
            response.outputs ??
            interaction.outputs,
        ).length > 0)
    ) {
      this.firstContent = true;
      this.span.setAttribute(
        "gen_ai.response.time_to_first_token",
        (performance.now() - this.started) / 1000,
      );
    }
    if (!this.allowOutput || this.remaining <= 0) return;
    for (const candidate of this.bounded(candidates, 16)) {
      const content = record(record(candidate).content);
      this.append({
        role: this.text(content.role) ?? "model",
        content: this.parts(content.parts),
      });
    }
    // Released Interactions use model_output steps; older releases use outputs.
    const steps = response.steps ?? interaction.steps;
    if (steps !== undefined) {
      this.output = [];
      for (const step of this.bounded(steps, 64)) {
        if (record(step).type === "model_output")
          this.append({
            role: "model",
            content: this.parts(record(step).content),
          });
      }
    } else if (response.outputs ?? interaction.outputs) {
      this.output = [];
      this.append({
        role: "model",
        content: this.parts(response.outputs ?? interaction.outputs),
      });
    } else if (
      response.event_type === "content.delta" ||
      response.event_type === "step.delta"
    ) {
      this.append({ role: "model", content: this.parts(response.delta) });
    } else if (
      response.event_type === "step.start" &&
      record(response.step).type === "model_output"
    ) {
      this.append({
        role: "model",
        content: this.parts(record(response.step).content),
      });
    }
  }

  private append(message: unknown): void {
    if (!list(record(message).content).length) return;
    this.output.push(message);
  }
  finish(): void {
    if (this.output.length)
      this.span.setAttribute(
        S.LLM_OUTPUT_MESSAGES,
        JSON.stringify(this.output),
      );
    if (this.truncated || this.remaining <= 0)
      this.span.setAttribute("google.genai.capture_truncated", true);
    if (this.generation && !this.hops)
      this.span.setAttribute("google.genai.usage_capture", "unavailable");
    else if (
      this.generation &&
      (!this.usageHops.size ||
        [...this.usageHops.values()].some((count) => count < this.hops))
    )
      this.span.setAttribute("google.genai.usage_capture", "partial");
  }
}
