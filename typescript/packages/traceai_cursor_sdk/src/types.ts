/**
 * Structural copies of the `@cursor/sdk` types this package reads
 * (dist/esm/*.d.ts of 1.0.37 and 1.0.38). The SDK is a peer dependency, so nothing here
 * imports it: the package type-checks and runs whether or not it is installed.
 * `contract/sdk_types.ts` checks that the real SDK types fit these shapes.
 *
 * Fields are optional or `unknown` because the wrapper must never throw on a
 * shape a newer SDK adds.
 */

/** `ModelSelection` (`options.d.ts`). */
export interface ModelSelectionLike {
  id?: string;
  params?: Array<{ id?: string; value?: string }>;
}

/** `SDKUserMessage`: the object form `agent.send()` accepts. */
export interface UserMessageLike {
  text?: string;
  images?: unknown[];
}

/** `SendOptions` (`agent.d.ts`). Only the fields read here. */
export interface SendOptionsLike {
  model?: ModelSelectionLike;
  mode?: string;
  mcpServers?: Record<string, unknown>;
}

/** `AgentOptions` (`options.d.ts`). Only the fields read here; `apiKey` is never read. */
export interface AgentOptionsLike {
  model?: ModelSelectionLike;
  name?: string;
  mode?: string;
  agentId?: string;
  local?: { cwd?: string | string[] };
  cloud?: object;
}

/** `TokenUsage` (`usage-types.d.ts`). */
export interface TokenUsageLike {
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  totalTokens?: number;
  reasoningTokens?: number;
}

/** `RunError` (`run.d.ts`). */
export interface RunErrorLike {
  message?: string;
  code?: string;
}

/** `RunResult` (`run.d.ts`), resolved by `run.wait()` and `Agent.prompt()`. */
export interface RunResultLike {
  id?: string;
  requestId?: string;
  status?: string;
  result?: string;
  error?: RunErrorLike;
  model?: ModelSelectionLike;
  durationMs?: number;
  usage?: TokenUsageLike;
}

/** `RunStatus`: "running" | "finished" | "error" | "cancelled". */
export type RunStatusLike = string;

/** `SDKMessage` (`messages.d.ts`), flattened: every member's fields, all optional. */
export interface SDKMessageLike {
  type?: string;
  agent_id?: string;
  run_id?: string;
  /** system */
  subtype?: string;
  model?: ModelSelectionLike;
  tools?: string[];
  /** user, assistant: the message body; status: Cursor's diagnostic text. */
  message?: MessageBodyLike | string;
  /** tool_call */
  call_id?: string;
  name?: string;
  status?: string;
  args?: unknown;
  result?: unknown;
  truncated?: { args?: boolean; result?: boolean };
  /** thinking, task */
  text?: string;
  thinking_duration_ms?: number;
  /** request */
  request_id?: string;
  /** usage */
  usage?: TokenUsageLike;
}

/** `message` of `user` / `assistant` events. */
export interface MessageBodyLike {
  role?: string;
  content?: Array<ContentBlockLike>;
}

/** `TextBlock | ToolUseBlock`. */
export interface ContentBlockLike {
  type?: string;
  text?: string;
  id?: string;
  name?: string;
  input?: unknown;
}

/** `Run` (`run.d.ts`). The wrapper patches `stream` and `wait` on the instance. */
export interface RunLike {
  readonly id?: string;
  readonly requestId?: string;
  readonly agentId?: string;
  readonly status?: RunStatusLike;
  readonly result?: string;
  readonly error?: RunErrorLike;
  readonly model?: ModelSelectionLike;
  readonly durationMs?: number;
  readonly usage?: TokenUsageLike;
  stream?(): AsyncGenerator<SDKMessageLike, void>;
  wait?(): Promise<RunResultLike>;
  onDidChangeStatus?(listener: (status: RunStatusLike) => void): () => void;
}

/** `SDKAgent` (`agent.d.ts`). The wrapper patches `send` on the instance. */
export interface AgentLike {
  readonly agentId?: string;
  readonly model?: ModelSelectionLike;
  send?(message: string | UserMessageLike, options?: SendOptionsLike): Promise<RunLike>;
}

/**
 * The `Agent` class (`stubs.d.ts`). Its static factories are the patch points:
 * `create` and `resume` return an `SDKAgent`; `prompt` calls `Agent.create`
 * (then `send` and `wait`) internally, so it is traced through them.
 */
export interface AgentClassLike {
  create?(options: AgentOptionsLike): Promise<AgentLike>;
  resume?(agentId: string, options?: Partial<AgentOptionsLike>): Promise<AgentLike>;
  prompt?(message: string, options?: AgentOptionsLike): Promise<RunResultLike>;
}

/**
 * The SDK module namespace (`import * as CursorSDK from "@cursor/sdk"`) or
 * any object exposing the `Agent` class. Typed loosely on purpose: the SDK's
 * `Agent` has a private constructor, so it is not assignable to a class type.
 */
export interface CursorSDKModuleLike {
  // eslint-disable-next-line @typescript-eslint/ban-types
  Agent?: Function | AgentClassLike;
}
