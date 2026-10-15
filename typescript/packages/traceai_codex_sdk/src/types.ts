/**
 * Structural copies of the `@openai/codex-sdk` types this package reads
 * (dist/index.d.ts of 0.162.1). The SDK is a peer dependency, so nothing here
 * imports it: the package type-checks and runs whether or not it is installed.
 *
 * Every field is optional or `unknown` because the wrapper must never throw on
 * an event shape a newer SDK adds.
 */

/** `UserInput` / `Input` accepted by `Thread.run()` and `Thread.runStreamed()`. */
export type UserInputLike = { type?: string; text?: unknown; path?: unknown };
export type InputLike = string | UserInputLike[];

/** `TurnOptions`. Only `signal` is read (abort detection). */
export interface TurnOptionsLike {
  signal?: AbortSignalLike;
  outputSchema?: unknown;
}

export interface AbortSignalLike {
  aborted?: boolean;
  reason?: unknown;
}

/** `ThreadOptions`, kept on the private `_threadOptions` field of a Thread. */
export interface ThreadOptionsLike {
  model?: string;
  sandboxMode?: string;
  approvalPolicy?: string;
  modelReasoningEffort?: string;
  webSearchMode?: string;
  webSearchEnabled?: boolean;
  networkAccessEnabled?: boolean;
  skipGitRepoCheck?: boolean;
  workingDirectory?: string;
}

/** `Usage` on `turn.completed`. */
export interface UsageLike {
  input_tokens?: number;
  cached_input_tokens?: number;
  cache_write_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
}

/** Union of `ThreadItem` members, flattened. */
export interface ThreadItemLike {
  id?: string;
  type?: string;
  /** agent_message, reasoning, todo item */
  text?: string;
  /** command_execution */
  command?: string;
  aggregated_output?: string;
  exit_code?: number;
  /** command_execution, file_change, mcp_tool_call */
  status?: string;
  /** file_change */
  changes?: Array<{ path?: string; kind?: string }>;
  /** mcp_tool_call */
  server?: string;
  tool?: string;
  arguments?: unknown;
  result?: { content?: unknown; structured_content?: unknown };
  error?: { message?: string };
  /** web_search */
  query?: string;
  /** todo_list */
  items?: Array<{ text?: string; completed?: boolean }>;
  /** error item */
  message?: string;
}

/** `ThreadEvent`. */
export interface ThreadEventLike {
  type?: string;
  thread_id?: string;
  usage?: UsageLike;
  error?: { message?: string };
  item?: ThreadItemLike;
  message?: string;
}

/**
 * The parts of a `Thread` instance the wrapper touches. `runStreamedInternal`
 * is private in the SDK but is the one method both `run()` and `runStreamed()`
 * call (every stable release from 0.44.0 to 0.162.1); `id` and
 * `_threadOptions` are read for the session id and model.
 */
export interface ThreadLike {
  readonly id?: string | null;
  _threadOptions?: ThreadOptionsLike;
  runStreamedInternal?: (input: InputLike, turnOptions?: TurnOptionsLike) => AsyncGenerator<ThreadEventLike>;
  runStreamed?: (input: InputLike, turnOptions?: TurnOptionsLike) => Promise<{ events: AsyncGenerator<ThreadEventLike> }>;
  run?: (input: InputLike, turnOptions?: TurnOptionsLike) => Promise<unknown>;
}

/**
 * The SDK module namespace (`import * as CodexSDK from "@openai/codex-sdk"`)
 * or any object exposing the `Thread` class. Typed loosely on purpose: the
 * SDK's `Thread` declares its fields `private`, so it is not assignable to an
 * interface that names them.
 */
export interface CodexModuleLike {
  // eslint-disable-next-line @typescript-eslint/ban-types
  Thread?: Function;
}
