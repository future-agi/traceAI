/**
 * Public entry points: an instrumentation object that wraps `query()`, and a
 * `shutdown()` that flushes the tracer provider before a short script exits.
 */
import { TracerProvider, diag, trace } from "@opentelemetry/api";
import {
  DEFAULT_HIDE_INPUTS,
  DEFAULT_HIDE_INPUT_MESSAGES,
  DEFAULT_HIDE_INPUT_TEXT,
  DEFAULT_HIDE_OUTPUTS,
  DEFAULT_HIDE_OUTPUT_MESSAGES,
  DEFAULT_HIDE_OUTPUT_TEXT,
  FITracer,
  FI_HIDE_INPUTS,
  FI_HIDE_INPUT_MESSAGES,
  FI_HIDE_INPUT_TEXT,
  FI_HIDE_OUTPUTS,
  FI_HIDE_OUTPUT_MESSAGES,
  FI_HIDE_OUTPUT_TEXT,
  TraceConfigOptions,
} from "@traceai/fi-core";
import { ContentPolicy } from "./spans";
import { QueryTracingContext, wrapQueryFunction } from "./queryWrapper";
import { QueryFunctionLike } from "./types";
import { VERSION } from "./version";

export const INSTRUMENTATION_NAME = "@traceai/claude-agent-sdk";

export interface ClaudeAgentSDKInstrumentationConfig {
  /** Provider to create spans with. Defaults to the global provider at call time. */
  tracerProvider?: TracerProvider;
  /**
   * fi-core trace config, same defaults as every traceAI instrumentor: content
   * is captured unless you hide it. `hideInputs` / `hideOutputs` (or
   * `FI_HIDE_INPUTS=true` / `FI_HIDE_OUTPUTS=true`) hide every input / output
   * value; `hideInputMessages` / `hideInputText` and `hideOutputMessages` /
   * `hideOutputText` hide the prompt-side and assistant-side text only.
   */
  traceConfig?: TraceConfigOptions;
}

/**
 * One fi-core boolean flag: option > env var > fi-core default, with fi-core's
 * own env rule (`value.toLowerCase() === "true"` hides). Same precedence as
 * fi-core `generateTraceConfig`, which is not exported; read here so the env
 * vars apply even when no `traceConfig` is passed (fi-core only reads them
 * when one is).
 */
function flag(option: boolean | undefined, envKey: string, fallback: boolean): boolean {
  if (option !== undefined) return option;
  const value = typeof process !== "undefined" ? process.env?.[envKey] : undefined;
  if (value !== undefined) return value.toLowerCase() === "true";
  return fallback;
}

/** The fi-core flags this package reads, resolved (option > env > default). */
export function resolveHideFlags(traceConfig?: TraceConfigOptions): Required<
  Pick<
    TraceConfigOptions,
    "hideInputs" | "hideOutputs" | "hideInputMessages" | "hideOutputMessages" | "hideInputText" | "hideOutputText"
  >
> {
  return {
    hideInputs: flag(traceConfig?.hideInputs, FI_HIDE_INPUTS, DEFAULT_HIDE_INPUTS),
    hideOutputs: flag(traceConfig?.hideOutputs, FI_HIDE_OUTPUTS, DEFAULT_HIDE_OUTPUTS),
    hideInputMessages: flag(traceConfig?.hideInputMessages, FI_HIDE_INPUT_MESSAGES, DEFAULT_HIDE_INPUT_MESSAGES),
    hideOutputMessages: flag(traceConfig?.hideOutputMessages, FI_HIDE_OUTPUT_MESSAGES, DEFAULT_HIDE_OUTPUT_MESSAGES),
    hideInputText: flag(traceConfig?.hideInputText, FI_HIDE_INPUT_TEXT, DEFAULT_HIDE_INPUT_TEXT),
    hideOutputText: flag(traceConfig?.hideOutputText, FI_HIDE_OUTPUT_TEXT, DEFAULT_HIDE_OUTPUT_TEXT),
  };
}

/**
 * Resolve content capture from the standard fi-core flags. Content is
 * captured by default (fi-core `DEFAULT_HIDE_* = false`, like every traceAI
 * instrumentor and the Python package).
 */
export function resolveContentPolicy(traceConfig?: TraceConfigOptions): ContentPolicy {
  const f = resolveHideFlags(traceConfig);
  return {
    hideInputs: f.hideInputs,
    hideOutputs: f.hideOutputs,
    hideInputMessages: f.hideInputs || f.hideInputMessages || f.hideInputText,
    hideOutputMessages: f.hideOutputs || f.hideOutputMessages || f.hideOutputText,
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
    // Always pass an object, so FITracer also reads the other fi-core env vars
    // (FI_PII_REDACTION, ...) when the app gave no traceConfig.
    const tracer = new FITracer({
      tracer: provider.getTracer(INSTRUMENTATION_NAME, VERSION),
      traceConfig: { ...(this.traceConfig ?? {}), ...resolveHideFlags(this.traceConfig) },
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
