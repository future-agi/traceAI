#!/usr/bin/env node
// Stand-in for the `codex` binary: the real @openai/codex-sdk spawns it with
// `exec --experimental-json [...] [resume <id>]`, writes the prompt to stdin and
// reads JSONL events from stdout. FAKE_CODEX_SCENARIO picks the script.
import { readFileSync } from "node:fs";

const args = process.argv.slice(2);
if (args[0] !== "exec" || args[1] !== "--experimental-json") {
  process.stderr.write(`unexpected args: ${args.join(" ")}\n`);
  process.exit(2);
}
const resumeAt = args.indexOf("resume");
const threadId = resumeAt >= 0 ? args[resumeAt + 1] : "thread-contract-1";
const prompt = readFileSync(0, "utf8");
const scenario = process.env.FAKE_CODEX_SCENARIO || "tool";
const turns = resumeAt >= 0 ? 2 : 1; // usage is the thread's running total
const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

emit({ type: "thread.started", thread_id: threadId });
emit({ type: "turn.started" });
if (scenario === "crash") {
  // Real codex prints config errors with the absolute path; the SDK puts stderr in its thrown message.
  process.stderr.write(`Error loading config.toml:\n/Users/probeuser/.codex/config.toml:2:9: unclosed table; prompt was: ${prompt.trim()}\n`);
  process.exit(1);
}
if (scenario === "badline") {
  process.stdout.write(`{"type":"item.completed","item":{"aggregated_output":"AWS_SECRET=abc /Users/probeuser ${prompt.trim()}"\n`);
  await pause(1000);
  process.exit(0);
}
if (scenario === "retry") {
  emit({ type: "error", message: `Reconnecting... 1/5 (stream disconnected before completion) ${prompt.trim()}` });
}
await pause(20);
emit({ type: "item.completed", item: { id: "item_0", type: "agent_message", text: "Looking at the folder." } });
emit({ type: "item.started", item: { id: "item_1", type: "command_execution", command: "ls", aggregated_output: "", status: "in_progress" } });
await pause(30);
if (scenario === "failed") {
  emit({ type: "turn.failed", error: { message: "model provider returned 500" } });
  process.exit(1);
}
emit({
  type: "item.completed",
  item: { id: "item_1", type: "command_execution", command: "ls", aggregated_output: "README.md\n", exit_code: 0, status: "completed" },
});
emit({ type: "item.completed", item: { id: "item_2", type: "agent_message", text: `Echo: ${prompt.trim().slice(-24)}` } });
emit({
  type: "turn.completed",
  usage: { input_tokens: 100 * turns, cached_input_tokens: 40 * turns, cache_write_input_tokens: 0, output_tokens: 10 * turns, reasoning_output_tokens: 2 * turns },
});
