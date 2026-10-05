/**
 * R7 nits: package metadata points at the real repository, and the
 * instrumentation does not keep tracer providers alive forever.
 */
import { readFileSync } from "fs";
import { join } from "path";
import { setFlagsFromString } from "v8";
import { runInNewContext } from "vm";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { ClaudeAgentSDKInstrumentation } from "../index";
import { trackedProviderCount } from "../instrumentation";

const PKG_DIR = join(__dirname, "../..");

function manifest(dir: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
}

describe("package.json", () => {
  it("points repository, bugs and homepage at future-agi/traceAI, as traceai_anthropic does", () => {
    const ours = manifest(PKG_DIR);
    const sibling = manifest(join(PKG_DIR, "../traceai_anthropic"));
    expect(ours.repository).toEqual({
      ...(sibling.repository as object),
      directory: "typescript/packages/traceai_claude_agent_sdk",
    });
    expect(ours.bugs).toEqual(sibling.bugs);
    expect(ours.homepage).toBe(
      (sibling.homepage as string).replace("traceai_anthropic", "traceai_claude_agent_sdk"),
    );
    expect(JSON.stringify([ours.repository, ours.bugs, ours.homepage])).not.toContain("github.com/futureagi/");
  });
});

describe("tracer provider registry", () => {
  setFlagsFromString("--expose_gc");
  const gc = runInNewContext("gc") as () => void;
  const tick = () => new Promise((resolve) => setImmediate(resolve));

  it("does not keep a provider alive after the app drops it and its instrumentation", async () => {
    const before = trackedProviderCount();
    (() => {
      for (let i = 0; i < 20; i += 1) {
        new ClaudeAgentSDKInstrumentation({ tracerProvider: new BasicTracerProvider() });
      }
    })();
    expect(trackedProviderCount()).toBe(before + 20);
    for (let attempt = 0; attempt < 10 && trackedProviderCount() > before; attempt += 1) {
      await tick();
      gc();
      await tick();
    }
    expect(trackedProviderCount()).toBe(before);
  });

  it("still tracks a provider while its instrumentation is alive, once per provider", async () => {
    const provider = new BasicTracerProvider();
    const kept = [new ClaudeAgentSDKInstrumentation({ tracerProvider: provider })];
    kept[0].setTracerProvider(provider);
    const before = trackedProviderCount();
    await tick();
    gc();
    await tick();
    expect(trackedProviderCount()).toBe(before);
    expect(kept).toHaveLength(1);
  });
});
