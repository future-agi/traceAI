/**
 * Recorded-style message sequences for the wrapper tests.
 *
 * Authored from the `@anthropic-ai/claude-agent-sdk` 0.3.289 `sdk.d.ts` types
 * (not from a live call). Every message is type-checked against the installed
 * SDK types, so a field rename in the pinned SDK breaks compilation here.
 */
import type {
  SDKAssistantMessage,
  SDKMessage,
  SDKResultError,
  SDKResultSuccess,
  SDKSystemMessage,
  SDKTaskNotificationMessage,
  SDKTaskStartedMessage,
  SDKTaskUpdatedMessage,
  SDKUserMessage,
} from "@anthropic-ai/claude-agent-sdk";

type UUID = `${string}-${string}-${string}-${string}-${string}`;
type BetaMessage = SDKAssistantMessage["message"];
type BetaContentBlock = BetaMessage["content"][number];
type UserContent = Exclude<SDKUserMessage["message"]["content"], string>;
type NonNullableUsage = SDKResultSuccess["usage"];

export const SESSION_ID = "11111111-2222-4333-8444-555555555555";
export const MODEL = "claude-sonnet-4-5";
export const PROMPT = "Summarize README.md. SECRET_PROMPT_MARKER";

let uuidCounter = 0;
function uuid(): UUID {
  uuidCounter += 1;
  const tail = uuidCounter.toString(16).padStart(12, "0");
  return `00000000-0000-4000-8000-${tail}` as UUID;
}

const betaUsage = (input: number, output: number): BetaMessage["usage"] => ({
  cache_creation: null,
  cache_creation_input_tokens: null,
  cache_read_input_tokens: null,
  fallback_credit: null,
  inference_geo: null,
  input_tokens: input,
  iterations: null,
  output_tokens: output,
  output_tokens_details: null,
  server_tool_use: null,
  service_tier: "standard",
  speed: null,
});

export function text(value: string): BetaContentBlock {
  return { type: "text", text: value, citations: null };
}

export function toolUse(id: string, name: string, input: unknown): BetaContentBlock {
  return { type: "tool_use", id, name, input };
}

export function assistant(
  messageId: string,
  content: BetaContentBlock[],
  parentToolUseId: string | null = null,
  extra: Partial<SDKAssistantMessage> = {},
): SDKAssistantMessage {
  return {
    type: "assistant",
    message: {
      id: messageId,
      type: "message",
      role: "assistant",
      model: MODEL,
      content,
      container: null,
      context_management: null,
      diagnostics: null,
      stop_details: null,
      stop_reason: "end_turn",
      stop_sequence: null,
      usage: betaUsage(10, 5),
    },
    parent_tool_use_id: parentToolUseId,
    uuid: uuid(),
    session_id: SESSION_ID,
    ...extra,
  };
}

export function toolResult(
  toolUseId: string,
  content: string,
  parentToolUseId: string | null = null,
  isError = false,
): SDKUserMessage {
  const blocks: UserContent = [{ type: "tool_result", tool_use_id: toolUseId, content, is_error: isError }];
  return {
    type: "user",
    message: { role: "user", content: blocks },
    parent_tool_use_id: parentToolUseId,
    uuid: uuid(),
    session_id: SESSION_ID,
  };
}

export function init(
  overrides: Partial<SDKSystemMessage> = {},
): SDKSystemMessage {
  return {
    type: "system",
    subtype: "init",
    apiKeySource: "ANTHROPIC_API_KEY",
    claude_code_version: "2.1.289",
    cwd: "/tmp/project",
    tools: ["Read", "Grep", "Agent"],
    mcp_servers: [],
    model: MODEL,
    permissionMode: "default",
    slash_commands: [],
    output_style: "default",
    skills: [],
    plugins: [],
    uuid: uuid(),
    session_id: SESSION_ID,
    ...overrides,
  };
}

const resultUsage: NonNullableUsage = {
  cache_creation: { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 0 },
  cache_creation_input_tokens: 7,
  cache_read_input_tokens: 11,
  fallback_credit: null,
  inference_geo: "us",
  input_tokens: 120,
  iterations: [],
  output_tokens: 45,
  output_tokens_details: { thinking_tokens: 0 },
  server_tool_use: { web_fetch_requests: 0, web_search_requests: 0 },
  service_tier: "standard",
  speed: "standard",
};

export const TOTAL_COST_USD = 0.0123;

export function resultSuccess(resultText: string, overrides: Partial<SDKResultSuccess> = {}): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 1500,
    duration_api_ms: 1200,
    ttft_ms: 300,
    is_error: false,
    num_turns: 2,
    result: resultText,
    stop_reason: "end_turn",
    total_cost_usd: TOTAL_COST_USD,
    usage: resultUsage,
    // modelUsage is the running total the wrapper reads (sdk.d.ts:5687); usage is main-loop only.
    modelUsage: { [MODEL]: modelUsage(120, 45, TOTAL_COST_USD, 11, 7) },
    permission_denials: [],
    uuid: uuid(),
    session_id: SESSION_ID,
    ...overrides,
  };
}

export function resultError(overrides: Partial<SDKResultError> = {}): SDKResultError {
  return {
    type: "result",
    subtype: "error_max_turns",
    duration_ms: 900,
    duration_api_ms: 800,
    is_error: true,
    num_turns: 3,
    stop_reason: null,
    total_cost_usd: 0.002,
    usage: resultUsage,
    modelUsage: { [MODEL]: modelUsage(120, 45, 0.002, 11, 7) },
    permission_denials: [],
    errors: ["Reached maximum number of turns (3)"],
    uuid: uuid(),
    session_id: SESSION_ID,
    ...overrides,
  };
}

export function taskStarted(toolUseId: string, overrides: Partial<SDKTaskStartedMessage> = {}): SDKTaskStartedMessage {
  return {
    type: "system",
    subtype: "task_started",
    task_id: "task-1",
    tool_use_id: toolUseId,
    description: "Review the diff",
    subagent_type: "code-reviewer",
    uuid: uuid(),
    session_id: SESSION_ID,
    ...overrides,
  };
}

export function taskNotification(
  toolUseId: string,
  status: SDKTaskNotificationMessage["status"] = "completed",
): SDKTaskNotificationMessage {
  return {
    type: "system",
    subtype: "task_notification",
    task_id: "task-1",
    tool_use_id: toolUseId,
    status,
    output_file: "/tmp/task-1.md",
    summary: "done",
    uuid: uuid(),
    session_id: SESSION_ID,
  };
}

// ----------------------------------------------------------------------------
// Journeys
// ----------------------------------------------------------------------------

/** `system/task_updated` (sdk.d.ts SDKTaskUpdatedMessage): carries task_id only, no tool_use_id. */
export function taskUpdated(taskId: string, patch: SDKTaskUpdatedMessage["patch"]): SDKTaskUpdatedMessage {
  return { type: "system", subtype: "task_updated", task_id: taskId, patch, uuid: uuid(), session_id: SESSION_ID };
}

export const TOOL_INPUT_MARKER = "SECRET_TOOL_INPUT_MARKER.md";
export const TOOL_OUTPUT_MARKER = "SECRET_TOOL_OUTPUT_MARKER";
export const ASSISTANT_TEXT_MARKER = "SECRET_ASSISTANT_TEXT_MARKER";

/** J1: one built-in tool call, then a final answer. The first API response is split over two SDK messages. */
export function simpleToolJourney(): SDKMessage[] {
  return [
    init(),
    assistant("msg_01", [text(`Reading the file. ${ASSISTANT_TEXT_MARKER}`)]),
    assistant("msg_01", [toolUse("toolu_read_1", "Read", { file_path: TOOL_INPUT_MARKER })]),
    toolResult("toolu_read_1", `# README ${TOOL_OUTPUT_MARKER}`),
    assistant("msg_02", [text(`The README describes traceAI. ${ASSISTANT_TEXT_MARKER}`)]),
    resultSuccess(`The README describes traceAI. ${ASSISTANT_TEXT_MARKER}`),
  ];
}

export const AGENT_TOOL_ID = "toolu_agent_1";
export const SUBAGENT_GREP_ID = "toolu_grep_1";

/** J2: the Agent (Task) tool runs a subagent that uses Grep. */
export function subagentJourney(): SDKMessage[] {
  return [
    init(),
    assistant("msg_10", [
      toolUse(AGENT_TOOL_ID, "Agent", {
        description: "Review the diff",
        prompt: "Look for TODOs. SECRET_SUBAGENT_PROMPT_MARKER",
        subagent_type: "code-reviewer",
        run_in_background: false,
      }),
    ]),
    taskStarted(AGENT_TOOL_ID),
    assistant("msg_11", [toolUse(SUBAGENT_GREP_ID, "Grep", { pattern: "TODO" })], AGENT_TOOL_ID),
    toolResult(SUBAGENT_GREP_ID, "src/a.ts:3: TODO", AGENT_TOOL_ID),
    assistant("msg_12", [text("Found one TODO.")], AGENT_TOOL_ID),
    taskNotification(AGENT_TOOL_ID, "completed"),
    toolResult(AGENT_TOOL_ID, "Found one TODO."),
    assistant("msg_13", [text("The reviewer found one TODO.")]),
    resultSuccess("The reviewer found one TODO.", { num_turns: 3 }),
  ];
}

export const BACKGROUND_PLACEHOLDER = "Agent is running in the background. You will be notified when it completes.";

/**
 * J2b: a foreground subagent moved to the background mid-run, either by
 * `system/task_updated` patch.is_backgrounded (sdk.d.ts:6059) or by the app
 * calling `Query.backgroundTasks()` (sdk.d.ts:3234). The Agent tool_result is
 * the "running in the background" placeholder; the subagent keeps working and
 * settles with `task_notification`.
 */
export function backgroundedSubagentJourney(
  options: { taskUpdated?: boolean; status?: SDKTaskNotificationMessage["status"] } = {},
): SDKMessage[] {
  const { taskUpdated: withTaskUpdated = true, status = "completed" } = options;
  return [
    init(),
    assistant("msg_10", [
      toolUse(AGENT_TOOL_ID, "Agent", {
        description: "Review the diff",
        prompt: "Look for TODOs. SECRET_SUBAGENT_PROMPT_MARKER",
        subagent_type: "code-reviewer",
        run_in_background: false,
      }),
    ]),
    taskStarted(AGENT_TOOL_ID, { is_backgrounded: false }),
    assistant("msg_11", [toolUse(SUBAGENT_GREP_ID, "Grep", { pattern: "TODO" })], AGENT_TOOL_ID),
    ...(withTaskUpdated ? [taskUpdated("task-1", { is_backgrounded: true })] : []),
    toolResult(AGENT_TOOL_ID, BACKGROUND_PLACEHOLDER),
    assistant("msg_13", [text("The reviewer is running in the background.")]),
    toolResult(SUBAGENT_GREP_ID, "src/a.ts:3: TODO", AGENT_TOOL_ID),
    assistant("msg_12", [text("Found one TODO.")], AGENT_TOOL_ID),
    taskNotification(AGENT_TOOL_ID, status),
    resultSuccess("The reviewer is running in the background.", { num_turns: 2 }),
  ];
}

export const MCP_TOOL_ID = "toolu_mcp_1";

/** J3: an MCP tool that errors, then the run stops on max turns. */
export function mcpErrorJourney(): SDKMessage[] {
  return [
    init({ mcp_servers: [{ name: "docs", status: "connected" }] }),
    assistant("msg_20", [toolUse(MCP_TOOL_ID, "mcp__docs__search", { query: "tracing" })]),
    toolResult(MCP_TOOL_ID, "server unavailable", null, true),
    assistant("msg_21", [text("The docs server failed.")]),
    resultError(),
  ];
}

type ModelUsage = SDKResultSuccess["modelUsage"][string];

/** One `modelUsage` entry (sdk.d.ts ModelUsage). */
export function modelUsage(
  input: number,
  output: number,
  costUSD: number,
  cacheRead = 0,
  cacheCreation = 0,
): ModelUsage {
  return {
    inputTokens: input,
    outputTokens: output,
    cacheReadInputTokens: cacheRead,
    cacheCreationInputTokens: cacheCreation,
    webSearchRequests: 0,
    costUSD,
    contextWindow: 200000,
    maxOutputTokens: 64000,
  };
}

/** `result.usage` for one main-loop turn (sdk.d.ts:5683: main agent loop only, per turn). */
export function turnUsage(input: number, output: number): NonNullableUsage {
  return { ...resultUsage, input_tokens: input, output_tokens: output, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 };
}

/** Cost of one 20-in / 8-out call on claude-sonnet-4-5 list prices, as the real CLI reports it. */
export const TURN_COST_USD = 0.00018;

/**
 * Streaming input: one query(), two user turns, two results. `total_cost_usd`
 * and `modelUsage` are running totals (sdk.d.ts:5679, 5687); `usage` is per
 * turn (sdk.d.ts:5683). Same numbers the real CLI reported against the mock.
 */
export function streamingInputJourney(): SDKMessage[] {
  return [
    init(),
    assistant("msg_s1", [text("First answer.")]),
    resultSuccess("First answer.", {
      num_turns: 1,
      total_cost_usd: TURN_COST_USD,
      usage: turnUsage(20, 8),
      modelUsage: { [MODEL]: modelUsage(20, 8, TURN_COST_USD) },
    }),
    assistant("msg_s2", [text("Second answer.")]),
    resultSuccess("Second answer.", {
      num_turns: 2,
      total_cost_usd: 2 * TURN_COST_USD,
      usage: turnUsage(20, 8),
      modelUsage: { [MODEL]: modelUsage(40, 16, 2 * TURN_COST_USD) },
    }),
  ];
}
