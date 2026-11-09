#!/usr/bin/env node
import { handleHook } from "./runtime.js";

// Gemini waits for command hooks. No policy decisions, no model response overrides, no stdout diagnostics.
let done = false;
function finish(): void {
  if (done) return;
  done = true;
  process.stdout.write("{}\n", () => process.exit(0));
}
const deadline = setTimeout(finish, 5500);
const flag = process.argv.indexOf("--config-dir");
if (!process.env.FI_AGENT_CONFIG_DIR && flag >= 0 && process.argv[flag + 1])
  process.env.FI_AGENT_CONFIG_DIR = process.argv[flag + 1];
let bytes = 0;
const chunks: Buffer[] = [];
process.stdin.on("data", (chunk: Buffer) => {
  bytes += chunk.length;
  if (bytes > 1024 * 1024) {
    process.stdin.pause();
    finish();
  } else chunks.push(chunk);
});
process.stdin.on("error", finish);
process.stdin.on("end", async () => {
  try {
    await handleHook(JSON.parse(Buffer.concat(chunks).toString("utf8")));
  } catch {
    /* Telemetry must never fail an agent operation or expose raw errors. */
  } finally {
    clearTimeout(deadline);
    finish();
  }
});
