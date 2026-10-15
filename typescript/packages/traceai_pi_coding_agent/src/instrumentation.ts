/**
 * Public entry points: an instrumentation object that traces Pi `AgentSession`
 * objects, a wrapper for `createAgentSession()`, and `shutdown()` to flush
 * spans before a short script exits.
 *
 * The Pi SDK is ESM-only and its module namespace cannot be patched, so the
 * application either wraps `createAgentSession` or passes each session to
 * `instrumentSession()`.
 */
import { TracerProvider, diag, trace } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { ContentPolicy, DEFAULT_MAX_CONTENT_LENGTH } from "./content";
import { PiSessionTracer } from "./sessionTracer";
import { CreateAgentSessionLike, PiSessionLike } from "./types";
import { VERSION } from "./version";

export const INSTRUMENTATION_NAME = "@traceai/pi-coding-agent";

/**
 * fi-core env keys. Read fail-closed: only an explicit `false` (trimmed, any
 * case) opts in to content capture.
 */
const FI_HIDE_INPUTS = "FI_HIDE_INPUTS";
const FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS";

export interface PiCodingAgentInstrumentationConfig {
  /** Provider to create spans with. Defaults to the global provider at call time. */
  tracerProvider?: TracerProvider;
  /**
   * fi-core trace config. Unlike fi-core, `hideInputs` and `hideOutputs`
   * default to `true` here: prompts, tool arguments and tool results of a
   * coding agent contain source code, file contents and command output. Opt in
   * with `false`, or with `FI_HIDE_INPUTS=false` / `FI_HIDE_OUTPUTS=false`.
   */
  traceConfig?: TraceConfigOptions;
  /** Longest exported text value, in characters, when content is on. Default 16384. */
  maxContentLength?: number;
}

/** `false` only for an explicit `false`; any other value (or none) keeps content hidden. */
function envHide(key: string): boolean {
  const value = typeof process !== "undefined" ? process.env?.[key] : undefined;
  return !(typeof value === "string" && value.trim().toLowerCase() === "false");
}

/**
 * Resolve content capture. Precedence: explicit option > fi-core env var >
 * this package's default (hidden).
 */
export function resolveContentPolicy(
  traceConfig?: TraceConfigOptions,
  maxContentLength: number = DEFAULT_MAX_CONTENT_LENGTH,
): ContentPolicy {
  return {
    hideInputs: traceConfig?.hideInputs ?? envHide(FI_HIDE_INPUTS),
    hideOutputs: traceConfig?.hideOutputs ?? envHide(FI_HIDE_OUTPUTS),
    maxContentLength,
  };
}

/**
 * Providers passed to an instrumentation, for `shutdown()` with no argument.
 * Held weakly so an app that drops a provider lets it be collected.
 */
const knownProviders = new Set<WeakRef<TracerProvider>>();

function liveProviders(): TracerProvider[] {
  const live: TracerProvider[] = [];
  for (const ref of knownProviders) {
    const provider = ref.deref();
    if (provider) live.push(provider);
    else knownProviders.delete(ref);
  }
  return live;
}

function trackProvider(provider: TracerProvider): void {
  if (!liveProviders().includes(provider)) {
    knownProviders.add(new WeakRef(provider));
  }
}

/** Sessions already traced, so a second `instrumentSession()` call is a no-op. */
const tracedSessions = new WeakMap<object, () => void>();

/**
 * Traces Pi Coding Agent SDK sessions.
 *
 * ```ts
 * import { createAgentSession } from "@earendil-works/pi-coding-agent";
 * const instrumentation = new PiCodingAgentInstrumentation({ tracerProvider });
 * const { session } = await instrumentation.wrapCreateAgentSession(createAgentSession)({ ... });
 * // or: instrumentation.instrumentSession(session);
 * ```
 */
export class PiCodingAgentInstrumentation {
  private tracerProvider?: TracerProvider;
  private readonly traceConfig?: TraceConfigOptions;
  private readonly maxContentLength: number;

  constructor(config: PiCodingAgentInstrumentationConfig = {}) {
    this.traceConfig = config.traceConfig;
    this.maxContentLength = config.maxContentLength ?? DEFAULT_MAX_CONTENT_LENGTH;
    if (config.tracerProvider) {
      this.setTracerProvider(config.tracerProvider);
    }
  }

  setTracerProvider(tracerProvider: TracerProvider): void {
    this.tracerProvider = tracerProvider;
    trackProvider(tracerProvider);
  }

  /** The content policy this instrumentation applies. */
  get contentPolicy(): ContentPolicy {
    return resolveContentPolicy(this.traceConfig, this.maxContentLength);
  }

  /**
   * Trace one `AgentSession`. Subscribe before calling `prompt()`. Returns a
   * function that stops tracing and ends any open spans. `session.dispose()`
   * also ends them. Calling it twice for one session returns the first stop
   * function. After an `AgentSessionRuntime` replaces its session (new, resume,
   * fork), call this again with the new session.
   */
  instrumentSession(session: PiSessionLike): () => void {
    const existing = tracedSessions.get(session);
    if (existing) return existing;
    const policy = this.contentPolicy;
    const provider = this.tracerProvider ?? trace.getTracerProvider();
    const tracer = new FITracer({
      tracer: provider.getTracer(INSTRUMENTATION_NAME, VERSION),
      traceConfig: { ...this.traceConfig, hideInputs: policy.hideInputs, hideOutputs: policy.hideOutputs },
    });
    const sessionTracer = new PiSessionTracer(session, tracer, policy);
    const stop = sessionTracer.attach();
    tracedSessions.set(session, stop);
    wrapDispose(session, stop);
    return stop;
  }

  /** Return a `createAgentSession()` that traces every session it creates. */
  wrapCreateAgentSession<F extends CreateAgentSessionLike>(createAgentSession: F): F {
    const instrumentation = this;
    const wrapped = async function tracedCreateAgentSession(this: unknown, ...args: unknown[]) {
      const result = await createAgentSession.apply(this, args);
      try {
        if (result?.session) instrumentation.instrumentSession(result.session);
      } catch (error) {
        diag.warn(`${INSTRUMENTATION_NAME}: could not instrument session: ${error}`);
      }
      return result;
    };
    return wrapped as unknown as F;
  }

  /** Flush the provider's span processors. Does not shut the provider down. */
  async forceFlush(): Promise<void> {
    await flushProvider(this.tracerProvider ?? trace.getTracerProvider());
  }

  /** Same as `forceFlush()`; named for short scripts. */
  async shutdown(): Promise<void> {
    await this.forceFlush();
  }
}

/** End open spans when the app disposes the session; Pi drops listeners on dispose. */
function wrapDispose(session: PiSessionLike, stop: () => void): void {
  const original = session.dispose;
  if (typeof original !== "function") return;
  try {
    Object.defineProperty(session, "dispose", {
      configurable: true,
      writable: true,
      value: function tracedDispose(this: unknown, ...args: unknown[]) {
        stop();
        return original.apply(this ?? session, args as []);
      },
    });
  } catch (error) {
    diag.debug(`${INSTRUMENTATION_NAME}: could not wrap dispose(): ${error}`);
  }
}

/** Python-style convenience constructor. */
export function instrumentPiCodingAgent(
  config: PiCodingAgentInstrumentationConfig = {},
): PiCodingAgentInstrumentation {
  return new PiCodingAgentInstrumentation(config);
}

/** Trace one session in one call. */
export function instrumentSession(
  session: PiSessionLike,
  config: PiCodingAgentInstrumentationConfig = {},
): () => void {
  return new PiCodingAgentInstrumentation(config).instrumentSession(session);
}

/** Wrap `createAgentSession()` in one call. */
export function wrapCreateAgentSession<F extends CreateAgentSessionLike>(
  createAgentSession: F,
  config: PiCodingAgentInstrumentationConfig = {},
): F {
  return new PiCodingAgentInstrumentation(config).wrapCreateAgentSession(createAgentSession);
}

/**
 * Flush spans before a short script exits. Flushes `tracerProvider` when
 * given; otherwise every provider passed to an instrumentation plus the global
 * provider. Export errors are logged, never thrown.
 */
export async function shutdown(tracerProvider?: TracerProvider): Promise<void> {
  const providers = tracerProvider ? [tracerProvider] : [...liveProviders(), trace.getTracerProvider()];
  const seen = new Set<unknown>();
  for (const provider of providers) {
    const target = resolveDelegate(provider);
    if (seen.has(target)) continue;
    seen.add(target);
    await flushProvider(target);
  }
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
