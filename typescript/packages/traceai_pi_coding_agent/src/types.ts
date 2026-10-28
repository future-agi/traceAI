/**
 * Structural types for the parts of the Pi Coding Agent SDK this package reads.
 *
 * Shapes follow `@earendil-works/pi-coding-agent` 1.2.0
 * (`dist/core/agent-session.d.ts`), `@earendil-works/pi-agent-core`
 * (`AgentEvent`) and `@earendil-works/pi-ai` (`AssistantMessage`, `Usage`).
 * They are declared here instead of imported so the package has no runtime or
 * type dependency on the SDK: any session object with `sessionId` and
 * `subscribe()` works, and fields the SDK adds later are ignored.
 */

/** One event from `AgentSession.subscribe()`. Only `type` is guaranteed. */
export interface PiSessionEvent {
  type: string;
  [key: string]: unknown;
}

export type PiSessionEventListener = (event: PiSessionEvent) => void;

/** The `AgentSession` surface this package uses. */
export interface PiSessionLike {
  readonly sessionId: string;
  /** User-chosen display name. Exported only when inputs are shown. */
  readonly sessionName?: string;
  subscribe(listener: PiSessionEventListener): () => void;
  dispose?: () => void;
  /**
   * Present from Pi 0.80.5, the release that added `agent_settled`. Only its
   * presence is checked (never called): it tells the tracer that runs end at
   * `agent_settled`.
   */
  waitForIdle?: () => Promise<void>;
}

/** The `createAgentSession()` result surface this package uses. */
export interface CreateAgentSessionResultLike {
  session: PiSessionLike;
}

export type CreateAgentSessionLike = (...args: any[]) => Promise<CreateAgentSessionResultLike>;

/** `@earendil-works/pi-ai` `Usage`. */
export interface PiUsage {
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  reasoning?: number;
  totalTokens?: number;
  cost?: { total?: number };
}

export interface PiContentPart {
  type?: string;
  text?: string;
  name?: string;
  id?: string;
  arguments?: unknown;
}

/** Any transcript message (`AgentMessage`). Pi emits system, user, assistant, toolResult and custom roles. */
export interface PiMessage {
  role?: string;
  content?: string | PiContentPart[];
  timestamp?: number;
  // assistant
  api?: string;
  provider?: string;
  model?: string;
  responseModel?: string;
  responseId?: string;
  thinkingLevel?: string;
  usage?: PiUsage;
  stopReason?: string;
  errorMessage?: string;
  rawStopReason?: string;
  /** Errors Pi recorded while producing the message; only `error.name` is read. */
  diagnostics?: Array<{ type?: string; error?: { name?: string } }>;
  durationMs?: number;
  // toolResult
  toolCallId?: string;
  toolName?: string;
  isError?: boolean;
}

/** `compaction_end.result` (`CompactionResult`). `summary` is model-written text: content. */
export interface PiCompactionResult {
  summary?: string;
  tokensBefore?: number;
  estimatedTokensAfter?: number;
  usage?: PiUsage;
}
