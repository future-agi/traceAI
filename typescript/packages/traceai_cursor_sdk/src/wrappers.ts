/**
 * Patch points.
 *
 * - `Agent.create` / `Agent.resume` (static, writable on the SDK's `Agent`
 *   class): the returned `SDKAgent` instance gets a traced `send`; a factory
 *   that throws is recorded as one ERROR span. `Agent.prompt()` calls
 *   `Agent.create()` then `send()` and `wait()` (1.0.37, 1.0.38), so it is traced
 *   through those without a wrapper of its own.
 * - `agent.send()` (instance): opens the run span and patches the returned
 *   `Run` instance's `stream()` and `wait()`, and subscribes to
 *   `onDidChangeStatus`.
 *
 * Nothing is wrapped in a Proxy: callers get the SDK's own agent and run
 * objects back, and `run.stream()` yields the SDK's own event objects
 * unchanged, forwarding `return()` / `throw()`. The local runtime's stream is a
 * single-consumer queue, so the wrapper never reads it on its own: tool spans
 * exist only for events the application reads through `run.stream()`.
 */
import { Context, Tracer, context as otelContext, diag } from "@opentelemetry/api";
import { SpanNames } from "./attributes";
import {
  ContentPolicy,
  RunTracer,
  clockMs,
  isTerminalStatus,
  recordFactoryError,
  runtimeOf,
} from "./spans";
import { AgentClassLike, AgentLike, AgentOptionsLike, RunLike, RunResultLike, SDKMessageLike, SendOptionsLike } from "./types";

const PATCHED = Symbol.for("@traceai/cursor-sdk/patched");
const LOG = "@traceai/cursor-sdk";

export interface TracingContext {
  tracer: Tracer;
  policy: ContentPolicy;
  /**
   * How long a run whose end was signalled without a `RunResult` (stream ended
   * before the status flipped, or the run finished before anyone streamed)
   * waits for `wait()` / a terminal status before its span is closed.
   */
  graceMs: number;
  /**
   * How long a run whose end is already known (wait() resolved, terminal
   * status) keeps its span open for a stream that is still being read. Stops
   * a partly read, never-closed stream from holding the span open forever.
   */
  drainMs: number;
  /** Runs whose span is still open, so `forceFlush()` / `shutdown()` can close them. */
  registry: Set<RunWatcher>;
}

export type GetContext = () => TracingContext | undefined;

export function isPatched(target: unknown): boolean {
  try {
    return !!target && (target as Record<symbol, unknown>)[PATCHED] === true;
  } catch {
    return false;
  }
}

function markPatched(target: object): boolean {
  try {
    Object.defineProperty(target, PATCHED, { value: true, configurable: true, enumerable: false });
    return true;
  } catch {
    return false;
  }
}

function defineMethod(target: object, name: string, value: unknown): boolean {
  try {
    Object.defineProperty(target, name, { value, writable: true, configurable: true, enumerable: false });
    return true;
  } catch (error) {
    diag.debug(`${LOG}: cannot patch ${name}: ${error}`);
    return false;
  }
}

function safe(fn: () => void): void {
  try {
    fn();
  } catch (error) {
    diag.debug(`${LOG}: ${error}`);
  }
}

/**
 * Patch the SDK's `Agent` class (`import { Agent } from "@cursor/sdk"`).
 * Returns an undo function, or `undefined` when it was already patched or
 * has nothing to patch.
 */
export function patchAgentClass(AgentClass: AgentClassLike, getContext: GetContext): (() => void) | undefined {
  if (!AgentClass || (typeof AgentClass !== "function" && typeof AgentClass !== "object") || isPatched(AgentClass)) {
    return undefined;
  }
  const target = AgentClass as unknown as Record<string, unknown>;
  const undo: Array<() => void> = [];
  for (const [method, spanName] of [
    ["create", SpanNames.CREATE],
    ["resume", SpanNames.RESUME],
  ] as const) {
    const original = target[method];
    if (typeof original !== "function") continue;
    const hadOwn = Object.prototype.hasOwnProperty.call(target, method);
    const traced = function tracedAgentFactory(this: unknown, ...args: unknown[]) {
      const ctx = getContext();
      if (!ctx) return (original as (...a: unknown[]) => unknown).apply(this, args);
      const startTimeMs = clockMs();
      const parentContext = otelContext.active();
      let pending: unknown;
      try {
        pending = (original as (...a: unknown[]) => unknown).apply(this, args);
      } catch (error) {
        factoryFailed(ctx, spanName, method, args, parentContext, startTimeMs, error);
        throw error;
      }
      return Promise.resolve(pending).then(
        (agent) => wrapAgent(agent as AgentLike, getContext),
        (error) => {
          factoryFailed(ctx, spanName, method, args, parentContext, startTimeMs, error);
          throw error;
        },
      );
    };
    if (!defineMethod(target, method, traced)) continue;
    undo.push(() => {
      if (hadOwn) defineMethod(target, method, original);
      else delete target[method];
    });
  }
  if (!undo.length) return undefined;
  markPatched(target);
  return () => {
    while (undo.length) safe(() => undo.pop()?.());
    safe(() => delete (target as Record<symbol, unknown>)[PATCHED]);
  };
}

function factoryFailed(
  ctx: TracingContext,
  spanName: string,
  method: "create" | "resume",
  args: unknown[],
  parentContext: Context,
  startTimeMs: number,
  error: unknown,
): void {
  safe(() => {
    let options: AgentOptionsLike | undefined;
    let agentId: string | undefined;
    if (method === "create") {
      options = args[0] as AgentOptionsLike | undefined;
      agentId = typeof options?.agentId === "string" ? options.agentId : undefined;
    } else {
      agentId = typeof args[0] === "string" ? (args[0] as string) : undefined;
      options = args[1] as AgentOptionsLike | undefined;
    }
    const runtime = options && typeof options === "object" && options.cloud ? "cloud" : options?.local ? "local" : runtimeOf(agentId);
    recordFactoryError({
      tracer: ctx.tracer,
      policy: ctx.policy,
      name: spanName,
      parentContext,
      startTimeMs,
      error,
      agentId,
      model: options && typeof options === "object" ? options.model : undefined,
      runtime,
    });
  });
}

/**
 * Patch one `SDKAgent` instance so each `send()` is traced. Returns the same
 * instance. A no-op for anything without a `send` method.
 */
export function wrapAgent<T>(agent: T, getContext: GetContext): T {
  const target = agent as unknown as AgentLike;
  if (!target || typeof target !== "object" || isPatched(target)) return agent;
  const original = target.send;
  if (typeof original !== "function") return agent;
  const traced = function tracedSend(this: AgentLike, ...args: unknown[]) {
    const ctx = getContext();
    if (!ctx) return original.apply(this, args as Parameters<NonNullable<AgentLike["send"]>>);
    let tracer: RunTracer | undefined;
    try {
      tracer = new RunTracer({
        tracer: ctx.tracer,
        policy: ctx.policy,
        parentContext: otelContext.active(),
        agent: this ?? target,
        message: args[0],
        sendOptions: args[1] as SendOptionsLike | undefined,
        startTimeMs: clockMs(),
      });
    } catch (error) {
      diag.debug(`${LOG}: tracing disabled for this run: ${error}`);
    }
    if (!tracer) return original.apply(this, args as Parameters<NonNullable<AgentLike["send"]>>);
    const runTracer = tracer;
    let pending: Promise<RunLike>;
    try {
      pending = original.apply(this, args as Parameters<NonNullable<AgentLike["send"]>>);
    } catch (error) {
      safe(() => runTracer.finish({ kind: "error", error }));
      throw error;
    }
    return Promise.resolve(pending).then(
      (run) => {
        safe(() => watchRun(run, runTracer, ctx));
        return run;
      },
      (error) => {
        safe(() => runTracer.finish({ kind: "error", error }));
        throw error;
      },
    );
  };
  if (!defineMethod(target, "send", traced)) return agent;
  markPatched(target);
  return agent;
}

/**
 * Decides when a run span closes. The span closes at the first of:
 * `wait()` resolving or throwing, the stream throwing, the stream ending
 * cleanly (the run ended with it), or a terminal status from
 * `onDidChangeStatus`. While the application is reading the stream, closing
 * is deferred to the stream's end so buffered tool events are not lost.
 */
export class RunWatcher {
  private activeStreams = 0;
  private streamed = false;
  private pendingResult?: RunResultLike;
  private pendingError?: { error: unknown };
  private terminalSeen = false;
  private streamEnded = false;
  private graceTimer?: ReturnType<typeof setTimeout>;
  private drainTimer?: ReturnType<typeof setTimeout>;
  private unsubscribe?: () => void;

  constructor(
    private readonly run: RunLike,
    private readonly tracer: RunTracer,
    private readonly ctx: TracingContext,
  ) {
    ctx.registry.add(this);
    tracer.attachRun(run);
    this.subscribe();
  }

  get finished(): boolean {
    return this.tracer.isFinished;
  }

  /** True while the run's end is known but the span waits for `wait()` or a status. */
  get inGrace(): boolean {
    return this.graceTimer !== undefined;
  }

  /** True once the run's end is known: wait() settled, a terminal status, or the stream ended cleanly. */
  get outcomeKnown(): boolean {
    return (
      !!this.pendingResult ||
      !!this.pendingError ||
      this.terminalSeen ||
      this.streamEnded ||
      !!this.tracer.statusFromStream ||
      this.handleTerminal()
    );
  }

  /**
   * Close now with what is known. Used by `forceFlush()` (runs whose end is
   * known) and `shutdown()` (every open run). A stream still being read is
   * detached first: its open tool spans end as incomplete.
   */
  flush(): void {
    if (this.finished) return;
    if (this.activeStreams > 0) {
      this.activeStreams = 0;
      this.tracer.detach();
    }
    if (this.pendingError) this.finish({ kind: "error", error: this.pendingError.error });
    else if (this.pendingResult) this.finish({ kind: "result", result: this.pendingResult });
    else if (this.outcomeKnown) this.finish({ kind: "status" });
    else this.finish({ kind: "detached" });
  }

  onStreamStart(): void {
    this.activeStreams += 1;
    this.streamed = true;
    this.clearGrace();
    // Started after the run ended (within the grace period): bound how long it may stay open.
    if (this.terminalSeen || this.pendingResult || this.pendingError) this.startDrain();
  }

  onStreamEvent(event: SDKMessageLike): void {
    this.tracer.onEvent(event);
  }

  /** The stream ended cleanly: the run ended. */
  onStreamDone(): void {
    this.activeStreams = Math.max(0, this.activeStreams - 1);
    this.streamEnded = true;
    if (this.activeStreams > 0) return;
    if (this.pendingError || this.pendingResult || this.handleTerminal() || this.tracer.statusFromStream) {
      this.terminalSeen = true;
      this.tryFinish();
    } else {
      // The run handle may flip to terminal a moment after the queue closes.
      this.startGrace();
    }
  }

  onStreamError(error: unknown): void {
    this.activeStreams = Math.max(0, this.activeStreams - 1);
    this.finish({ kind: "error", error });
  }

  /** The consumer stopped reading (`break`, `return()`). */
  onStreamReturn(): void {
    this.activeStreams = Math.max(0, this.activeStreams - 1);
    if (this.activeStreams > 0) return;
    this.tracer.detach();
    if (this.pendingError || this.pendingResult || this.handleTerminal() || this.tracer.statusFromStream) {
      this.terminalSeen = true;
      this.tryFinish();
    } else if (!this.unsubscribe) {
      // No way to observe the end: close now, status from the handle ("running").
      this.finish({ kind: "detached" });
    }
    // Otherwise the status listener or wait() closes it.
  }

  onWaitResult(result: RunResultLike): void {
    this.pendingResult = result;
    this.tryFinish();
  }

  onWaitError(error: unknown): void {
    this.pendingError = { error };
    this.tryFinish();
  }

  /**
   * The run's end is known but a stream is still open (read partly and never
   * closed, or still draining). Close the span after `drainMs` anyway.
   */
  private startDrain(): void {
    if (this.drainTimer || this.finished || this.activeStreams === 0) return;
    const timer = setTimeout(() => {
      this.drainTimer = undefined;
      if (!this.finished) this.flush();
    }, this.ctx.drainMs);
    (timer as { unref?: () => void }).unref?.();
    this.drainTimer = timer;
  }

  private subscribe(): void {
    const subscribe = this.run.onDidChangeStatus;
    if (typeof subscribe !== "function") return;
    try {
      const dispose = subscribe.call(this.run, (status) => {
        safe(() => {
          if (!isTerminalStatus(status) || this.finished) return;
          this.terminalSeen = true;
          if (this.activeStreams > 0) {
            this.startDrain();
            return;
          }
          if (!this.streamed && !this.pendingResult && !this.pendingError) {
            // Finished before anyone read the stream: give the app a moment to start reading it or call wait().
            this.startGrace();
            return;
          }
          this.tryFinish();
        });
      });
      if (typeof dispose === "function") this.unsubscribe = dispose;
      else this.unsubscribe = () => undefined;
    } catch (error) {
      diag.debug(`${LOG}: onDidChangeStatus failed: ${error}`);
    }
  }

  private handleTerminal(): boolean {
    try {
      return isTerminalStatus(this.run.status);
    } catch {
      return false;
    }
  }

  private tryFinish(): void {
    if (this.finished) return;
    if (this.activeStreams > 0) {
      if (this.pendingError || this.pendingResult || this.terminalSeen) this.startDrain();
      return;
    }
    if (this.pendingError) this.finish({ kind: "error", error: this.pendingError.error });
    else if (this.pendingResult) this.finish({ kind: "result", result: this.pendingResult });
    else if (this.terminalSeen || this.handleTerminal()) this.finish({ kind: "status" });
  }

  private startGrace(): void {
    if (this.graceTimer || this.finished) return;
    const timer = setTimeout(() => {
      this.graceTimer = undefined;
      if (this.finished) return;
      if (this.activeStreams > 0) {
        this.startDrain();
        return;
      }
      this.finish(this.pendingResult ? { kind: "result", result: this.pendingResult } : { kind: "status" });
    }, this.ctx.graceMs);
    (timer as { unref?: () => void }).unref?.();
    this.graceTimer = timer;
  }

  private clearGrace(): void {
    if (this.graceTimer) clearTimeout(this.graceTimer);
    this.graceTimer = undefined;
  }

  private finish(outcome: Parameters<RunTracer["finish"]>[0]): void {
    this.clearGrace();
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.drainTimer = undefined;
    this.tracer.finish(outcome, this.run);
    this.ctx.registry.delete(this);
    const unsubscribe = this.unsubscribe;
    this.unsubscribe = undefined;
    if (unsubscribe) safe(unsubscribe);
  }
}

/** Patch one `Run` instance's `stream()` and `wait()` and watch its status. */
export function watchRun(run: RunLike, tracer: RunTracer, ctx: TracingContext): void {
  if (!run || typeof run !== "object") {
    tracer.finish({ kind: "status" });
    return;
  }
  const watcher = new RunWatcher(run, tracer, ctx);
  const target = run as unknown as Record<string, unknown>;

  const originalStream = run.stream;
  if (typeof originalStream === "function") {
    defineMethod(target, "stream", function tracedStream(this: RunLike, ...args: unknown[]) {
      const source = (originalStream as (...a: unknown[]) => AsyncGenerator<SDKMessageLike, void>).apply(this, args);
      return traceStream(source, watcher);
    });
  }
  const originalWait = run.wait;
  if (typeof originalWait === "function") {
    defineMethod(target, "wait", function tracedWait(this: RunLike, ...args: unknown[]) {
      const pending = (originalWait as (...a: unknown[]) => Promise<RunResultLike>).apply(this, args);
      return Promise.resolve(pending).then(
        (result) => {
          safe(() => watcher.onWaitResult(result));
          return result;
        },
        (error) => {
          safe(() => watcher.onWaitError(error));
          throw error;
        },
      );
    });
  }
  markPatched(target);
}

/**
 * Return a generator that yields exactly what `source` yields and feeds the
 * watcher. A tracing failure never reaches the caller.
 */
export function traceStream(
  source: AsyncGenerator<SDKMessageLike, void>,
  watcher: RunWatcher,
): AsyncGenerator<SDKMessageLike, void> {
  if (!source || typeof source.next !== "function") return source;
  let started = false;
  let ended = false;
  const start = () => {
    if (started) return;
    started = true;
    safe(() => watcher.onStreamStart());
  };
  const end = (fn: () => void) => {
    if (ended || !started) return;
    ended = true;
    safe(fn);
  };

  const proxy = {
    async next(...args: [] | [unknown]) {
      start();
      let result: IteratorResult<SDKMessageLike, void>;
      try {
        result = await source.next(...(args as []));
      } catch (error) {
        end(() => watcher.onStreamError(error));
        throw error;
      }
      if (result.done) end(() => watcher.onStreamDone());
      else if (!ended) safe(() => watcher.onStreamEvent(result.value));
      return result;
    },
    async return(value?: unknown) {
      try {
        return await source.return(value as never);
      } finally {
        end(() => watcher.onStreamReturn());
      }
    },
    async throw(error?: unknown) {
      start();
      let result: IteratorResult<SDKMessageLike, void>;
      try {
        result = await source.throw(error);
      } catch (thrown) {
        end(() => watcher.onStreamError(thrown));
        throw thrown;
      }
      if (result.done) end(() => watcher.onStreamReturn());
      else if (!ended) safe(() => watcher.onStreamEvent(result.value));
      return result;
    },
    [Symbol.asyncIterator]() {
      return proxy;
    },
  } as unknown as AsyncGenerator<SDKMessageLike, void>;
  const asyncDispose = (Symbol as unknown as { asyncDispose?: symbol }).asyncDispose;
  if (asyncDispose) {
    (proxy as unknown as Record<symbol, unknown>)[asyncDispose] = async () => {
      await proxy.return(undefined);
    };
  }
  return proxy;
}
