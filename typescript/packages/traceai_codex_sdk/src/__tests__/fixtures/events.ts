/**
 * Event streams shaped like real `codex exec --experimental-json` output
 * (captured from @openai/codex-sdk 0.162.1 + codex-cli 0.162.1, then trimmed).
 * Usage on `turn.completed` is the thread's running total.
 */
import { ThreadEventLike } from "../../types";

export const THREAD_ID = "01a13d0e-b7c1-7080-a94a-fdbbdd9dd475";
export const PROMPT_MARKER = "MARKER-PROMPT-4417";
export const COMMAND_MARKER = "MARKER-CMD-8823";
export const OUTPUT_MARKER = "MARKER-OUT-5150";
export const ANSWER_MARKER = "MARKER-ANSWER-9091";

export const PROMPT = `List the files and read README.md ${PROMPT_MARKER}`;

export function toolTurn(opts: { threadId?: string; usage?: Record<string, number> } = {}): ThreadEventLike[] {
  return [
    { type: "thread.started", thread_id: opts.threadId ?? THREAD_ID },
    { type: "turn.started" },
    { type: "item.completed", item: { id: "item_0", type: "reasoning", text: "Plan: list then read." } },
    { type: "item.completed", item: { id: "item_1", type: "agent_message", text: "I'll inspect the folder." } },
    {
      type: "item.started",
      item: { id: "item_2", type: "command_execution", command: `ls -la ${COMMAND_MARKER}`, aggregated_output: "", status: "in_progress" },
    },
    {
      type: "item.completed",
      item: {
        id: "item_2",
        type: "command_execution",
        command: `ls -la ${COMMAND_MARKER}`,
        aggregated_output: `README.md\napp.py ${OUTPUT_MARKER}\n`,
        exit_code: 0,
        status: "completed",
      },
    },
    { type: "item.completed", item: { id: "item_3", type: "agent_message", text: `The first line is # demo ${ANSWER_MARKER}` } },
    {
      type: "turn.completed",
      usage: opts.usage ?? {
        input_tokens: 19378,
        cached_input_tokens: 16896,
        cache_write_input_tokens: 0,
        output_tokens: 177,
        reasoning_output_tokens: 20,
      },
    },
  ];
}

export function failedTurn(): ThreadEventLike[] {
  return [
    { type: "thread.started", thread_id: THREAD_ID },
    { type: "turn.started" },
    {
      type: "item.started",
      item: { id: "item_0", type: "command_execution", command: "sleep 100", aggregated_output: "", status: "in_progress" },
    },
    { type: "turn.failed", error: { message: "stream disconnected before completion" } },
  ];
}
