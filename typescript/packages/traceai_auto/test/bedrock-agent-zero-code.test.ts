import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { integrations } from "../src/libraries";

function run(env: Record<string, string | undefined> = {}) {
  const base: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(base)) if (/^(?:FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete base[key];
  const result = spawnSync(process.execPath, [resolve(__dirname, "apps/bedrock-agent.cjs")], { cwd: resolve(__dirname, ".."), env: { ...base, ...env }, encoding: "utf8", timeout: 25000 });
  if (result.status !== 0) throw new Error(`${result.error ?? ""}\n${result.stderr}\n${result.stdout}`);
  return JSON.parse(result.stdout.trim().split("\n").at(-1)!);
}

test("an application with only @aws-sdk/client-bedrock-agent-runtime gets Bedrock Agent spans with no code changes", () => {
  expect(run()).toEqual({ status: "instrumented", agentKind: "AGENT", output: "hello world", childOfAgent: true, tokens: 3 });
});

test("the bedrock catalog entry covers the agent-runtime SDK, so it activates when only that SDK is installed", () => {
  const entry = integrations.find(i => i.name === "bedrock")!;
  for (const field of [entry.targets, entry.modules]) expect(field).toContain("@aws-sdk/client-bedrock-agent-runtime");
});
