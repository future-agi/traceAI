import { diag } from "@opentelemetry/api";

type SignalListener = (signal: NodeJS.Signals) => unknown;

/** The parts of `process` used here; injectable for tests. */
export interface SignalTarget {
  readonly pid: number;
  listeners(event: NodeJS.Signals): Function[];
  on(event: NodeJS.Signals, listener: SignalListener): unknown;
  removeListener(event: NodeJS.Signals, listener: Function): unknown;
  kill(pid: number, signal?: NodeJS.Signals): unknown;
}

export interface FlushOnSignalsOptions {
  /** Default `["SIGTERM", "SIGINT"]`. */
  signals?: NodeJS.Signals[];
  /** Upper bound for `flush` before the earlier listeners run. Default 10000 ms. */
  timeoutMillis?: number;
  /** Default `process`. */
  target?: SignalTarget;
}

/**
 * Run `flush` first when the process receives a signal, then hand the signal
 * to the listeners that were installed before this call.
 *
 * genkit 1.42.0 installs a module-level SIGTERM/SIGINT listener that stops the
 * reflection servers and calls `process.exit(0)` (genkit/src/genkit.ts:786-793).
 * An app's own async SIGTERM listener that awaits `flushTracing()` loses that
 * race, so spans still in flight are dropped. This helper takes the listeners
 * registered so far (Genkit's, and Genkit's NodeSDK cleanup from
 * `enableTelemetry`), awaits `flush` (bounded, errors swallowed), then calls them
 * in their original order. With no earlier listener it re-raises the signal.
 *
 * Call it after `import "genkit"` and after `await enableTelemetry(...)`.
 * Returns a function that restores the original listeners.
 */
export function flushOnSignals(flush: () => Promise<unknown>, options: FlushOnSignalsOptions = {}): () => void {
  const target: SignalTarget = options.target ?? (process as unknown as SignalTarget);
  const signals = options.signals ?? ["SIGTERM", "SIGINT"];
  const timeoutMillis = options.timeoutMillis ?? 10_000;
  const installed: { signal: NodeJS.Signals; listener: SignalListener; previous: Function[] }[] = [];

  for (const signal of signals) {
    const previous = target.listeners(signal).slice();
    let handled = false;
    const listener: SignalListener = async (received) => {
      if (handled) return;
      handled = true;
      await boundedFlush(flush, timeoutMillis);
      target.removeListener(signal, listener);
      if (previous.length === 0) {
        target.kill(target.pid, signal);
        return;
      }
      for (const original of previous) {
        try {
          void (original as SignalListener).call(target, received ?? signal);
        } catch (error) {
          diag.warn(`flushOnSignals: ${signal} listener threw: ${error}`);
        }
      }
    };
    for (const original of previous) target.removeListener(signal, original);
    target.on(signal, listener);
    installed.push({ signal, listener, previous });
  }

  return () => {
    for (const { signal, listener, previous } of installed) {
      target.removeListener(signal, listener);
      const current = target.listeners(signal);
      for (const original of previous) {
        if (!current.includes(original)) target.on(signal, original as SignalListener);
      }
    }
  };
}

async function boundedFlush(flush: () => Promise<unknown>, timeoutMillis: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      Promise.resolve()
        .then(flush)
        .catch((error: unknown) => diag.warn(`flushOnSignals: flush failed: ${error}`)),
      new Promise<void>((resolve) => {
        timer = setTimeout(() => {
          diag.warn(`flushOnSignals: flush did not finish within ${timeoutMillis} ms`);
          resolve();
        }, timeoutMillis);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
