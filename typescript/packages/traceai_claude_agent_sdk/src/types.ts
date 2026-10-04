/**
 * Structural types for the parts of the Claude Agent SDK surface this package
 * reads. They are written from `sdk.d.ts` of `@anthropic-ai/claude-agent-sdk`
 * 0.3.289 (and checked against 0.3.142) and deliberately do not import the
 * SDK, so the SDK stays a peer dependency with no type or runtime import.
 *
 * Every field is optional: the wrapper reads defensively and never fails the
 * agent because a field moved.
 */

/** A content block inside `BetaMessage.content` or `MessageParam.content`. */
export interface ContentBlockLike {
  type?: string;
  text?: string;
  /** tool_use / server_tool_use / mcp_tool_use */
  id?: string;
  name?: string;
  input?: unknown;
  /** mcp_tool_use */
  server_name?: string;
  /** tool_result */
  tool_use_id?: string;
  content?: unknown;
  is_error?: boolean;
}

/** `SDKAssistantMessage` (type: 'assistant'). */
export interface AssistantMessageLike {
  type: "assistant";
  message?: {
    id?: string;
    model?: string;
    content?: ContentBlockLike[] | string;
    stop_reason?: string | null;
  };
  parent_tool_use_id?: string | null;
  error?: string;
  session_id?: string;
}

/** `SDKUserMessage` / `SDKUserMessageReplay` (type: 'user'). */
export interface UserMessageLike {
  type: "user";
  message?: {
    role?: string;
    content?: ContentBlockLike[] | string;
  };
  parent_tool_use_id?: string | null;
  session_id?: string;
}

/** `SDKResultSuccess` / `SDKResultError` (type: 'result'). */
export interface ResultMessageLike {
  type: "result";
  subtype?: string;
  duration_ms?: number;
  duration_api_ms?: number;
  ttft_ms?: number;
  is_error?: boolean;
  num_turns?: number;
  result?: string;
  total_cost_usd?: number;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_input_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
  };
  errors?: string[];
  session_id?: string;
}

/** `SDKSystemMessage` and the other `type: 'system'` messages. */
export interface SystemMessageLike {
  type: "system";
  subtype?: string;
  session_id?: string;
  /** init */
  model?: string;
  permissionMode?: string;
  mcp_servers?: { name: string; status?: string }[];
  /** task_started / task_notification / task_progress / task_updated */
  task_id?: string;
  tool_use_id?: string;
  status?: string;
  subagent_type?: string;
  is_backgrounded?: boolean;
  /** task_updated: the TaskState fields that changed (sdk.d.ts SDKTaskUpdatedMessage). */
  patch?: { is_backgrounded?: boolean; status?: string };
}

export type SDKMessageLike =
  | AssistantMessageLike
  | UserMessageLike
  | ResultMessageLike
  | SystemMessageLike
  | { type: string; [key: string]: unknown };

/** The parts of `AbortSignal` the wrapper uses (structural, no DOM/Node lib needed). */
export interface AbortSignalLike {
  readonly aborted: boolean;
  readonly reason?: unknown;
  addEventListener(type: "abort", listener: () => void): void;
  removeEventListener(type: "abort", listener: () => void): void;
}

/** The subset of `Options` the wrapper reads. It never writes to options. */
export interface OptionsLike {
  abortController?: { readonly signal: AbortSignalLike };
  env?: { [envVar: string]: string | undefined };
  model?: string;
  permissionMode?: string;
  allowedTools?: string[];
  systemPrompt?: unknown;
  resume?: string;
  forkSession?: boolean;
  sessionId?: string;
  mcpServers?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface QueryParamsLike {
  prompt: string | AsyncIterable<unknown>;
  options?: OptionsLike;
}

/**
 * The `query()` function shape. The SDK's `Query` extends
 * `AsyncGenerator<SDKMessage, void>` and adds control methods
 * (`interrupt()`, `setModel()`, ...); the wrapper forwards those untouched.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type QueryFunctionLike = (params: any) => AsyncGenerator<any, any, any>;
