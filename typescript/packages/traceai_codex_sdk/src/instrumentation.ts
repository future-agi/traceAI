/**
 * Public entry points: an instrumentation object that patches the SDK's
 * `Thread` class (or single `Codex` / `Thread` instances), and a `shutdown()`
 * that flushes the tracer provider before a short script exits.
 */
import { TracerProvider, diag, trace } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
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
 */
export class CodexSDKInstrumentation {
  private tracerProvider?: TracerProvider;
  private readonly traceConfig?: TraceConfigOptions;
  private readonly undo: Array<() => void> = [];

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
    const undo = patchThread(proto, () => this.tracingContext());
    if (undo) this.undo.push(undo);
    return !!undo;
  }

  /** Patch one Thread instance. Returns the same instance. */
  wrapThread<T extends object>(thread: T): T {
    const undo = patchThread(thread as ThreadLike, () => this.tracingContext());
    if (undo) this.undo.push(undo);
    return thread;
  }

  /** Patch one Codex instance so every thread it starts or resumes is traced. Returns the same instance. */
  wrapCodex<T extends object>(codex: T): T {
    if (!codex || typeof codex !== "object") return codex;
    const target = codex as Record<string, unknown>;
    for (const method of ["startThread", "resumeThread"] as const) {
      const original = target[method] as ((...args: unknown[]) => object) | undefined;
      if (typeof original !== "function") continue;
      const hadOwn = Object.prototype.hasOwnProperty.call(codex, method);
      const self = this;
      Object.defineProperty(codex, method, {
        value: function tracedThreadFactory(this: unknown, ...args: unknown[]) {
          return self.wrapThread(original.apply(this, args));
        },
        writable: true,
        configurable: true,
      });
      this.undo.push(() => {
        if (hadOwn) Object.defineProperty(codex, method, { value: original, writable: true, configurable: true });
        else delete target[method];
      });
    }
    return codex;
  }

  /** Remove every patch this instrumentation applied. */
  unpatch(): void {
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

  private tracingContext(): TurnTracingContext {
    const policy = this.contentPolicy;
    const provider = this.tracerProvider ?? trace.getTracerProvider();
    const tracer = new FITracer({
      tracer: provider.getTracer(INSTRUMENTATION_NAME, VERSION),
      traceConfig: { ...this.traceConfig, hideInputs: policy.hideInputs, hideOutputs: policy.hideOutputs },
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
