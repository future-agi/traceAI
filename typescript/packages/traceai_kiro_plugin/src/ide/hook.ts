#!/usr/bin/env node
import { resolveConfig } from "@traceai/agent-core";
import { IDE_QUALIFIED } from "./qualification.js";
import { IDE_EVENTS } from "./events.js";
import { observeIdeEvent } from "./observer.js";
import { isAbsolute } from "node:path";

async function main(): Promise<void> {
  // Preview refuses to collect until the IDE payload and cross-surface provenance are qualified.
  // CLI fixture replay, USER_PROMPT alone, or a project-authored marker is not IDE provenance.
  if (!IDE_QUALIFIED || ["false", "0"].includes(process.env.FI_AGENT_ENABLED ?? "")) return;
  const args = process.argv.slice(2);
  const option = (name: string): string | undefined => {
    const i = args.indexOf(name);
    return i < 0 ? undefined : args[i + 1];
  };
  const trigger = option("--event"), projectDir = option("--project-dir"), configDir = option("--config-dir");
  if (option("--marker") !== "fi-kiro-ide-v1" || !IDE_EVENTS.includes(trigger as typeof IDE_EVENTS[number]) ||
    !projectDir || !isAbsolute(projectDir) || !configDir || !isAbsolute(configDir)) return;
  const env = { ...process.env, FI_AGENT_CONFIG_DIR: process.env.FI_AGENT_CONFIG_DIR || configDir };
  const config = await resolveConfig({ agent: "kiro-ide", env, projectDir });
  if (!config.enabled) return;
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const bytes of process.stdin) {
    length += bytes.length;
    if (length > 256 * 1024) return;
    chunks.push(Buffer.from(bytes));
  }
  const raw = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  await observeIdeEvent(config, trigger!, raw, process.env.USER_PROMPT);
}
// Empty stdout AND stderr, exit zero even on malformed input, missing config or export failure.
// The shell wrapper also neutralizes a missing/corrupt runtime. No prompt injection or permission decisions.
const deadline = setTimeout(() => process.exit(0), process.argv.includes("Stop") ? 6000 : 1000);
main().catch(() => {}).finally(() => { clearTimeout(deadline); process.exit(0); });
