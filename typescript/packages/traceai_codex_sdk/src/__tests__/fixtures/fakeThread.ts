/**
 * A Thread with the same control flow as @openai/codex-sdk 0.162.1
 * (dist/index.js `Thread`): `run()` and `runStreamed()` both drive the private
 * `runStreamedInternal()` generator, `thread.started` sets the id, `run()`
 * breaks on `turn.failed` and throws its message. `sdkShape.test.ts` checks
 * the real SDK still has this shape.
 */
import { InputLike, ThreadEventLike, ThreadOptionsLike, TurnOptionsLike } from "../../types";

export type Script = (input: InputLike, turnOptions: TurnOptionsLike) => AsyncGenerator<ThreadEventLike>;

export function scripted(events: ThreadEventLike[], opts: { throwAfter?: unknown; delayMs?: number } = {}): Script {
  return async function* script(_input, turnOptions) {
    for (const event of events) {
      if (opts.delayMs) await new Promise((resolve) => setTimeout(resolve, opts.delayMs));
      if (turnOptions?.signal?.aborted) {
        const error = new Error("The operation was aborted");
        error.name = "AbortError";
        throw error;
      }
      yield JSON.parse(JSON.stringify(event));
    }
    if (opts.throwAfter !== undefined) throw opts.throwAfter;
  };
}

export class FakeThread {
  private _id: string | null;
  private _threadOptions: ThreadOptionsLike;
  readonly scripts: Script[];
  readonly calls: Array<{ input: InputLike; turnOptions: TurnOptionsLike }> = [];

  constructor(scripts: Script[], threadOptions: ThreadOptionsLike = {}, id: string | null = null) {
    this.scripts = scripts;
    this._threadOptions = threadOptions;
    this._id = id;
  }

  get id(): string | null {
    return this._id;
  }

  async runStreamed(input: InputLike, turnOptions: TurnOptionsLike = {}) {
    return { events: this.runStreamedInternal(input, turnOptions) };
  }

  async *runStreamedInternal(input: InputLike, turnOptions: TurnOptionsLike = {}): AsyncGenerator<ThreadEventLike> {
    this.calls.push({ input, turnOptions });
    const script = this.scripts.shift();
    if (!script) throw new Error("no script left");
    for await (const event of script(input, turnOptions)) {
      if (event.type === "thread.started" && event.thread_id) this._id = event.thread_id;
      yield event;
    }
  }

  async run(input: InputLike, turnOptions: TurnOptionsLike = {}) {
    const generator = this.runStreamedInternal(input, turnOptions);
    const items: unknown[] = [];
    let finalResponse = "";
    let usage: unknown = null;
    let turnFailure: { message?: string } | null = null;
    for await (const event of generator) {
      if (event.type === "item.completed") {
        if (event.item?.type === "agent_message") finalResponse = event.item.text ?? "";
        items.push(event.item);
      } else if (event.type === "turn.completed") {
        usage = event.usage;
      } else if (event.type === "turn.failed") {
        turnFailure = event.error ?? {};
        break;
      }
    }
    if (turnFailure) throw new Error(turnFailure.message);
    return { items, finalResponse, usage };
  }
}

/** A module-like object, as `import * as CodexSDK` would give. */
export function fakeModule() {
  class Thread extends FakeThread {}
  class Codex {
    readonly started: Thread[] = [];
    constructor(private readonly scripts: Script[]) {}
    startThread(options?: ThreadOptionsLike) {
      const thread = new Thread(this.scripts, options ?? {});
      this.started.push(thread);
      return thread;
    }
    resumeThread(id: string, options?: ThreadOptionsLike) {
      return new Thread(this.scripts, options ?? {}, id);
    }
  }
  return { Thread, Codex };
}
