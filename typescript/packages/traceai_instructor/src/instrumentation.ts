/** OpenTelemetry extraction/validation spans for Instructor's Zod-based SDK. */
import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { Context, Span, SpanKind, SpanStatusCode, context, trace } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions, FISpanKind, MimeType } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface InstructorInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

// The instrumentor does not import the SDK: doing so would load it before the require hook.
type Callable = (...args: any[]) => any;
type Completions = { create: Callable };
type Client = { chat?: { completions?: Completions } };
type InstructorModule = {
  default?: Callable;
  Instructor?: { prototype?: Client };
  _fiPatched?: boolean;
};
type PatchState = { active: boolean; restore: Array<() => void> };
let owners = new WeakMap<InstructorModule, PatchState>();
let activePatches = 0;

export class InstructorInstrumentation extends InstrumentationBase {
  private readonly traceConfig?: TraceConfigOptions;
  private readonly patches = new Map<InstructorModule, PatchState>();

  constructor(config: InstructorInstrumentationConfig = {}) {
    // InstrumentationBase can call enable() from its constructor. Initialize our state first.
    super("@traceai/fi-instrumentation-instructor", VERSION, {
      ...config.instrumentationConfig, enabled: false,
    });
    this.traceConfig = config.traceConfig;
    this.setConfig({ ...config.instrumentationConfig, enabled: config.instrumentationConfig?.enabled !== false });
    if (this.getConfig().enabled) this.enable();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@instructor-ai/instructor", ["^1.0.0"], this.patch.bind(this), this.unpatch.bind(this)
    );
  }

  /** Patch a mutable module export object before constructing Instructor clients. */
  manuallyInstrument(instructorModule: InstructorModule | null | undefined): void {
    if (instructorModule) this.patch(instructorModule);
  }

  override disable(): void {
    super.disable();
    // The OTel loader only unpatches modules it loaded, not manually supplied modules.
    for (const module of this.patches.keys()) this.unpatch(module);
  }

  private patch(module: InstructorModule): InstructorModule {
    if (owners.has(module) || module._fiPatched) return module;
    if (typeof module.default !== "function" && !module.Instructor?.prototype?.chat?.completions) return module;
    const state: PatchState = { active: true, restore: [] };
    const instrumentation = this;
    const clients = new WeakSet<Completions>();
    const patchClient = (client: Client) => {
      const completions = client?.chat?.completions;
      if (!completions || typeof completions.create !== "function" || clients.has(completions)) return;
      clients.add(completions);
      const original = completions.create;
      completions.create = function (...args: any[]) {
        if (!state.active || isTracingSuppressed(context.active()) || !args[0]?.response_model) {
          return original.apply(this, args);
        }
        return instrumentation.traceStructuredExtraction(original, this, args);
      };
      // Do not retain clients here. Their wrappers become inert when this module is disabled.
    };
    if (typeof module.default === "function") {
      const original = module.default;
      const wrapped = function (this: unknown, ...args: any[]) {
        const client = original.apply(this, args);
        if (state.active) patchClient(client);
        return client;
      };
      module.default = wrapped;
      state.restore.push(() => { if (module.default === wrapped) module.default = original; });
    }
    const prototype = module.Instructor?.prototype;
    if (prototype?.chat?.completions) {
      const completions = prototype.chat.completions;
      const original = completions.create;
      patchClient(prototype);
      const wrapped = completions.create;
      state.restore.push(() => { if (completions.create === wrapped) completions.create = original; });
    }
    module._fiPatched = true;
    owners.set(module, state);
    this.patches.set(module, state);
    activePatches++;
    return module;
  }

  private unpatch(module: InstructorModule): void {
    const state = this.patches.get(module);
    if (!state) return;
    state.active = false;
    for (const restore of state.restore) restore();
    module._fiPatched = false;
    owners.delete(module);
    this.patches.delete(module);
    activePatches--;
  }

  private async traceStructuredExtraction(original: Callable, instance: unknown, args: any[]): Promise<unknown> {
    const params = args[0];
    const responseModel = typeof params.response_model?.name === "string" ? params.response_model.name : "unknown";
    // Resolve the tracer at call time, including providers assigned after enable().
    const tracer = new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig });
    const span = tracer.startSpan(`Instructor Extract: ${responseModel}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(params.messages),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
        "instructor.response_model": responseModel,
        "instructor.model": typeof params.model === "string" ? params.model : undefined,
      },
    });
    const execContext = trace.setSpan(context.active(), span);
    try {
      const result = await context.with(execContext, () => original.apply(instance, args));
      if (isAsyncIterable(result)) return traceIterator(result, span, execContext);
      captureOutput(span, result);
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    } catch (error) {
      endWithError(span, error);
      throw error;
    }
  }
}

/** Whether this package owns any active module patches. */
export function isPatched(): boolean { return activePatches > 0; }

/** Reset diagnostics for isolated tests; disable instrumentors first. */
export function _resetPatchedStateForTesting(): void {
  owners = new WeakMap();
  activePatches = 0;
}

function isAsyncIterable(value: unknown): value is AsyncIterable<unknown> {
  return value != null && typeof (value as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function";
}

function traceIterator(source: AsyncIterable<unknown>, span: Span, execContext: Context): AsyncIterableIterator<unknown> {
  const iterator = context.with(execContext, () => source[Symbol.asyncIterator]());
  let ended = false;
  let cancelled = false;
  let latest: unknown;
  const finish = () => {
    if (ended) return;
    ended = true;
    captureOutput(span, latest);
    if (cancelled) span.setAttribute("instructor.stream.cancelled", true);
    else span.setStatus({ code: SpanStatusCode.OK });
    span.end();
    latest = undefined;
  };
  const advance = async (method: "next" | "return" | "throw", args: unknown[]): Promise<IteratorResult<unknown>> => {
    try {
      if (method === "return") cancelled = true;
      const result = await context.with(execContext, () => {
        const operation = iterator[method];
        if (operation) return operation.apply(iterator, args as []);
        if (method === "throw") throw args[0];
        return { done: true, value: args[0] };
      });
      if (!ended) {
        if (!result.done) {
          latest = result.value;
          captureUsage(span, latest);
        } else finish();
      }
      return result;
    } catch (error) {
      if (!ended) { ended = true; latest = undefined; endWithError(span, error); }
      throw error;
    }
  };
  // Unlike an async-generator wrapper, return()/throw() before first next() also finalize the span.
  return {
    next: (...args: [] | [unknown]) => advance("next", args),
    return: (...args: [unknown?]) => advance("return", args),
    throw: (...args: [unknown?]) => advance("throw", args),
    [Symbol.asyncIterator]() { return this; },
  };
}

function captureOutput(span: Span, value: unknown): void {
  if (value === undefined) return;
  captureUsage(span, value);
  // Instructor's metadata is not the extracted document (and can contain internal parser state).
  const output = value !== null && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).filter(([key]) => key !== "_meta")) : value;
  const serialized = safeJsonStringify(output);
  if (serialized !== undefined) span.setAttributes({
    [SemanticConventions.OUTPUT_VALUE]: serialized,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
  });
}

function captureUsage(span: Span, value: unknown): void {
  const usage = (value as { _meta?: { usage?: Record<string, unknown> } } | null)?._meta?.usage;
  // A CHAIN span is not another model request. Only provider instrumentation owns gen_ai usage/cost.
  for (const key of ["prompt_tokens", "completion_tokens", "total_tokens"]) {
    const count = usage?.[key];
    if (typeof count === "number" && Number.isFinite(count) && count >= 0) {
      span.setAttribute(`instructor.usage.${key}`, count);
    }
  }
}

function endWithError(span: Span, error: unknown): void {
  const exception = error instanceof Error || typeof error === "string" ? error : { name: "Error" };
  span.recordException(exception);
  span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : undefined });
  span.end();
}

function safeJsonStringify(value: unknown): string | undefined {
  try { return JSON.stringify(value); } catch { return undefined; }
}
