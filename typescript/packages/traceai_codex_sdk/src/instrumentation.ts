/**
 * Public entry points: an instrumentation object that patches the SDK's
 * `Thread` class (or single `Codex` / `Thread` instances), and a `shutdown()`
 * that flushes the tracer provider before a short script exits.
 */
import { TracerProvider, diag, trace } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { patchCodex } from "./codexWrapper";
import { ContentPolicy } from "./spans";
import { TurnTracingContext, patchThread } from "./threadWrapper";
import { CodexModuleLike, ThreadLike } from "./types";
import { VERSION } from "./version";

export const INSTRUMENTATION_NAME = "@traceai/codex-sdk";

/**
 * fi-core env keys. Read fail-closed, unlike fi-core (`value.toLowerCase() ===
 * "true"`): only an explicit `false` (trimmed, any case) opts in to content.
 */
const FI_HIDE_INPUTS = "FI_HIDE_INPUTS";
const FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS";
const FI_HIDE_ERROR_DETAILS = "FI_HIDE_ERROR_DETAILS";

export interface CodexSDKInstrumentationConfig {
  /** Provider to create spans with. Defaults to the global provider at call time. */
  tracerProvider?: TracerProvider;
  /**
   * fi-core trace config. Unlike fi-core, `hideInputs` and `hideOutputs`
   * default to `true` here: prompts, shell commands, command output, file
   * paths and MCP arguments can contain source code or secrets. Opt in with
   * `false`, or with `FI_HIDE_INPUTS=false` / `FI_HIDE_OUTPUTS=false`.
   */
  traceConfig?: TraceConfigOptions;
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
    hideErrorDetails: traceConfig?.hideErrorDetails ?? envHide(FI_HIDE_ERROR_DETAILS),
  };
}

/**
 * Traces `@openai/codex-sdk` turns.
 *
 * ```ts
 * import * as CodexSDK from "@openai/codex-sdk";
 * const instrumentation = new CodexSDKInstrumentation({ tracerProvider });
 * instrumentation.manuallyInstrument(CodexSDK);
 * const thread = new CodexSDK.Codex().startThread();
 * await thread.run("...");   // one codex.turn trace
 * ```
 *
 * The SDK is ESM-only, so its module namespace cannot be patched; its `Thread`
 * class prototype can. `wrapCodex()` / `wrapThread()` patch single instances
 * instead, for apps that prefer not to touch the shared class.
 *
 * This is not an OpenTelemetry `InstrumentationBase`: there is no module to
 * hook on `require`/`import`, so it cannot be passed to
 * `registerInstrumentations()`. Call `manuallyInstrument()` or the wrap methods.
 */
export class CodexSDKInstrumentation {
  private tracerProvider?: TracerProvider;
  private readonly traceConfig?: TraceConfigOptions;
  /** Undo functions for the class patch and wrapCodex() factories. */
  private readonly undo: Array<() => void> = [];
  /** Thread undo functions hold only WeakRefs; collected threads release their bookkeeping. */
  private readonly threadUndo = new Set<() => void>();
  private readonly threadFinalizer = new FinalizationRegistry<() => void>((undo) => {
    this.threadUndo.delete(undo);
  });
  private enabled = true;

  constructor(config: CodexSDKInstrumentationConfig = {}) {
    this.traceConfig = config.traceConfig;
    this.tracerProvider = config.tracerProvider;
  }

  setTracerProvider(tracerProvider: TracerProvider): void {
    this.tracerProvider = tracerProvider;
  }

  /** The content policy this instrumentation applies. */
  get contentPolicy(): ContentPolicy {
    return resolveContentPolicy(this.traceConfig);
  }

  /**
   * Patch `module.Thread.prototype`. Pass the SDK module namespace
   * (`import * as CodexSDK from "@openai/codex-sdk"`) or `{ Thread }`.
   * Returns `true` when the class was patched by this call.
   */
  manuallyInstrument(module: CodexModuleLike): boolean {
    const proto = (module?.Thread as { prototype?: ThreadLike } | undefined)?.prototype;
    if (!proto) {
      diag.warn(`${INSTRUMENTATION_NAME}: manuallyInstrument() expects the @openai/codex-sdk module (no Thread export found)`);
      return false;
    }
    this.enabled = true;
    const undo = patchThread(proto, () => this.tracingContext());
    if (undo) this.undo.push(undo);
    return !!undo;
  }

  /**
   * Patch one Thread instance. Returns the same instance. Only a weak reference
   * is kept, so `unpatch()` can restore live threads without retaining dropped ones.
   */
  wrapThread<T extends object>(thread: T): T {
    this.enabled = true;
    const undo = patchThread(thread as ThreadLike, () => this.tracingContext());
    if (undo) {
      this.threadUndo.add(undo);
      this.threadFinalizer.register(thread, undo, undo);
    }
    return thread;
  }

  /** Patch one Codex instance so every thread it starts or resumes is traced. Returns the same instance. */
  wrapCodex<T extends object>(codex: T): T {
    if (!codex || typeof codex !== "object") return codex;
    this.enabled = true;
    const undo = patchCodex(codex as Record<string | symbol, unknown>, (thread) => this.wrapThread(thread));
    if (undo) this.undo.push(undo);
    return codex;
  }

  /** Remove every patch this instrumentation applied. */
  unpatch(): void {
    this.enabled = false;
    for (const undo of this.threadUndo) {
      this.threadFinalizer.unregister(undo);
      undo();
    }
    this.threadUndo.clear();
    while (this.undo.length) {
      const fn = this.undo.pop();
      try {
        fn?.();
      } catch (error) {
        diag.debug(`${INSTRUMENTATION_NAME}: unpatch failed: ${error}`);
      }
    }
  }

  /** Flush the provider's span processors. Does not shut the provider down. */
  async forceFlush(): Promise<void> {
    await flushProvider(this.tracerProvider ?? trace.getTracerProvider());
  }

  private tracingContext(): TurnTracingContext | undefined {
    if (!this.enabled) return undefined;
    const policy = this.contentPolicy;
    const provider = this.tracerProvider ?? trace.getTracerProvider();
    const tracer = new FITracer({
      tracer: provider.getTracer(INSTRUMENTATION_NAME, VERSION),
      traceConfig: { ...this.traceConfig, hideInputs: policy.hideInputs, hideOutputs: policy.hideOutputs, hideErrorDetails: policy.hideErrorDetails },
    });
    return { tracer, policy };
  }
}

/** Python-style convenience constructor. */
export function instrumentCodexSDK(
  module: CodexModuleLike,
  config: CodexSDKInstrumentationConfig = {},
): CodexSDKInstrumentation {
  const instrumentation = new CodexSDKInstrumentation(config);
  instrumentation.manuallyInstrument(module);
  return instrumentation;
}

/**
 * Flush spans before a short script exits. Export errors are logged, never
 * thrown: export failure does not fail the agent.
 */
export async function shutdown(tracerProvider?: TracerProvider): Promise<void> {
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
