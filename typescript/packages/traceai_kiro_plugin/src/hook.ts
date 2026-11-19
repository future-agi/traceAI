#!/usr/bin/env node
import { resolveConfig } from "@traceai/agent-core";
import { isAbsolute } from "node:path";
import { capture } from "./capture.js";
import { parseEvent, record } from "./events.js";

/** Kiro can forward stdout into model context and interpret nonzero exits as decisions. Always silent/zero. */
async function main(): Promise<void> {
  const [trigger, configDir, agentDir] = process.argv.slice(2);
  if (!trigger || !configDir || !agentDir || !isAbsolute(configDir) || !isAbsolute(agentDir)) return;
  let bytes = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += data.length;
    if (bytes > 1024 * 1024) return;
    chunks.push(data);
  }
  const payload = record(JSON.parse(Buffer.concat(chunks).toString("utf8")));
  const projectDir = typeof payload.cwd === "string" && isAbsolute(payload.cwd) ? payload.cwd : process.cwd();
  const config = await resolveConfig({ agent: "kiro-cli", projectDir, agentDir,
    env: { ...process.env, FI_AGENT_CONFIG_DIR: process.env.FI_AGENT_CONFIG_DIR ?? configDir } });
  const event = parseEvent(trigger, payload, config);
  if (event) await capture(event, config);
}
// No background worker or retries. Terminal export has a six-second total hook budget.
const deadline = setTimeout(() => process.exit(0), 6000);
main().catch(() => {}).finally(() => { clearTimeout(deadline); process.exit(0); });
