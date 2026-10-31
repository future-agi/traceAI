#!/usr/bin/env node
"use strict";
// Neutral observer: no imports from the runtime, SQLite, credential lookup,
// HTTP, stdout, disk writes, retries or control-flow responses.
const net = require("node:net");
const args = process.argv.slice(2);
const events = new Set([
  "SessionStart",
  "UserPromptSubmit",
  "PreModelRequest",
  "PostModelRequest",
  "PostToolUse",
  "PostCompact",
  "Stop",
  "SubagentStop",
  "Interrupt",
  "SessionEnd",
]);
let chunks = [],
  size = 0,
  socket;
const end = () => {
  socket?.destroy();
  process.exit(0);
};
setTimeout(end, 25);
process.on("uncaughtException", end);
process.on("unhandledRejection", end);
process.stdin.on("error", end);
process.stdin.on("data", (chunk) => {
  size += chunk.length;
  if (size > 1048576) return end();
  chunks.push(chunk);
});
process.stdin.on("end", () => {
  try {
    if (
      args.length !== 4 ||
      args[0] !== "--socket" ||
      args[2] !== "--route" ||
      args[3] !== "codex.cli" ||
      !args[1].startsWith("/")
    )
      return end();
    const raw = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    chunks = [];
    if (
      !events.has(raw.hook_event_name) ||
      typeof raw.session_id !== "string" ||
      !/^[a-zA-Z0-9_-]{1,256}$/.test(raw.session_id)
    )
      return end();
    if (
      raw.transcript_path !== null &&
      (typeof raw.transcript_path !== "string" ||
        raw.transcript_path.length > 4096 ||
        !raw.transcript_path.startsWith("/"))
    )
      return end();
    // Anonymous pre/post callbacks are wakeups and activation evidence ONLY.
    // Deliberately discard model, prompt, tools, error and token_usage.
    const body = Buffer.from(
      JSON.stringify({
        protocol_version: 1,
        operation: "adapter-observe",
        route_ref: "codex.cli",
        input: {
          event: raw.hook_event_name,
          session: raw.session_id,
          path: raw.transcript_path,
        },
      }),
    );
    const prefix = Buffer.alloc(4);
    prefix.writeUInt32BE(body.length);
    socket = net.createConnection(args[1]);
    socket.on("error", end);
    socket.on("end", end);
    socket.on("data", end);
    socket.on("connect", () => socket.write(Buffer.concat([prefix, body])));
  } catch {
    end();
  }
});
