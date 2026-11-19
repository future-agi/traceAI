import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, copyFileSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { integrations } from "../src/libraries";

const HOSTED = ["@anthropic-ai/bedrock-sdk", "@anthropic-ai/vertex-sdk"];
const packages = resolve(__dirname, "../..");
const anthropicModules = resolve(packages, "traceai_anthropic/node_modules");

test("the anthropic entry covers the Bedrock and Vertex AI clients", () => {
  const entry = integrations.find(item => item.name === "anthropic")!;
  expect(entry.package).toBe("@traceai/anthropic");
  expect(entry.className).toBe("AnthropicInstrumentation");
  for (const name of HOSTED) {
    expect(entry.targets).toContain(name);
    expect(entry.modules).toContain(name);
  }
  expect(entry.manual).toBeUndefined();
});

/** An application directory holding exactly these packages, linked from the workspace. */
function app(installed: string[]) {
  const dir = mkdtempSync(join(tmpdir(), "traceai-anthropic-app-"));
  const link = (name: string, target: string) => {
    mkdirSync(join(dir, "node_modules", name, ".."), { recursive: true });
    symlinkSync(realpathSync(target), join(dir, "node_modules", name), "dir");
  };
  link("@traceai/auto", resolve(packages, "traceai_auto"));
  link("@traceai/anthropic", resolve(packages, "traceai_anthropic"));
  for (const name of installed) link(name, join(anthropicModules, name));
  copyFileSync(resolve(__dirname, "apps/anthropic-hosted.cjs"), join(dir, "app.cjs"));
  return dir;
}

function run(installed: string[], env: Record<string, string> = {}) {
  const dir = app(installed);
  try {
    const base: NodeJS.ProcessEnv = { ...process.env };
    for (const key of Object.keys(base)) if (/^(?:FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete base[key];
    const result = spawnSync(process.execPath, ["app.cjs"], { cwd: dir, env: { ...base, ...env }, encoding: "utf8", timeout: 40000 });
    if (result.status !== 0) throw new Error(`${result.error ?? ""}\n${result.stderr}\n${result.stdout}`);
    return JSON.parse(result.stdout.trim().split("\n").at(-1)!);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("an application with only a hosted client package is pending at startup and instrumented once it is used", () => {
  const result = run(["@anthropic-ai/sdk", "@anthropic-ai/bedrock-sdk"].slice(1));
  expect(result.before).toBe("pending");
  expect(result).toMatchObject({ status: "instrumented", provider: "aws.bedrock" });
});

test("the base SDK and a hosted client share one Messages class: the status stays instrumented", () => {
  const result = run(["@anthropic-ai/sdk", "@anthropic-ai/bedrock-sdk"], { APP_BASE_FIRST: "1" });
  expect(result).toMatchObject({ status: "instrumented", provider: "aws.bedrock" });
  expect(result.reason).toBeUndefined();
});
