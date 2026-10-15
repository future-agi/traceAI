/**
 * Public entry points: an OpenTelemetry instrumentation that patches the
 * `@cursor/sdk` `Agent` class (automatically for `require("@cursor/sdk")`,
 * or through `manuallyInstrument()`), plus `forceFlush()` / `shutdown()` to
 * close and flush spans before a short script exits.
 */
import { TracerProvider, diag, trace } from "@opentelemetry/api";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { ContentPolicy } from "./spans";
import { AgentClassLike, CursorSDKModuleLike } from "./types";
import { VERSION } from "./version";
import { RunWatcher, TracingContext, isPatched, patchAgentClass, wrapAgent } from "./wrappers";

export const INSTRUMENTATION_NAME = "@traceai/cursor-sdk";
const MODULE_NAME = "@cursor/sdk";
const SUPPORTED_VERSIONS = [">=1.0.0 <2"];
const DEFAULT_GRACE_MS = 2000;
const DEFAULT_DRAIN_MS = 30_000;

/**
 * fi-core env keys. Read fail-closed, unlike fi-core (`value.toLowerCase() ===
 * "true"`): only an explicit `false` (trimmed, any case) opts in to content.
 */
const FI_HIDE_INPUTS = "FI_HIDE_INPUTS";
const FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS";

export interface CursorSDKInstrumentationConfig {
  /** OpenTelemetry instrumentation options (`enabled`...). */
  instrumentationConfig?: InstrumentationConfig;
  /** Provider to create spans with. Defaults to the global provider. */
  tracerProvider?: TracerProvider;
  /**
   * fi-core trace config. Unlike fi-core, `hideInputs` and `hideOutputs`
   * default to `true` here: prompts, tool arguments (shell commands, file
   * paths, edits), tool results (file contents, command output), thinking and
   * assistant text of a coding agent carry source code and secrets. Opt in
   * with `false`, or with `FI_HIDE_INPUTS=false` / `FI_HIDE_OUTPUTS=false`.
   */
  traceConfig?: TraceConfigOptions;
  /**
   * Milliseconds a run span waits, after the run's end was signalled without a
   * `RunResult`, for `run.wait()` or a terminal status before it closes.
   * Default 2000. `forceFlush()` / `shutdown()` close waiting runs at once.
   */
  runEndGraceMs?: number;
  /**
   * Milliseconds a run span stays open, after the run's end is known, for a
   * `run.stream()` that is still being read (or was read partly and never
   * closed). Default 30000. Open tool spans then end as incomplete.
   */
  streamDrainTimeoutMs?: number;
}

function nonNegative(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : fallback;
}

/** `false` only for an explicit `false`; any other value (or none) keeps content hidden. */
function envHide(key: string): boolean {
  const value = typeof process !== "undefined" ? process.env?.[key] : undefined;
  return !(typeof value === "string" && value.trim().toLowerCase() === "false");
}

/** Precedence: explicit option > fi-core env var > this package's default (hidden). */
export function resolveContentPolicy(traceConfig?: TraceConfigOptions): ContentPolicy {
  return {
    hideInputs: traceConfig?.hideInputs ?? envHide(FI_HIDE_INPUTS),
    hideOutputs: traceConfig?.hideOutputs ?? envHide(FI_HIDE_OUTPUTS),
  };
}

/** Instrumentations created in this process, so `shutdown()` can close their waiting runs. */
const instances = new Set<WeakRef<CursorSDKInstrumentation>>();

/**
 * Traces `@cursor/sdk` agent runs.
 *
 * ```ts
 * import * as CursorSDK from "@cursor/sdk";
 * const instrumentation = new CursorSDKInstrumentation({ tracerProvider });
 * instrumentation.manuallyInstrument(CursorSDK);
 * const agent = await CursorSDK.Agent.create({ model: { id: "composer-2.5" }, local: { cwd } });
 * const run = await agent.send("...");          // one "Cursor Agent run" span
 * for await (const event of run.stream()) {}    // tool / thinking / assistant child spans
 * ```
 *
 * `require("@cursor/sdk")` after `registerInstrumentations()` is patched
 * automatically. ESM imports need `manuallyInstrument()` (or an
 * import-in-the-middle loader).
 */
export class CursorSDKInstrumentation extends InstrumentationBase {
  private readonly traceConfig?: TraceConfigOptions;
  private readonly graceMs: number;
  private readonly drainMs: number;
  /** Set by `unpatch()`: agents created earlier pass through untraced. */
  private stopped = false;
  private readonly registry = new Set<RunWatcher>();
  private readonly undo = new Map<object, () => void>();
  private providerForFlush?: TracerProvider;

  constructor(config: CursorSDKInstrumentationConfig = {}) {
    super(INSTRUMENTATION_NAME, VERSION, Object.assign({}, config.instrumentationConfig));
    this.traceConfig = config.traceConfig;
    this.graceMs = nonNegative(config.runEndGraceMs, DEFAULT_GRACE_MS);
    this.drainMs = nonNegative(config.streamDrainTimeoutMs, DEFAULT_DRAIN_MS);
    if (config.tracerProvider) this.setTracerProvider(config.tracerProvider);
    instances.add(new WeakRef(this));
  }

  override setTracerProvider(tracerProvider: TracerProvider): void {
    super.setTracerProvider(tracerProvider);
    this.providerForFlush = tracerProvider;
  }

  /** The content policy this instrumentation applies. */
  get contentPolicy(): ContentPolicy {
    return resolveContentPolicy(this.traceConfig);
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      SUPPORTED_VERSIONS,
      (moduleExports: CursorSDKModuleLike) => {
        this.patchModule(moduleExports);
        return moduleExports;
      },
      (moduleExports: CursorSDKModuleLike) => {
        this.unpatchModule(moduleExports);
      },
    );
  }

  /**
   * Patch the SDK's `Agent` class. Pass the module namespace
   * (`import * as CursorSDK from "@cursor/sdk"`) or `{ Agent }`. Returns
   * `true` when the class is patched after this call.
   */
  manuallyInstrument(module: CursorSDKModuleLike): boolean {
    return this.patchModule(module);
  }

  /** Trace one existing `SDKAgent` (from any source). Returns the same instance. */
  wrapAgent<T extends object>(agent: T): T {
    this.stopped = false;
    return wrapAgent(agent, () => this.tracingContext());
  }

  /**
   * Remove the class patches this instrumentation applied. Agents created
   * before this call stop tracing too (their wrapped `send()` passes straight
   * through) until `manuallyInstrument()` or `wrapAgent()` is called again.
   */
  unpatch(): void {
    this.stopped = true;
    for (const [target, undo] of this.undo) {
      this.undo.delete(target);
      try {
        undo();
      } catch (error) {
        diag.debug(`${INSTRUMENTATION_NAME}: unpatch failed: ${error}`);
      }
    }
  }

  override disable(): void {
    super.disable();
    this.closeRuns(false);
  }

  /**
   * Close runs whose end is already known but whose span is still open
   * (waiting for `wait()` / a terminal status, or for a stream that is still
   * being read), then flush the provider's span processors. Does not shut
   * the provider down.
   */
  async forceFlush(): Promise<void> {
    this.closeRuns(false);
    await flushProvider(this.providerForFlush ?? trace.getTracerProvider());
  }

  /** `all`: also close runs still in progress (process exit). */
  private closeRuns(all: boolean): void {
    // disable() can run inside the base constructor, before fields exist.
    if (!this.registry) return;
    for (const watcher of [...this.registry]) {
      if (all || watcher.inGrace || watcher.outcomeKnown) watcher.flush();
    }
  }

  private patchModule(module: CursorSDKModuleLike): boolean {
    const AgentClass = module?.Agent as AgentClassLike | undefined;
    if (!AgentClass || typeof AgentClass.create !== "function") {
      diag.warn(`${INSTRUMENTATION_NAME}: expected the @cursor/sdk module (no Agent.create found); nothing patched`);
      return false;
    }
    this.stopped = false;
    if (isPatched(AgentClass)) return true;
    const undo = patchAgentClass(AgentClass, () => this.tracingContext());
    if (undo) this.undo.set(AgentClass, undo);
    return !!undo;
  }

  private unpatchModule(module: CursorSDKModuleLike): void {
    const AgentClass = module?.Agent as object | undefined;
    const undo = AgentClass ? this.undo.get(AgentClass) : undefined;
    if (!AgentClass || !undo) return;
    this.undo.delete(AgentClass);
    undo();
  }

  private tracingContext(): TracingContext | undefined {
    if (!this.isEnabled() || this.stopped) return undefined;
    const policy = this.contentPolicy;
    const tracer = new FITracer({
      tracer: this.tracer,
      traceConfig: { ...this.traceConfig, hideInputs: policy.hideInputs, hideOutputs: policy.hideOutputs },
    });
    return { tracer, policy, graceMs: this.graceMs, drainMs: this.drainMs, registry: this.registry };
  }
}

/** Python-style convenience constructor: create, then `manuallyInstrument(module)`. */
export function instrumentCursorSDK(
  module: CursorSDKModuleLike,
  config: CursorSDKInstrumentationConfig = {},
): CursorSDKInstrumentation {
  const instrumentation = new CursorSDKInstrumentation(config);
  instrumentation.manuallyInstrument(module);
  return instrumentation;
}

/**
 * Before a short script exits: close every open run span of every
 * instrumentation in this process (a run still in progress closes with
 * `cursor.run.status=running` and no status), then flush the provider.
 * Export errors are logged, never thrown: export failure does not fail the agent.
 */
export async function shutdown(tracerProvider?: TracerProvider): Promise<void> {
  for (const ref of [...instances]) {
    const instrumentation = ref.deref();
    if (!instrumentation) {
      instances.delete(ref);
      continue;
    }
    (instrumentation as unknown as { closeRuns(all: boolean): void }).closeRuns(true);
  }
  await flushProvider(tracerProvider ?? trace.getTracerProvider());
}

function resolveDelegate(provider: TracerProvider): TracerProvider {
  const maybeProxy = provider as TracerProvider & {
    getDelegate?: () => TracerProvider;
    forceFlush?: () => Promise<void>;
  };
  if (typeof maybeProxy.forceFlush !== "function" && typeof maybeProxy.getDelegate === "function") {
    return maybeProxy.getDelegate();
  }
  return provider;
}

async function flushProvider(provider: TracerProvider): Promise<void> {
  const target = resolveDelegate(provider) as TracerProvider & { forceFlush?: () => Promise<void> };
  if (typeof target.forceFlush !== "function") return;
  try {
    await target.forceFlush();
  } catch (error) {
    diag.warn(`${INSTRUMENTATION_NAME}: forceFlush failed: ${error}`);
  }
}
