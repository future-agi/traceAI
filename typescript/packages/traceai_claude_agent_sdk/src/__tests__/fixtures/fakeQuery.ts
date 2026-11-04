/**
 * A fake `query()` with the SDK's signature. It yields recorded SDK-typed
 * messages and never starts the Claude Code CLI or calls the Anthropic API.
 */
import type { Options, Query, SDKMessage, query } from "@anthropic-ai/claude-agent-sdk";

export interface FakeQueryControl {
  /** Every params object the fake received, by reference. */
  calls: { prompt: unknown; options?: Options }[];
  interruptCalls: number;
  closeCalls: number;
  disposeCalls: number;
  backgroundTasksCalls: (string | undefined)[];
}

export interface FakeQueryBehavior {
  /** Throw `error` instead of yielding message `throwAt`. */
  throwAt?: number;
  error?: unknown;
  /** Before yielding message `waitForAbortAt`, block until options.abortController aborts. */
  waitForAbortAt?: number;
  /** Wait this long before yielding each message, so span times are ordered. */
  delayMs?: number;
  /** What `backgroundTasks()` resolves to (or rejects with, when an Error). */
  backgroundTasksResult?: boolean | Error;
}

export class FakeAbortError extends Error {
  constructor() {
    super("Claude Code process aborted by user");
    this.name = "AbortError";
  }
}

export function makeFakeQuery(
  messages: SDKMessage[],
  behavior: FakeQueryBehavior = {},
): { query: typeof query; control: FakeQueryControl } {
  const control: FakeQueryControl = {
    calls: [],
    interruptCalls: 0,
    closeCalls: 0,
    disposeCalls: 0,
    backgroundTasksCalls: [],
  };

  const fake = (params: { prompt: string | AsyncIterable<unknown>; options?: Options }): Query => {
    control.calls.push(params);
    const signal = params.options?.abortController?.signal;

    async function* generate(): AsyncGenerator<SDKMessage, void> {
      for (let index = 0; index < messages.length; index += 1) {
        if (behavior.delayMs) {
          await new Promise((resolve) => setTimeout(resolve, behavior.delayMs));
        }
        if (behavior.throwAt === index) {
          throw behavior.error ?? new Error("fake query failed");
        }
        if (behavior.waitForAbortAt === index) {
          await new Promise<void>((_, reject) => {
            if (!signal) {
              reject(new Error("waitForAbortAt needs options.abortController"));
              return;
            }
            if (signal.aborted) {
              reject(new FakeAbortError());
              return;
            }
            signal.addEventListener("abort", () => reject(new FakeAbortError()), { once: true });
          });
        }
        yield messages[index];
      }
    }

    const generator = generate();
    const asyncDispose = (Symbol as unknown as { asyncDispose?: symbol }).asyncDispose ?? Symbol.for("Symbol.asyncDispose");
    const controlMethods = {
      interrupt: async () => {
        control.interruptCalls += 1;
        return undefined;
      },
      setPermissionMode: async () => undefined,
      setModel: async () => undefined,
      backgroundTasks: async (toolUseId?: string) => {
        control.backgroundTasksCalls.push(toolUseId);
        const result = behavior.backgroundTasksResult ?? true;
        if (result instanceof Error) throw result;
        return result;
      },
      // Like the SDK: close() and asyncDispose stop the stream; no further messages.
      close: () => {
        control.closeCalls += 1;
        void generator.return(undefined);
      },
      [asyncDispose]: async () => {
        control.disposeCalls += 1;
        await generator.return(undefined);
      },
    };
    return Object.assign(generator, controlMethods) as unknown as Query;
  };

  return { query: fake as unknown as typeof query, control };
}
