/**
 * A stand-in for `@cursor/sdk` 1.0.37 with the same public shapes
 * (`dist/esm/stubs.d.ts`, `agent.d.ts`, `run.d.ts`, `messages.d.ts`) and the
 * same control flow as the real local runtime:
 *
 * - `Agent.prompt()` calls `Agent.create()` through the class, then
 *   `send()`, `wait()` and `[Symbol.asyncDispose]()`;
 * - `run.stream()` is a single-consumer queue (`async *stream()` over a
 *   shifted array), closed when the run ends;
 * - the run's status flips through `onDidChangeStatus` listeners;
 * - `wait()` resolves with a `RunResult` built from the handle once terminal.
 */
import type { RunResultLike, SDKMessageLike, TokenUsageLike } from "../../types";

export class FakeAuthenticationError extends Error {
  readonly code = "error";
  readonly status = 401;
  readonly isRetryable = false;
  readonly operation: string;
  constructor(message: string, operation: string) {
    super(message);
    this.name = "AuthenticationError";
    this.operation = operation;
  }
}

export type Step =
  | { event: SDKMessageLike; delayMs?: number }
  | { finish: "finished" | "error" | "cancelled"; result?: string; error?: { message: string; code?: string }; usage?: TokenUsageLike; delayMs?: number }
  | { throw: Error; delayMs?: number };

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Single-consumer event queue, like the SDK's run event buffer. */
class EventQueue {
  private readonly queue: SDKMessageLike[] = [];
  private readonly waiters: Array<(r: { done: boolean; value?: SDKMessageLike; error?: Error }) => void> = [];
  private closed = false;
  private failure?: Error;

  push(event: SDKMessageLike): void {
    const waiter = this.waiters.shift();
    if (waiter) waiter({ done: false, value: event });
    else this.queue.push(event);
  }

  close(error?: Error): void {
    this.closed = true;
    this.failure = error;
    for (const waiter of this.waiters.splice(0)) waiter({ done: true, error });
  }

  async *stream(): AsyncGenerator<SDKMessageLike, void> {
    for (;;) {
      const next = this.queue.shift();
      if (next) {
        yield next;
        continue;
      }
      if (this.closed) {
        if (this.failure) throw this.failure;
        return;
      }
      const r = await new Promise<{ done: boolean; value?: SDKMessageLike; error?: Error }>((resolve) => this.waiters.push(resolve));
      if (r.done) {
        if (r.error) throw r.error;
        return;
      }
      yield r.value as SDKMessageLike;
    }
  }
}

export interface FakeRunOptions {
  /** Omit `onDidChangeStatus` (an SDK without status listeners). */
  noStatusListener?: boolean;
  /** Flip the status this many ms after the queue closes (models the real ordering race). */
  statusLagMs?: number;
  /** Do not set `usage` on the handle at the end (only usage events carry tokens). */
  noHandleUsage?: boolean;
}

export class FakeRun {
  readonly id: string;
  readonly agentId: string;
  readonly requestId = "req-1";
  readonly createdAt = Date.now();
  model?: { id: string; params?: Array<{ id: string; value: string }> };
  result?: string;
  error?: { message: string; code?: string };
  durationMs?: number;
  usage?: TokenUsageLike;
  private currentStatus = "running";
  private readonly listeners = new Set<(status: string) => void>();
  private readonly buffer = new EventQueue();
  private readonly done: Promise<void>;
  readonly onDidChangeStatus?: (listener: (status: string) => void) => () => void;

  constructor(agentId: string, runId: string, model: FakeRun["model"], steps: Step[], private readonly options: FakeRunOptions = {}) {
    this.id = runId;
    this.agentId = agentId;
    this.model = model;
    if (!options.noStatusListener) {
      this.onDidChangeStatus = (listener) => {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
      };
    }
    this.done = this.play(steps);
    this.done.catch(() => undefined);
  }

  get status(): string {
    return this.currentStatus;
  }

  supports(): boolean {
    return true;
  }

  stream(): AsyncGenerator<SDKMessageLike, void> {
    return this.buffer.stream();
  }

  async wait(): Promise<RunResultLike> {
    await this.done.catch(() => undefined);
    while (this.currentStatus === "running") await sleep(1);
    return {
      id: this.id,
      requestId: this.requestId,
      status: this.currentStatus,
      ...(this.result !== undefined ? { result: this.result } : {}),
      ...(this.currentStatus === "error" && this.error ? { error: this.error } : {}),
      ...(this.model ? { model: this.model } : {}),
      ...(this.durationMs !== undefined ? { durationMs: this.durationMs } : {}),
      ...(this.usage ? { usage: this.usage } : {}),
    };
  }

  async cancel(): Promise<void> {
    this.setStatus("cancelled");
  }

  async conversation(): Promise<unknown[]> {
    return [];
  }

  private setStatus(status: string): void {
    this.currentStatus = status;
    for (const listener of [...this.listeners]) listener(status);
  }

  private async play(steps: Step[]): Promise<void> {
    const started = Date.now();
    for (const step of steps) {
      if (step.delayMs) await sleep(step.delayMs);
      else await Promise.resolve();
      if ("event" in step) {
        this.buffer.push(step.event);
      } else if ("throw" in step) {
        this.buffer.close(step.throw);
        this.setStatus("error");
        return;
      } else {
        this.result = step.result;
        this.error = step.error;
        this.durationMs = Date.now() - started;
        if (step.usage && !this.options.noHandleUsage) this.usage = step.usage;
        this.buffer.close();
        if (this.options.statusLagMs) await sleep(this.options.statusLagMs);
        this.setStatus(step.finish);
        return;
      }
    }
  }
}

export interface FakeAgentScript {
  /** Steps per send(), in order. */
  runs: Step[][];
  runOptions?: FakeRunOptions;
  /** Make send() reject with this error. */
  sendError?: Error;
}

let agentCounter = 0;

export class FakeAgent {
  readonly agentId: string;
  model?: { id: string; params?: Array<{ id: string; value: string }> };
  private sends = 0;
  disposed = false;

  constructor(agentId: string, model: FakeAgent["model"], private readonly script: FakeAgentScript) {
    this.agentId = agentId;
    this.model = model;
  }

  async send(message: string | { text: string }, options?: { model?: { id: string } }): Promise<FakeRun> {
    void message;
    if (this.script.sendError) throw this.script.sendError;
    if (options?.model) this.model = options.model;
    const steps = this.script.runs[this.sends] ?? this.script.runs[this.script.runs.length - 1] ?? [];
    this.sends += 1;
    return new FakeRun(this.agentId, `run-${this.sends}`, this.model, steps, this.script.runOptions);
  }

  close(): void {
    this.disposed = true;
  }

  async reload(): Promise<void> {}

  async [Symbol.asyncDispose](): Promise<void> {
    this.disposed = true;
  }
}

/** Builds a fresh `Agent` class per test, scripted like the real one. */
export function makeFakeSdk(script: FakeAgentScript, createError?: Error) {
  class Agent {
    private constructor() {}

    static async create(options: { apiKey?: string; model?: { id: string }; local?: { cwd?: string }; agentId?: string }): Promise<FakeAgent> {
      if (createError) throw createError;
      agentCounter += 1;
      const id = options.agentId ?? `agent-0000000${agentCounter}-aaaa-4bbb-8ccc-000000000000`;
      return new FakeAgent(id, options.model, script);
    }

    static async resume(agentId: string, options?: { model?: { id: string } }): Promise<FakeAgent> {
      if (createError) throw createError;
      return new FakeAgent(agentId, options?.model, script);
    }

    /** Same body as 1.0.37: `const e = await De.create(r ?? {}); ... e.send(t) ... r.wait() ... asyncDispose`. */
    static async prompt(message: string, options?: { model?: { id: string } }): Promise<RunResultLike> {
      const agent = await Agent.create(options ?? {});
      try {
        const run = await agent.send(message);
        return await run.wait();
      } finally {
        await agent[Symbol.asyncDispose]();
      }
    }

    static async list(): Promise<{ items: unknown[] }> {
      return { items: [] };
    }
  }
  return { Agent };
}
