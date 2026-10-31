import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  safeExecuteInTheMiddle,
} from "@opentelemetry/instrumentation";
import {
  diag,
  context,
  trace,
  SpanKind,
  Attributes,
  SpanStatusCode,
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
} from "@traceai/fi-core";
import { observeResponse } from "./response";

const MODULE_NAME = "groq-sdk";

/**
 * Flag to check if the groq module has been patched
 */
let _isFIPatched = false;

/**
 * Function to check if instrumentation is enabled / disabled
 */
export function isPatched() {
  return _isFIPatched;
}

// Request fields used by instrumentation; all other SDK options pass through.
interface GroqChatCompletionCreateParams {
  model: string;
  messages: Array<{
    role: string;
    content: string;
    tool_call_id?: string;
    name?: string;
  }>;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  stream?: boolean;
  tools?: Array<{
    type: string;
    function: {
      name: string;
      description?: string;
      parameters?: Record<string, unknown>;
    };
  }>;
  tool_choice?: string | { type: string; function: { name: string } };
  [key: string]: unknown;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class GroqInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/groq",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public readonly instrumentationName = "@traceai/groq";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "groq-sdk",
      [">=0.3.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Groq module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Groq module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: GroqInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch Chat Completions
    const Groq = module.Groq as {
      Chat?: { Completions?: { prototype: Record<string, unknown> } };
    } | undefined;

    if (Groq?.Chat?.Completions?.prototype) {
      this._wrap(
        Groq.Chat.Completions.prototype,
        "create",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (original: any): any => {
          return function patchedCreate(
            this: unknown,
            ...args: [GroqChatCompletionCreateParams, Record<string, unknown>?]
          ) {
            if (isTracingSuppressed(context.active())) return original.apply(this, args);
            const body = args[0];
            const { messages: _messages, ...invocationParameters } = body;

            const span = instrumentation.fiTracer.startSpan(
              `Groq Chat Completions`,
              {
                kind: SpanKind.INTERNAL,
                attributes: {
                  [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
                  [SemanticConventions.LLM_MODEL_NAME]: body.model,
                  [SemanticConventions.INPUT_VALUE]: safelyJSONStringify(body) ?? undefined,
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                    safelyJSONStringify(invocationParameters) ?? undefined,
                  [SemanticConventions.LLM_PROVIDER]: LLMProvider.GROQ,
                  [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
                  ...getLLMInputMessagesAttributes(body),
                  ...getLLMToolsJSONSchema(body),
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
                },
              },
            );

            const execContext = trace.setSpan(context.active(), span);
            const execPromise = safeExecuteInTheMiddle(
              () => {
                return context.with(execContext, () => {
                  return original.apply(this, args);
                });
              },
              (error) => {
                if (error) {
                  span.recordException(error);
                  span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
                  span.end();
                }
              },
            );

            return observeResponse(execPromise, span, execContext);
          };
        },
      );
    }

    _isFIPatched = true;
    try {
      module.fiPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }

    return module;
  }

  /**
   * Un-patches the Groq module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const Groq = moduleExports.Groq as {
      Chat?: { Completions?: { prototype: Record<string, unknown> } };
    } | undefined;

    if (Groq?.Chat?.Completions?.prototype) {
      this._unwrap(Groq.Chat.Completions.prototype, "create");
    }

    _isFIPatched = false;
    try {
      moduleExports.fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }
  }
}

/**
 * Converts the body of a chat completions request to LLM input messages
 */
function getLLMInputMessagesAttributes(
  body: GroqChatCompletionCreateParams,
): Attributes {
  const serialized = body.messages.map((message) => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) {
      obj.content = message.content;
    }
    if (message.tool_call_id) {
      obj.tool_call_id = message.tool_call_id;
    }
    return obj;
  });
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]" };
}

/**
 * Converts each tool definition into a json schema
 */
function getLLMToolsJSONSchema(
  body: GroqChatCompletionCreateParams,
): Attributes {
  if (!body.tools) {
    return {};
  }
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(body.tools) ?? "[]" };
}
