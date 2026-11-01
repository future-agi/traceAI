"use strict";
// Independently authored synthetic fixtures from the documented fields, not captured user data.
// https://cursor.com/docs/hooks (common schema, postToolUse, postToolUseFailure, stop)
// https://cursor.com/docs/cli/reference/output-format (partial stream flush and tool IDs)
const canary = "CURSOR_PRIVATE_CANARY";
const hook = (event = "postToolUse", surface = "ide") => ({
  conversation_id: "conversation-fixture",
  generation_id: "turn-1",
  cursor_version: surface === "ide" ? "3.24.18" : "2026.10.17",
  hook_event_name: event,
  model: canary,
  model_id: canary,
  workspace_roots: ["/private/" + canary],
  user_email: canary + "@invalid.example",
  transcript_path: "/private/" + canary,
  tool_use_id: "tool-1",
  tool_name: "Shell",
  duration: 40,
  tool_input: { command: canary },
  tool_output: canary,
  error_message: canary,
  text: canary,
  prompt: canary,
  status: "completed",
  loop_count: 0,
  failure_type: "error",
  is_interrupt: false,
});
const stream = () => {
  const session_id = "conversation-stream";
  return [
    {
      type: "system",
      subtype: "init",
      session_id,
      model: canary,
      cwd: "/private/" + canary,
    },
    {
      type: "user",
      session_id,
      message: { role: "user", content: [{ type: "text", text: canary }] },
    },
    {
      type: "assistant",
      session_id,
      timestamp_ms: 1793400000000,
      message: { role: "assistant", content: [{ type: "text", text: canary }] },
    },
    {
      type: "assistant",
      session_id,
      timestamp_ms: 1793400000100,
      model_call_id: "model-1",
      message: { role: "assistant", content: [{ type: "text", text: canary }] },
    },
    {
      type: "tool_call",
      subtype: "started",
      session_id,
      call_id: "tool-1",
      tool_call: { readToolCall: { args: { path: canary } } },
    },
    {
      type: "tool_call",
      subtype: "completed",
      session_id,
      call_id: "tool-1",
      tool_call: { readToolCall: { result: { success: { content: canary } } } },
    },
    {
      type: "assistant",
      session_id,
      timestamp_ms: 1793400000200,
      model_call_id: "model-2",
      message: { role: "assistant", content: [{ type: "text", text: canary }] },
    },
    {
      type: "tool_call",
      subtype: "completed",
      session_id,
      call_id: "tool-2",
      tool_call: { writeToolCall: { result: { success: { path: canary } } } },
    },
    {
      type: "assistant",
      session_id,
      message: { role: "assistant", content: [{ type: "text", text: canary }] },
    },
    {
      type: "result",
      subtype: "success",
      is_error: false,
      session_id,
      request_id: "run-1",
      duration_ms: 1000,
      duration_api_ms: 1000,
      result: canary,
    },
  ];
};
module.exports = {
  hook,
  stream,
  canary,
  bytes: (records) =>
    Buffer.from(records.map((r) => JSON.stringify(r)).join("\n") + "\n"),
};
