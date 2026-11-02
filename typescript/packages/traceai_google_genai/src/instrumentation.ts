import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  diag,
  context,
  trace,
  SpanKind,
  Attributes,
  SpanStatusCode,
  Span,
  TracerProvider,
} from "@opentelemetry/api";
import { VERSION } from "./version";
import {
  SemanticConventions as S,
  FISpanKind,
  MimeType,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  safelyJSONStringify,
  generateTraceConfig,
  TraceConfig,
  TraceConfigOptions,
  REDACTED_VALUE,
} from "@traceai/fi-core";
import {
  requestCredentials,
  RequestCredentials,
  telemetryError,
} from "./telemetryError";

const MODULE_NAME = "@google/generative-ai";
const METHODS = [
  "generateContent",
  "embedContent",
  "batchEmbedContents",
] as const;
type Operation = (typeof METHODS)[number];
type ModelPrototype = Record<string, unknown>;
type SDKModule = Record<string, unknown>;

// Ownership is per prototype, not per process or export namespace. ESM and CJS
// namespaces can expose the same prototype, while nested SDKs expose different ones.
const owners = new WeakMap<ModelPrototype, GoogleGenAIInstrumentation>();
let patchedPrototypeCount = 0;
export function isPatched() {
  return patchedPrototypeCount > 0;
}

interface Part {
  text?: string;
  inlineData?: { mimeType: string; data: string };
  functionCall?: { name: string; args: Record<string, unknown> };
  functionResponse?: { name: string; response: Record<string, unknown> };
}
interface Content {
  role?: string;
  parts: Part[];
}
interface GenerateContentRequest {
  contents: Content[];
  generationConfig?: Record<string, unknown>;
  safetySettings?: unknown[];
  tools?: Array<{ functionDeclarations?: unknown[] }>;
  toolConfig?: unknown;
  systemInstruction?: string | Part | Content | null;
  cachedContent?: string;
}
interface ModelDefaults {
  apiKey?: string;
  _requestOptions?: { customHeaders?: unknown };
  model?: string;
  generationConfig?: GenerateContentRequest["generationConfig"];
  safetySettings?: GenerateContentRequest["safetySettings"];
  tools?: GenerateContentRequest["tools"];
  toolConfig?: unknown;
  systemInstruction?: GenerateContentRequest["systemInstruction"];
  cachedContent?: { name: string };
}
interface GenerateContentResponse {
  response: {
    text?: () => string;
    modelVersion?: string;
    candidates?: Array<{ content?: Content; finishReason?: string }>;
    usageMetadata?: {
      promptTokenCount?: number;
      candidatesTokenCount?: number;
      totalTokenCount?: number;
    };
  };
}
interface EmbedContentRequest {
  content: Content;
  taskType?: string;
  title?: string;
}
interface BatchEmbedContentsRequest {
  requests: EmbedContentRequest[];
}
interface EmbedContentResponse {
  embedding?: { values: number[] };
}
interface BatchEmbedContentsResponse {
  embeddings?: Array<{ values: number[] }>;
}

/** Legacy @google/generative-ai non-streaming compatibility instrumentation. */
export class GoogleGenAIInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private privacy!: TraceConfig;
  // Base construction may call enable/patch before subclass field initialization.
  private declare patchedPrototypes?: Set<ModelPrototype>;
  private declare manualModules?: Set<SDKModule>;
  public readonly instrumentationName = "@traceai/google-genai";

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/google-genai", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
    this.refreshTracer();
  }

  private refreshTracer() {
    this.privacy = generateTraceConfig(this._traceConfig);
    // Published fi-core 1.1.0 stops at the first matching key-based rule,
    // bypassing its PII fallback (notably for image hiding). Compose a separate
    // PII-only pass after content masking using the public, maintained tracer.
    // Both wrappers delegate to ONE underlying span; no extra span is created.
    const tracer = this.privacy.piiRedaction
      ? new FITracer({
          tracer: this.tracer,
          traceConfig: {
            hideInputs: false,
            hideOutputs: false,
            hideInputMessages: false,
            hideOutputMessages: false,
            hideInputImages: false,
            hideInputText: false,
            hideOutputText: false,
            hideEmbeddingVectors: false,
            hideErrorDetails: false,
            piiRedaction: true,
          },
        })
      : this.tracer;
    this.fiTracer = new FITracer({
      tracer,
      traceConfig: { ...this.privacy, piiRedaction: false },
    });
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  public override enable(): void {
    super.enable();
    this.refreshTracer();
    for (const module of this.manualModules ?? []) this.patch(module);
  }

  public override disable(): void {
    super.disable();
    for (const prototype of this.patchedPrototypes ?? [])
      this.unpatchPrototype(prototype);
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
  }

  /** Also works when the SDK was imported before OpenTelemetry was registered. */
  manuallyInstrument(module: SDKModule) {
    (this.manualModules ??= new Set()).add(module);
    this.patch(module);
  }

  private patch(module: SDKModule) {
    const prototype = (
      module?.GenerativeModel as { prototype?: ModelPrototype }
    )?.prototype;
    if (
      !prototype ||
      !METHODS.some((method) => typeof prototype[method] === "function")
    ) {
      diag.warn(
        "@traceai/google-genai only instruments the legacy @google/generative-ai SDK; " +
          "the supplied module has no supported GenerativeModel methods. " +
          "The modern @google/genai SDK is not supported by this adapter.",
      );
      return module;
    }
    if (owners.has(prototype)) return module;
    this.refreshTracer();
    for (const method of METHODS) {
      if (typeof prototype[method] === "function") {
        this._wrap(prototype, method, this.wrapMethod(method));
      }
    }
    owners.set(prototype, this);
    (this.patchedPrototypes ??= new Set()).add(prototype);
    patchedPrototypeCount++;
    return module;
  }

  private unpatch(module: SDKModule) {
    const prototype = (
      module?.GenerativeModel as { prototype?: ModelPrototype }
    )?.prototype;
    if (prototype) this.unpatchPrototype(prototype);
  }

  private unpatchPrototype(prototype: ModelPrototype) {
    if (owners.get(prototype) !== this) return;
    for (const method of METHODS) {
      if (typeof prototype[method] === "function")
        this._unwrap(prototype, method);
    }
    owners.delete(prototype);
    this.patchedPrototypes?.delete(prototype);
    patchedPrototypeCount--;
  }

  private wrapMethod(method: Operation) {
    const instrumentation = this;
    // The wrapper forwards every argument unchanged, including requestOptions.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any =>
      function (this: ModelDefaults, ...args: unknown[]) {
        if (isTracingSuppressed(context.active()))
          return original.apply(this, args);
        const privacy = instrumentation.privacy;
        let span: Span;
        let credentials: RequestCredentials;
        try {
          // Snapshot effective API/header credentials before asynchronous SDK
          // validation or application mutation. Never attach or log these values.
          credentials = requestCredentials(this, args[1]);
          span = instrumentation.fiTracer.startSpan(`Google GenAI ${method}`, {
            kind: SpanKind.INTERNAL,
            attributes: {
              [S.FI_SPAN_KIND]:
                method === "generateContent"
                  ? FISpanKind.LLM
                  : FISpanKind.EMBEDDING,
              [method === "generateContent"
                ? S.LLM_MODEL_NAME
                : S.EMBEDDING_MODEL_NAME]: this.model || "unknown",
              [S.LLM_PROVIDER]: LLMProvider.GOOGLE_GENERATIVE_AI,
              [S.GEN_AI_OPERATION_NAME]:
                method === "generateContent" ? "chat" : "embeddings",
              ...safeAttributes(() =>
                inputAttributes(method, args[0], this, privacy),
              ),
            },
          });
        } catch {
          // A telemetry failure must never prevent the SDK from being called.
          return original.apply(this, args);
        }
        const execContext = trace.setSpan(context.active(), span);
        let result;
        try {
          result = context.with(execContext, () => original.apply(this, args));
        } catch (error) {
          finishError(span, error, credentials);
          throw error;
        }
        const promise = Promise.resolve(result).then(
          (value) => {
            try {
              span.setAttributes(
                safeAttributes(() => outputAttributes(method, value, privacy)),
              );
              span.setStatus({ code: SpanStatusCode.OK });
            } catch {
              diag.debug("Google GenAI response telemetry unavailable");
            } finally {
              endSpan(span);
            }
            return value;
          },
          (error) => {
            finishError(span, error, credentials);
            throw error;
          },
        );
        return context.bind(execContext, promise);
      };
  }
}

function safeAttributes(extract: () => Attributes): Attributes {
  try {
    return extract();
  } catch {
    // Do not log payloads or extraction errors, which can contain hidden content.
    diag.debug("Google GenAI telemetry extraction unavailable");
    return {};
  }
}
function endSpan(span: Span) {
  try {
    span.end();
  } catch {
    /* exporter failures cannot change SDK results */
  }
}
function finishError(
  span: Span,
  error: unknown,
  credentials: RequestCredentials,
) {
  try {
    const sanitized = telemetryError(error, credentials);
    span.recordException(sanitized);
    span.setStatus({
      code: SpanStatusCode.ERROR,
      message: sanitized.message,
    });
  } catch {
    diag.debug("Google GenAI error telemetry unavailable");
  } finally {
    endSpan(span);
  }
}

/** Normalize only the telemetry view; never modify SDK arguments or model defaults. */
function contentFromInput(input: string | Array<string | Part>): Content {
  const parts = (typeof input === "string" ? [input] : input).map((part) =>
    typeof part === "string" ? { text: part } : part,
  );
  return {
    role:
      parts.length && parts.every((part) => part.functionResponse)
        ? "function"
        : "user",
    parts,
  };
}
function systemContent(
  input: GenerateContentRequest["systemInstruction"],
): Content | undefined {
  if (input == null) return undefined;
  if (typeof input === "string")
    return { role: "system", parts: [{ text: input }] };
  if ("parts" in input)
    return { role: input.role || "system", parts: input.parts };
  return { role: "system", parts: [input] };
}
function effectiveRequest(
  input: unknown,
  model: ModelDefaults,
): GenerateContentRequest {
  const request =
    typeof input === "string" || Array.isArray(input)
      ? { contents: [contentFromInput(input)] }
      : (input as GenerateContentRequest);
  // The SDK replaces whole per-call fields, rather than deeply merging config.
  return {
    generationConfig: model.generationConfig,
    safetySettings: model.safetySettings,
    tools: model.tools,
    toolConfig: model.toolConfig,
    systemInstruction: model.systemInstruction,
    cachedContent: model.cachedContent?.name,
    ...request,
  };
}
function embeddingRequest(input: unknown): EmbedContentRequest {
  return typeof input === "string" || Array.isArray(input)
    ? { content: contentFromInput(input) }
    : (input as EmbedContentRequest);
}
function textOf(content?: Content): string {
  return content?.parts?.map((part) => part.text || "").join("") || "";
}

// Serialized copies are opaque to the shared fine-grained masking rules. Omit
// them whenever they could contain hidden content; retain safe metadata and let
// FITracer apply its canonical message, error and PII masking rules as well.
function hideInputCopies(config: TraceConfig): boolean {
  return (
    config.hideInputs ||
    config.hideInputMessages ||
    config.hideInputText ||
    config.hideInputImages
  );
}
function hideOutputCopies(config: TraceConfig): boolean {
  return (
    config.hideOutputs || config.hideOutputMessages || config.hideOutputText
  );
}
function inputAttributes(
  method: Operation,
  input: unknown,
  model: ModelDefaults,
  config: TraceConfig,
): Attributes {
  if (method === "generateContent") {
    const request = effectiveRequest(input, model);
    const messages: Array<Record<string, unknown>> = [];
    const system = systemContent(request.systemInstruction);
    if (system) messages.push({ role: "system", content: textOf(system) });
    for (const content of request.contents ?? []) {
      const text = textOf(content);
      messages.push({
        role: content.role || "user",
        ...(text ? { content: text } : {}),
      });
    }
    // Published fi-core 1.1.0 applies only its first matching masking rule.
    // Redact text here so hideInputImages cannot short-circuit hideInputText.
    if (config.hideInputText) {
      for (const message of messages) {
        if (message.content !== undefined) message.content = REDACTED_VALUE;
      }
    }
    const functions = request.tools?.flatMap(
      (tool) => tool.functionDeclarations || [],
    );
    return {
      [S.LLM_INPUT_MESSAGES]: json(messages),
      ...(request.generationConfig
        ? { [S.LLM_INVOCATION_PARAMETERS]: json(request.generationConfig) }
        : {}),
      ...(functions?.length ? { [S.LLM_TOOLS]: json(functions) } : {}),
      ...(!hideInputCopies(config)
        ? {
            [S.INPUT_VALUE]: json(request),
            [S.INPUT_MIME_TYPE]: MimeType.JSON,
            [S.RAW_INPUT]: json(request),
          }
        : {}),
    };
  }
  const request =
    method === "embedContent"
      ? embeddingRequest(input)
      : (input as BatchEmbedContentsRequest);
  const requests = "requests" in request ? request.requests : [request];
  const attributes: Attributes = {};
  if (
    !config.hideInputs &&
    !config.hideInputText &&
    !config.hideInputMessages
  ) {
    requests.forEach((entry, index) => {
      attributes[`${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_TEXT}`] =
        textOf(entry.content);
    });
  }
  if (!hideInputCopies(config)) {
    attributes[S.INPUT_VALUE] =
      method === "embedContent" ? textOf(requests[0].content) : json(request);
    attributes[S.INPUT_MIME_TYPE] =
      method === "embedContent" ? MimeType.TEXT : MimeType.JSON;
    attributes[S.RAW_INPUT] = json(request);
  }
  return attributes;
}

function outputAttributes(
  method: Operation,
  value: unknown,
  config: TraceConfig,
): Attributes {
  const attributes: Attributes = {};
  if (
    !hideOutputCopies(config) &&
    (method === "generateContent" || !config.hideEmbeddingVectors)
  ) {
    attributes[S.RAW_OUTPUT] = json(value);
  }
  if (method !== "generateContent") {
    const embeddings =
      method === "embedContent"
        ? [(value as EmbedContentResponse).embedding]
        : (value as BatchEmbedContentsResponse).embeddings;
    if (!config.hideOutputs && !config.hideEmbeddingVectors) {
      embeddings?.forEach((embedding, index) => {
        attributes[`${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_VECTOR}`] =
          embedding?.values;
      });
    }
    return attributes;
  }
  const response = (value as GenerateContentResponse).response;
  // Real SDK text() throws for safety-blocked responses. The successful SDK
  // response (including promptFeedback) still belongs to the application.
  if (!hideOutputCopies(config)) {
    try {
      const text = response.text?.();
      if (text !== undefined) {
        attributes[S.OUTPUT_VALUE] = text;
        attributes[S.OUTPUT_MIME_TYPE] = MimeType.TEXT;
      }
    } catch {
      /* response is inspectable even when text is unavailable */
    }
  }
  if (response.modelVersion)
    attributes[S.GEN_AI_RESPONSE_MODEL] = response.modelVersion;
  const candidate = response.candidates?.[0];
  if (candidate?.content) {
    const text = textOf(candidate.content);
    const toolCalls = config.hideOutputText
      ? []
      : candidate.content.parts
          ?.filter((part) => part.functionCall)
          .map((part) => ({
            function: {
              name: part.functionCall!.name,
              arguments: json(part.functionCall!.args),
            },
          }));
    attributes[S.LLM_OUTPUT_MESSAGES] = json([
      {
        role: candidate.content.role || "model",
        ...(text ? { content: text } : {}),
        ...(toolCalls?.length ? { tool_calls: toolCalls } : {}),
      },
    ]);
  }
  if (response.candidates) {
    attributes[S.GEN_AI_RESPONSE_FINISH_REASONS] = json(
      response.candidates.map((c) => c.finishReason).filter(Boolean),
    );
  }
  const usage = response.usageMetadata;
  if (usage) {
    attributes[S.LLM_TOKEN_COUNT_PROMPT] = usage.promptTokenCount;
    attributes[S.LLM_TOKEN_COUNT_COMPLETION] = usage.candidatesTokenCount;
    attributes[S.LLM_TOKEN_COUNT_TOTAL] = usage.totalTokenCount;
  }
  return attributes;
}

function json(value: unknown): string | undefined {
  return safelyJSONStringify(value) ?? undefined;
}
