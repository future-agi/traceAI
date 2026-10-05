/**
 * Public entry points: an instrumentation object that wraps `query()`, and a
 * `shutdown()` that flushes the tracer provider before a short script exits.
 */
import { TracerProvider, diag, trace } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { ContentPolicy } from "./spans";
import { QueryTracingContext, wrapQueryFunction } from "./queryWrapper";
import { QueryFunctionLike } from "./types";
import { VERSION } from "./version";

export const INSTRUMENTATION_NAME = "@traceai/claude-agent-sdk";

/**
 * fi-core env keys. Read fail-closed, unlike fi-core (`value.toLowerCase() ===
 * "true"`, so `1`, `yes` or ` true` would turn capture ON): only an explicit
 * `false` (trimmed, any case) opts in to content capture.
 */
const FI_HIDE_INPUTS = "FI_HIDE_INPUTS";
const FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS";

export interface ClaudeAgentSDKInstrumentationConfig {
  /** Provider to create spans with. Defaults to the global provider at call time. */
  tracerProvider?: TracerProvider;
  /**
   * fi-core trace config. Unlike fi-core, `hideInputs` and `hideOutputs`
   * default to `true` here: prompts, tool inputs and tool outputs can contain
   * source code, file contents or secrets. Opt in with `false`, or with
   * `FI_HIDE_INPUTS=false` / `FI_HIDE_OUTPUTS=false`.
   */
  traceConfig?: TraceConfigOptions;
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
export function resolveContentPolicy(traceConfig?: TraceConfigOptions): ContentPolicy {
  return {
    hideInputs: traceConfig?.hideInputs ?? envHide(FI_HIDE_INPUTS),
    hideOutputs: traceConfig?.hideOutputs ?? envHide(FI_HIDE_OUTPUTS),
  };
}

/**
 * Providers passed to an instrumentation, for `shutdown()` with no argument.
 * Held weakly: an app that drops a provider and its instrumentation lets both
 * be collected. Dead entries are pruned whenever the set is read or added to.
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

/** Providers currently tracked for `shutdown()`. */
export function trackedProviderCount(): number {
  return liveProviders().length;
}

/**
 * Traces Claude Agent SDK `query()` calls.
 *
 * ```ts
 * import { query } from "@anthropic-ai/claude-agent-sdk";
 * const instrumentation = new ClaudeAgentSDKInstrumentation({ tracerProvider });
 * const tracedQuery = instrumentation.wrapQuery(query);
 * ```
 *
 * The SDK is ESM-only and its module namespace cannot be patched, so the
 * application calls the wrapped function instead of the original.
 */
export class ClaudeAgentSDKInstrumentation {
  private tracerProvider?: TracerProvider;
  private readonly traceConfig?: TraceConfigOptions;

  constructor(config: ClaudeAgentSDKInstrumentationConfig = {}) {
    this.traceConfig = config.traceConfig;
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
    return resolveContentPolicy(this.traceConfig);
  }

  /** Return a traced `query()` with the same signature as the original. */
  wrapQuery<Q extends QueryFunctionLike>(query: Q): Q {
    return wrapQueryFunction(query, () => this.tracingContext());
  }

  /** Flush the provider's span processors. Does not shut the provider down. */
  async forceFlush(): Promise<void> {
    await flushProvider(this.tracerProvider ?? trace.getTracerProvider());
  }

  /** Same as `forceFlush()`; named for short scripts (architecture R-10). */
  async shutdown(): Promise<void> {
    await this.forceFlush();
  }

  private tracingContext(): QueryTracingContext {
    const policy = this.contentPolicy;
    const provider = this.tracerProvider ?? trace.getTracerProvider();
    const tracer = new FITracer({
      tracer: provider.getTracer(INSTRUMENTATION_NAME, VERSION),
      traceConfig: { ...this.traceConfig, hideInputs: policy.hideInputs, hideOutputs: policy.hideOutputs },
    });
    return { tracer, policy };
  }
}

/** Python-style convenience constructor (`instrument_claude_agent_sdk`). */
export function instrumentClaudeAgentSDK(
  config: ClaudeAgentSDKInstrumentationConfig = {},
): ClaudeAgentSDKInstrumentation {
  return new ClaudeAgentSDKInstrumentation(config);
}

/** Wrap `query()` in one call. */
export function wrapQuery<Q extends QueryFunctionLike>(
  query: Q,
  config: ClaudeAgentSDKInstrumentationConfig = {},
): Q {
  return new ClaudeAgentSDKInstrumentation(config).wrapQuery(query);
}

/**
 * Flush spans before a short script exits. Flushes `tracerProvider` when
 * given; otherwise every provider passed to an instrumentation plus the global
 * provider. Export errors are logged, never thrown: export failure does not
 * fail the agent.
 */
export async function shutdown(tracerProvider?: TracerProvider): Promise<void> {
  const providers = tracerProvider
    ? [tracerProvider]
    : [...liveProviders(), trace.getTracerProvider()];
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
  if (typeof target.forceFlush !== "function") {
    return;
  }
  try {
    await target.forceFlush();
  } catch (error) {
    diag.warn(`${INSTRUMENTATION_NAME}: forceFlush failed: ${error}`);
  }
}
