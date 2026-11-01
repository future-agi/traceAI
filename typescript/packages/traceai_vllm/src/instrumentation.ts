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
  TracerProvider,
} from "@opentelemetry/api";
import { VERSION } from "./version";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  safelyJSONStringify,
  TraceConfigOptions,
  generateTraceConfig,
} from "@traceai/fi-core";

import {
  observeResponse,
  observeStream,
  spanEnder,
  StreamChunk,
} from "./response";

type Create = (
  this: unknown,
  body: ChatCompletionRequest | CompletionRequest,
  options?: Record<string, unknown>,
) => Promise<unknown>;
interface ResourcePrototype {
  create: Create & { __wrapped?: boolean };
}
interface OpenAIModule {
  OpenAI?: OpenAIModule;
  default?: OpenAIModule;
  Chat?: { Completions?: { prototype: ResourcePrototype } };
  Completions?: { prototype: ResourcePrototype };
}

/**
 * Flag to check if the vLLM module has been patched
 */
let patchedTargets = 0;

/**
 * Function to check if instrumentation is enabled / disabled
 */
export function isPatched() {
  return patchedTargets > 0;
}

// Type definitions for OpenAI-compatible API
interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

interface ToolCall {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string;
  };
}

interface Tool {
  type: "function";
  function: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
}

interface ChatCompletionRequest {
  model: string;
  messages: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stop?: string | string[];
  stream?: boolean;
  tools?: Tool[];
  tool_choice?: string | { type: string; function: { name: string } };
  [key: string]: unknown;
}

interface ChatCompletionChoice {
  index: number;
  message: ChatMessage;
  finish_reason: string;
  delta?: {
    content?: string;
    role?: string;
    tool_calls?: ToolCall[];
  };
}

interface ChatCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: ChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface CompletionRequest {
  model: string;
  prompt: string | string[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stop?: string | string[];
  stream?: boolean;
  [key: string]: unknown;
}

interface CompletionChoice {
  text: string;
  index: number;
  finish_reason: string;
}

interface Completion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: CompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface StreamResponse<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
}

/**
 * Configuration for vLLM instrumentation
 */
export interface VLLMInstrumentationConfig {
  instrumentationConfig?: InstrumentationConfig;
  traceConfig?: TraceConfigOptions;
  /** Base URL pattern to identify vLLM requests (optional) */
  baseUrlPattern?: string | RegExp;
}

/**
 * @param config The config for the instrumentation
 */
export class VLLMInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _baseUrlPattern?: string | RegExp;
  private readonly modules = new Set<OpenAIModule>();
  private readonly patches = new Map<ResourcePrototype, Create>();
  private hideInputPayload = false;
  private hideOutputPayload = false;

  constructor(config: VLLMInstrumentationConfig = {}) {
    // InstrumentationBase can call enable() before subclass fields/configuration exist.
    super("@traceai/vllm", VERSION, {
      ...config.instrumentationConfig,
      enabled: false,
    });
    this._traceConfig = config.traceConfig;
    this._baseUrlPattern = config.baseUrlPattern;
    this.setConfig(config.instrumentationConfig ?? {});
    this.refreshTracer();
    if (config.instrumentationConfig?.enabled !== false) this.enable();
  }

  private refreshTracer() {
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
    const privacy = generateTraceConfig(this._traceConfig);
    // The raw body, invocation extras and tool definitions duplicate message content.
    // Fine-grained core masking cannot safely redact arbitrary nested payloads, so
    // omit content-bearing aliases whenever any corresponding privacy flag is set.
    this.hideInputPayload =
      privacy.hideInputs ||
      privacy.hideInputMessages ||
      privacy.hideInputText ||
      privacy.hideInputImages;
    this.hideOutputPayload =
      privacy.hideOutputs || privacy.hideOutputMessages || privacy.hideOutputText;
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  public override enable(): void {
    this.refreshTracer();
    super.enable();
    for (const module of this.modules) this.patch(module);
  }

  public override disable(): void {
    super.disable();
    for (const target of [...this.patches.keys()]) this.restore(target);
  }

  public readonly instrumentationName = "@traceai/vllm";

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "openai",
      [">=4.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
  }

  /** Manually instrument a loaded CJS export or an immutable ESM namespace. */
  manuallyInstrument(module: Record<string, unknown>) {
    this.modules.add(module as OpenAIModule);
    if (this.isEnabled()) this.patch(module as OpenAIModule);
  }

  private isVLLMRequest(resource: unknown): boolean {
    if (!this._baseUrlPattern) return true;
    const value = resource as
      | {
          _client?: { baseURL?: string };
          client?: { baseURL?: string };
          baseURL?: string;
        }
      | undefined;
    const baseURL =
      value?._client?.baseURL ?? value?.client?.baseURL ?? value?.baseURL;
    if (typeof baseURL !== "string") return false;
    if (typeof this._baseUrlPattern === "string")
      return baseURL.includes(this._baseUrlPattern);
    // RegExp g/y flags must not make identical calls alternate between traced/untraced.
    return new RegExp(
      this._baseUrlPattern.source,
      this._baseUrlPattern.flags,
    ).test(baseURL);
  }

  private targets(module: OpenAIModule) {
    const sdk = module.OpenAI ?? module.default ?? module;
    return [sdk.Chat?.Completions?.prototype, sdk.Completions?.prototype];
  }

  private patch(module: OpenAIModule) {
    this.refreshTracer();
    let found = false;
    this.targets(module).forEach((target, index) => {
      if (!target || typeof target.create !== "function") return;
      found = true;
      if (this.patches.has(target)) return;
      // Do not replace another owner (including @traceai/openai) or stack duplicate spans.
      if (target.create.__wrapped) {
        diag.warn(
          "@traceai/vllm: resource already instrumented; use only one OpenAI client instrumentor",
        );
        return;
      }
      this._wrap(
        target,
        "create",
        index === 0
          ? this.createChatCompletionWrapper(this)
          : this.createCompletionWrapper(this),
      );
      this.patches.set(target, target.create);
      patchedTargets++;
    });
    if (!found)
      diag.warn(
        "@traceai/vllm: no supported OpenAI resource prototypes found; nothing patched",
      );
    return module;
  }

  private restore(target: ResourcePrototype) {
    const owned = this.patches.get(target);
    if (!owned) return;
    // Never unwrap a replacement installed by another owner after this instrumentor.
    if (target.create === owned) this._unwrap(target, "create");
    this.patches.delete(target);
    patchedTargets--;
  }

  private unpatch(module: OpenAIModule) {
    for (const target of this.targets(module)) if (target) this.restore(target);
  }

  private createChatCompletionWrapper(instrumentation: VLLMInstrumentation) {
    return instrumentation.createWrapper(true);
  }

  private createCompletionWrapper(instrumentation: VLLMInstrumentation) {
    return instrumentation.createWrapper(false);
  }

  private createWrapper(chat: boolean) {
    const instrumentation = this;
    return (original: Create): Create =>
      function (body, options) {
        if (
          !body ||
          !instrumentation.isEnabled() ||
          isTracingSuppressed(context.active()) ||
          !instrumentation.isVLLMRequest(this)
        ) {
          return original.call(this, body, options);
        }
        const { messages: _messages, prompt: _prompt, ...parameters } = body;
        const hideInputPayload = instrumentation.hideInputPayload;
        const hideOutputPayload = instrumentation.hideOutputPayload;
        const span = instrumentation.fiTracer.startSpan(
          chat ? "vLLM Chat Completions" : "vLLM Completions",
          {
            kind: SpanKind.CLIENT,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: body.model,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.VLLM,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: chat
                ? "chat"
                : "text_completion",
              ...(!hideInputPayload
                ? {
                    [SemanticConventions.INPUT_VALUE]:
                      safelyJSONStringify(body) ?? "",
                    [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                    [SemanticConventions.RAW_INPUT]:
                      safelyJSONStringify(body) ?? "",
                    [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                      safelyJSONStringify(parameters) ?? "",
                    ...(chat
                      ? {
                          ...getChatInputMessagesAttributes(
                            body as ChatCompletionRequest,
                          ),
                          ...getToolsJSONSchema(body as ChatCompletionRequest),
                        }
                      : getPromptAttributes(
                          Array.isArray(body.prompt)
                            ? (body.prompt as string[])
                            : [body.prompt as string],
                        )),
                  }
                : {}),
            },
          },
        );
        const finish = spanEnder(span);
        let result: Promise<unknown>;
        try {
          result = context.with(trace.setSpan(context.active(), span), () =>
            original.call(this, body, options),
          );
        } catch (error) {
          finish(error);
          throw error;
        }
        return observeResponse(
          result,
          (value) => {
            if (
              value &&
              typeof (value as AsyncIterable<ChatCompletion>)[
                Symbol.asyncIterator
              ] === "function"
            ) {
              return observeStream(
                value as StreamResponse<StreamChunk>,
                finish,
                chat,
                hideOutputPayload,
              );
            }
            const response = value as ChatCompletion & Completion;
            if (response && Array.isArray(response.choices)) {
              finish(undefined, {
                [SemanticConventions.LLM_MODEL_NAME]: response.model,
                [SemanticConventions.GEN_AI_RESPONSE_MODEL]: response.model,
                [SemanticConventions.GEN_AI_RESPONSE_ID]: response.id,
                [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]:
                  safelyJSONStringify(
                    response.choices.map((c) => c.finish_reason),
                  ) ?? "[]",
                ...getUsageAttributes(response),
                ...(!hideOutputPayload
                  ? {
                      [SemanticConventions.OUTPUT_VALUE]: chat
                        ? (safelyJSONStringify(response) ?? "")
                        : (response.choices[0]?.text ?? ""),
                      [SemanticConventions.OUTPUT_MIME_TYPE]: chat
                        ? MimeType.JSON
                        : MimeType.TEXT,
                      ...(chat ? getChatOutputMessagesAttributes(response) : {}),
                      [SemanticConventions.RAW_OUTPUT]:
                        safelyJSONStringify(response) ?? "",
                    }
                  : {}),
              });
            } else finish();
            return value;
          },
          (error) => finish(error),
          (status) =>
            finish(
              undefined,
              { "http.response.status_code": status },
              status >= 400 ? SpanStatusCode.ERROR : SpanStatusCode.OK,
            ),
        );
      };
  }
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(
  request: ChatCompletionRequest,
): Attributes {
  const serialized = (request.messages ?? []).map((message) => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) {
      obj.content = message.content;
    }
    if (message.tool_call_id) {
      obj.tool_call_id = message.tool_call_id;
    }
    if (message.tool_calls) obj.tool_calls = message.tool_calls;
    return obj;
  });
  return {
    [SemanticConventions.LLM_INPUT_MESSAGES]:
      safelyJSONStringify(serialized) ?? "[]",
  };
}

/**
 * Gets tool schema attributes
 */
function getToolsJSONSchema(request: ChatCompletionRequest): Attributes {
  if (!request.tools) {
    return {};
  }
  return {
    [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(request.tools) ?? "[]",
  };
}

/**
 * Gets output message attributes from chat response
 */
function getChatOutputMessagesAttributes(response: ChatCompletion): Attributes {
  const choice = response.choices[0];
  if (!choice?.message) {
    return {};
  }

  const msg: Record<string, unknown> = { role: choice.message.role };
  if (choice.message.content) {
    msg.content = choice.message.content;
  }
  if (choice.message.tool_calls) {
    msg.tool_calls = choice.message.tool_calls.map((tc) => ({
      id: tc.id,
      function: { name: tc.function.name, arguments: tc.function.arguments },
    }));
  }
  return {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]:
      safelyJSONStringify([msg]) ?? "[]",
  };
}

/**
 * Gets usage attributes from chat response
 */
function getUsageAttributes(response: ChatCompletion): Attributes {
  if (response.usage) {
    return {
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]:
        response.usage.completion_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]:
        response.usage.prompt_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: response.usage.total_tokens,
    };
  }
  return {};
}

/**
 * Gets prompt attributes for completion requests
 */
function getPromptAttributes(prompts: string[]): Attributes {
  return prompts.reduce((acc: Attributes, prompt, index) => {
    acc[`${SemanticConventions.LLM_PROMPTS}.${index}`] = prompt;
    return acc;
  }, {});
}
