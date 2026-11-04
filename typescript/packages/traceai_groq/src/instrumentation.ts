import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  safeExecuteInTheMiddle,
} from "@opentelemetry/instrumentation";
import {
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
} from "@traceai/fi-core";
import { observeResponse, audioResponseAttributes } from "./response";

const MODULE_NAME = "groq-sdk";
type Operation = "chat" | "transcription" | "translation" | "speech";
const spanNames: Record<Operation, string> = {
  chat: "Groq Chat Completions",
  transcription: "Groq Audio Transcriptions",
  translation: "Groq Audio Translations",
  speech: "Groq Audio Speech",
};
type Create = (this: unknown, ...args: unknown[]) => Promise<unknown>;
interface Resource { create: Create }
interface Patch { original: Create; wrapped: Create }
interface GroqClass {
  Chat?: { Completions?: { prototype: Resource } };
  Audio?: {
    Transcriptions?: { prototype: Resource };
    Translations?: { prototype: Resource };
    Speech?: { prototype: Resource };
  };
}

// SDK copies have different prototypes; module-wide flags cannot express ownership.
const owners = new WeakMap<Resource, GroqInstrumentation>();
let patchedResources = 0;
export function isPatched() {
  return patchedResources > 0;
}
function resources(module: Record<string, unknown>): Array<[Resource | undefined, Operation]> {
  const Groq = (module.Groq ?? module.default ?? module) as GroqClass;
  return [
    [Groq.Chat?.Completions?.prototype, "chat"],
    [Groq.Audio?.Transcriptions?.prototype, "transcription"],
    [Groq.Audio?.Translations?.prototype, "translation"],
    [Groq.Audio?.Speech?.prototype, "speech"],
  ];
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
  tools?: Array<{
    type: string;
    function: {
      name: string;
      description?: string;
      parameters?: Record<string, unknown>;
    };
  }>;
  [key: string]: unknown;
}

/** Invocation metadata is not an input-content channel. Unknown fields stay in input.value. */
function invocationParameters(body: Record<string, unknown>): Record<string, unknown> {
  const parameters: Record<string, unknown> = {};
  for (const key of [
    "model", "temperature", "max_tokens", "max_completion_tokens", "top_p", "stream",
    "n", "seed", "frequency_penalty", "presence_penalty", "logprobs", "top_logprobs",
    "parallel_tool_calls", "disable_tool_validation", "reasoning_effort", "reasoning_format",
    "service_tier",
  ]) {
    const value = body[key];
    if (["string", "number", "boolean"].includes(typeof value)) parameters[key] = value;
  }
  return parameters;
}

/** Audio files belong to the SDK/user: never serialize, inspect, or read them for telemetry. */
function audioInput(body: Record<string, unknown>): Record<string, unknown> {
  const input: Record<string, unknown> = {};
  for (const key of ["model", "language", "prompt", "input", "voice", "response_format", "temperature", "sample_rate", "speed"]) {
    const value = body[key];
    if (typeof value === "string" || typeof value === "number") input[key] = value;
  }
  return input;
}

export class GroqInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  // Base construction can call enable()/patch() before our constructor runs.
  private manualModules?: Set<Record<string, unknown>>;
  private patches?: Map<Resource, Patch>;

  constructor({ instrumentationConfig, traceConfig }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/groq", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  public override enable(): void {
    super.enable();
    this.refreshTracer();
    this.manualModules?.forEach(module => this.patch(module));
  }

  public override disable(): void {
    super.disable();
    this.patches?.forEach((_patch, resource) => this.unpatchResource(resource));
  }

  public readonly instrumentationName = "@traceai/groq";

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME, [">=0.3.0"], this.patch.bind(this), this.unpatch.bind(this),
    );
  }

  /** Register imported modules, including read-only ESM namespaces, for enable/disable. */
  manuallyInstrument(module: Record<string, unknown>) {
    (this.manualModules ??= new Set()).add(module);
    if (this.isEnabled()) this.patch(module);
  }

  private patch(module: Record<string, unknown>) {
    for (const [resource, operation] of resources(module)) {
      if (!resource || typeof resource.create !== "function" || owners.has(resource)) continue;
      const original = resource.create;
      const wrapped = this.createWrapper(original, operation);
      // Do not use shimmer's wrap here: it unwraps other libraries' existing wrappers.
      resource.create = wrapped;
      (this.patches ??= new Map()).set(resource, { original, wrapped });
      owners.set(resource, this);
      patchedResources++;
    }
    return module;
  }

  private unpatch(module: Record<string, unknown>) {
    for (const [resource] of resources(module)) if (resource) this.unpatchResource(resource);
  }

  private unpatchResource(resource: Resource) {
    const patch = this.patches?.get(resource);
    if (!patch || owners.get(resource) !== this) return;
    // An outer wrapper may still call ours. Leave it intact and let isEnabled()
    // bypass tracing while disabled; re-enable must not add a second wrapper.
    if (resource.create !== patch.wrapped) return;
    resource.create = patch.original;
    this.patches!.delete(resource);
    owners.delete(resource);
    patchedResources--;
  }

  private createWrapper(original: Create, operation: Operation): Create {
    const instrumentation = this;
    return function (...args: unknown[]) {
      if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) return original.apply(this, args);
      const body = args[0] as Record<string, unknown> | undefined;
      // Preserve SDK validation/error behavior for invalid arguments.
      if (!body || typeof body !== "object") return original.apply(this, args);
      const input = operation === "chat" ? body : audioInput(body);
      const parameters = operation === "chat" ? invocationParameters(body) : { ...input };
      if (operation !== "chat") { delete parameters.prompt; delete parameters.input; }
      const span = instrumentation.fiTracer.startSpan(
        spanNames[operation],
        {
          kind: SpanKind.INTERNAL,
          attributes: {
            [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
            [SemanticConventions.LLM_MODEL_NAME]: typeof body.model === "string" ? body.model : undefined,
            [SemanticConventions.INPUT_VALUE]: safelyJSONStringify(input) ?? undefined,
            [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
            [SemanticConventions.LLM_INVOCATION_PARAMETERS]: safelyJSONStringify(parameters) ?? undefined,
            [SemanticConventions.LLM_PROVIDER]: LLMProvider.GROQ,
            [SemanticConventions.GEN_AI_OPERATION_NAME]: operation,
            ...(operation === "chat" ? {
              ...getLLMInputMessagesAttributes(body as unknown as GroqChatCompletionCreateParams),
              ...getLLMToolsJSONSchema(body as unknown as GroqChatCompletionCreateParams),
            } : {}),
            [SemanticConventions.RAW_INPUT]: safelyJSONStringify(input) ?? "",
          },
        },
      );
      const execContext = trace.setSpan(context.active(), span);
      const execPromise = safeExecuteInTheMiddle(
        () => context.with(execContext, () => original.apply(this, args)),
        error => {
          if (error) {
            span.recordException(error);
            span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
            span.end();
          }
        },
      );
      // Speech resolves to a binary Response owned by the caller. Record completion
      // at response availability, without reading/cloning the body or logging audio.
      const output = operation === "chat" ? undefined : operation === "speech" ? () => ({}) : audioResponseAttributes;
      return observeResponse(execPromise, span, execContext, output);
    };
  }
}

function getLLMInputMessagesAttributes(body: GroqChatCompletionCreateParams): Attributes {
  if (!Array.isArray(body.messages)) return {};
  const serialized = body.messages.map(message => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) obj.content = message.content;
    if (message.tool_call_id) obj.tool_call_id = message.tool_call_id;
    return obj;
  });
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]" };
}

function getLLMToolsJSONSchema(body: GroqChatCompletionCreateParams): Attributes {
  if (!body.tools) return {};
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(body.tools) ?? "[]" };
}
