import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
  InstrumentationNodeModuleFile,
} from "@opentelemetry/instrumentation";
import { Context, Span, SpanKind, SpanStatusCode, context, diag, trace } from "@opentelemetry/api";
import { FITracer, REDACTED_VALUE, TraceConfigOptions, generateTraceConfig } from "@traceai/fi-core";
import { SemanticConventions as SC, FISpanKind, MimeType } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface BeeAIInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

// Structural interfaces keep BeeAI optional and avoid importing a second (CJS/ESM)
// copy of the framework. The application supplies the copy it actually uses.
interface BeeEmitter {
  namespace: string[];
  match(matcher: (event: BeeEvent) => boolean, callback: (data: unknown, event: BeeEvent) => void,
    options: { matchNested: boolean; isBlocking: boolean }): () => void;
}
interface BeeContext {
  instance: { constructor: { name: string }; emitter: BeeEmitter; name?: string; modelId?: string; providerId?: string };
  emitter: BeeEmitter;
  runId: string;
  groupId: string;
  parentId?: string;
  runParams: unknown[];
}
interface BeeEvent {
  name: string;
  creator: BeeContext;
  source: BeeEmitter;
  context: { internal?: boolean };
}
interface BeeRun {
  middleware(callback: (ctx: BeeContext) => void): BeeRun;
}
interface RunSpan {
  span: Span;
  parent: Context;
  kind: FISpanKind;
  hideEmbeddingVectors: boolean;
}
interface Patch { restore(): void }

const wrappers = new WeakSet<Function>();
const activePatches = new Set<Patch>();
const versions = ["^0.1.0"];

/** Trace BeeAI's lazy Run lifecycle without replacing or eagerly awaiting a Run. */
export class BeeAIInstrumentation extends InstrumentationBase {
  private readonly traceConfig?: TraceConfigOptions;
  private patches: Patch[] = [];
  private manualModules = new Set<object>();
  private observed = new WeakSet<BeeContext>();
  private completed = new WeakSet<BeeContext>();
  private events = new WeakSet<BeeEvent>();
  private spans = new Map<string, RunSpan>();

  constructor(config: BeeAIInstrumentationConfig = {}) {
    super("@traceai/fi-instrumentation-beeai", VERSION, config.instrumentationConfig || {});
    this.traceConfig = config.traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "beeai-framework", versions, this.patch.bind(this), () => this.restore(),
      ["beeai-framework/dist/context.cjs", "beeai-framework/dist/context.js"].map(name =>
        new InstrumentationNodeModuleFile(name, versions, this.patch.bind(this), () => this.restore()))
    );
  }

  /**
   * Pass { RunContext } from beeai-framework/context for all native runs, including
   * standalone tools, models and workflows. Root/BaseAgent exports remain supported.
   */
  manuallyInstrument(beeModule: object): void {
    this.manualModules.add(beeModule);
    if (this.isEnabled()) this.patch(beeModule);
  }

  override enable(): void {
    super.enable();
    // InstrumentationBase invokes enable() before subclass fields are initialized.
    this.manualModules?.forEach(module => this.patch(module));
  }

  override disable(): void {
    super.disable();
    this.restore();
  }

  private restore(): void {
    this.patches?.splice(0).forEach(patch => patch.restore());
  }

  private patch(module: object): object {
    const exports = module as Record<string, any>;
    const instrumentation = this;
    if (exports.RunContext?.enter) {
      this.wrap(exports.RunContext, "enter", original => function (this: unknown,
        instance: unknown, input: unknown, handler: (ctx: BeeContext) => unknown) {
        const run = original.call(this, instance, input, (ctx: BeeContext) => {
          const state = instrumentation.spans.get(key(ctx));
          return state
            ? context.with(trace.setSpan(state.parent, state.span), handler, undefined, ctx)
            : handler(ctx);
        }) as BeeRun;
        run.middleware(ctx => instrumentation.observe(ctx, context.active()));
        return run;
      });
    }
    // Compatibility for existing manuallyInstrument(root) and older agent exports.
    // The nested lifecycle events also cover child tool/model/workflow runs.
    for (const name of ["BaseAgent", "BeeAgent", "ReActAgent"]) {
      const prototype = exports[name]?.prototype;
      if (prototype?.run) {
        this.wrap(prototype, "run", original => function (this: unknown, ...args: unknown[]) {
          const run = original.apply(this, args) as BeeRun;
          if (typeof run?.middleware === "function") {
            run.middleware(ctx => instrumentation.observe(ctx, context.active()));
          }
          return run;
        });
      }
    }
    return module;
  }

  private wrap(target: Record<string, any>, name: string, factory: (original: Function) => Function): void {
    const original = target[name];
    if (wrappers.has(original)) return;
    const descriptor = Object.getOwnPropertyDescriptor(target, name);
    const wrapped = factory(original);
    wrappers.add(wrapped);
    Object.defineProperty(target, name, { configurable: true, writable: true, value: wrapped });
    const patch: Patch = { restore: () => {
      // Never remove a wrapper installed by another library after ours.
      if (target[name] === wrapped) {
        if (descriptor) Object.defineProperty(target, name, descriptor);
        else delete target[name];
      }
      activePatches.delete(patch);
    } };
    this.patches.push(patch);
    activePatches.add(patch);
  }

  private observe(ctx: BeeContext, parent: Context): void {
    if (!this.isEnabled() || this.observed.has(ctx)) return;
    this.observed.add(ctx);
    const cleanup = ctx.emitter.match(
      event => event.context?.internal === true && event.source.namespace.slice(-1)[0] === "run",
      (data, event) => {
        // Observers must not change application results, even if telemetry fails.
        try { this.onEvent(data, event, parent); }
        catch { this._diag.warn("Unable to record BeeAI lifecycle event"); }
        if (event.name === "finish" && event.creator === ctx) cleanup();
      },
      { matchNested: true, isBlocking: true }
    );
  }

  private onEvent(data: unknown, event: BeeEvent, parent: Context): void {
    // BeeAI pipes the same event object to direct and ancestor observers. Keep
    // discovery at every depth, but serialize success / record errors only once.
    if (this.events.has(event)) return;
    this.events.add(event);
    const ctx = event.creator;
    const id = key(ctx);
    let state = this.spans.get(id);
    if (event.name === "start") {
      if (!this.isEnabled() || state || this.completed.has(ctx)) return;
      const parentSpan = ctx.parentId && this.spans.get(`${ctx.groupId}:${ctx.parentId}`);
      const spanParent = parentSpan ? trace.setSpan(parentSpan.parent, parentSpan.span) : parent;
      const kind = spanKind(ctx.instance.emitter.namespace);
      // Resolve the tracer at execution time so setTracerProvider and privacy
      // configuration apply equally to automatic and manual registration.
      const config = generateTraceConfig(this.traceConfig);
      // Agent/workflow payloads can contain model messages too. Do not leak a
      // hidden message through a parent's generic input/output JSON attribute.
      const tracer = new FITracer({ tracer: this.tracer, traceConfig: {
        ...config,
        hideInputs: config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages,
        hideOutputs: config.hideOutputs || config.hideOutputMessages || config.hideOutputText,
      } });
      const toolName = kind === FISpanKind.TOOL && typeof ctx.instance.name === "string"
        ? ctx.instance.name : undefined;
      const span = tracer.startSpan(`${toolName || ctx.instance.constructor.name} Run`, {
        kind: SpanKind.INTERNAL,
        attributes: { [SC.FI_SPAN_KIND]: kind, ...(toolName ? { [SC.TOOL_NAME]: toolName } : {}) },
      }, spanParent);
      state = { span, parent: spanParent, kind, hideEmbeddingVectors: config.hideEmbeddingVectors };
      this.spans.set(id, state);
      // Current BeeAI pipes only a run's own events to its parent. Subscribe to
      // each discovered child as well so root-only registration sees deeper runs.
      this.observe(ctx, spanParent);
      // Serialize only operation input, never options, clients or credentials.
      setContent(state, "input", ctx.runParams?.[0]);
      if (kind === FISpanKind.LLM) {
        if (typeof ctx.instance.modelId === "string") span.setAttribute(SC.LLM_MODEL_NAME, ctx.instance.modelId);
        if (typeof ctx.instance.providerId === "string") span.setAttribute(SC.LLM_PROVIDER, ctx.instance.providerId);
      }
    } else if (state) {
      if (event.name === "success") {
        setContent(state, "output", data);
        if (state.kind === FISpanKind.LLM) setUsage(state.span, data);
        state.span.setStatus({ code: SpanStatusCode.OK });
      } else if (event.name === "error") {
        if (data instanceof Error) state.span.recordException(data);
        state.span.setStatus({ code: SpanStatusCode.ERROR });
      } else if (event.name === "finish") {
        this.completed.add(ctx);
        this.spans.delete(id);
        state.span.end();
      }
    }
  }
}

function key(ctx: BeeContext): string { return `${ctx.groupId}:${ctx.runId}`; }

function spanKind(namespace: string[]): FISpanKind {
  // Names after the framework category are application/provider names, not kinds.
  // Models use backend/<provider>/<kind>; LangChain embeddings invert the prefix.
  const category = namespace[0] === "backend" || (namespace[0] === "langchain" && namespace[1] === "backend")
    ? namespace[2] : namespace[0];
  if (category === "tool") return FISpanKind.TOOL;
  if (category === "agent" || (namespace[0] === "a2a" && namespace[1] === "agent")) return FISpanKind.AGENT;
  if (category === "llm" || category === "chat") return FISpanKind.LLM;
  if (category === "embedding") return FISpanKind.EMBEDDING;
  return FISpanKind.CHAIN;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isACPTransportEvent(value: unknown): boolean {
  return isRecord(value) && typeof value.type === "string"
    && /^run\.(completed|failed)$/.test(value.type) && isRecord(value.run);
}

function payloadJSON(value: unknown, hideEmbeddingVectors: boolean): string | undefined {
  return JSON.stringify(value, (field, item: unknown) => {
    // FITracer cannot mask fields after they have become a generic JSON string.
    if (hideEmbeddingVectors && /^(embeddings?|vectors?)$/i.test(field)) return REDACTED_VALUE;
    // Agent results can also be embedded in a workflow/tool's returned state.
    // Export the answer, not runner memory, intermediate iterations or history.
    if (isRecord(item)) {
      if ("result" in item && "memory" in item) return { result: item.result };
      // A2A task history and ACP run completions are transport metadata, not
      // the answer. ACP also embeds the completion in result.meta.event.
      if ("result" in item && "event" in item && isRecord(item.event)
        && (typeof item.event.kind === "string" || isACPTransportEvent(item.event))) return { result: item.result };
      if (isRecord(item.meta) && isACPTransportEvent(item.meta.event)) {
        const { event, ...meta } = item.meta;
        return { ...item, meta };
      }
      // RePlanAgent uses message/intermediateMemory instead of result/memory.
      if ("message" in item && "intermediateMemory" in item) return { message: item.message };
    }
    return item;
  });
}

function setContent(state: RunSpan, direction: "input" | "output", value: unknown): void {
  const { span, kind, hideEmbeddingVectors } = state;
  // No RAW_* copies: all content goes through FITracer's masking policy.
  // Never serialize an embedding model's output into a generic output blob.
  if (!span.isRecording() || (kind === FISpanKind.EMBEDDING && direction === "output")) return;
  try {
    if (kind === FISpanKind.LLM) {
      const structured = direction === "output" && isRecord(value) && "object" in value ? value : undefined;
      const output = structured ? structured.output : value;
      const messages = isRecord(output) ? output.messages : undefined;
      if (Array.isArray(messages)) {
        const json = payloadJSON(messages, hideEmbeddingVectors);
        if (json !== undefined) span.setAttribute(
          direction === "input" ? SC.LLM_INPUT_MESSAGES : SC.LLM_OUTPUT_MESSAGES, json);
      }
      // createStructure returns { object } at the floor and { object, output }
      // on newer BeeAI. Keep the object even when there are no output messages.
      if (!structured) return;
      value = structured.object;
    } else if (kind === FISpanKind.AGENT && direction === "output" && isRecord(value) && "result" in value) {
      value = { result: value.result };
    }
    const json = payloadJSON(value, hideEmbeddingVectors);
    if (json !== undefined) span.setAttributes(direction === "input"
      ? { [SC.INPUT_VALUE]: json, [SC.INPUT_MIME_TYPE]: MimeType.JSON }
      : { [SC.OUTPUT_VALUE]: json, [SC.OUTPUT_MIME_TYPE]: MimeType.JSON });
  } catch {
    // Errors from application-defined toJSON methods can themselves contain secrets.
    diag.warn("BeeAI payload could not be serialized; omitting trace content");
  }
}

function setUsage(span: Span, output: unknown): void {
  const modelOutput = isRecord(output) && "object" in output ? output.output : output;
  const usage = isRecord(modelOutput) && isRecord(modelOutput.usage) ? modelOutput.usage : undefined;
  for (const [field, attribute] of [
    ["promptTokens", SC.LLM_TOKEN_COUNT_PROMPT],
    ["completionTokens", SC.LLM_TOKEN_COUNT_COMPLETION],
    ["totalTokens", SC.LLM_TOKEN_COUNT_TOTAL],
  ]) {
    const count = usage?.[field];
    if (typeof count === "number" && Number.isFinite(count)) span.setAttribute(attribute, count);
  }
}

export function isPatched(): boolean { return activePatches.size > 0; }

/** @internal Testing only. Restores real wrappers instead of clearing a global flag. */
export function _resetPatchedStateForTesting(): void {
  [...activePatches].forEach(patch => patch.restore());
}
