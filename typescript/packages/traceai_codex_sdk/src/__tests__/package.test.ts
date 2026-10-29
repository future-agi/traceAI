import { readFileSync } from "fs";
import { join } from "path";
import * as pkg from "../index";

const PKG_DIR = join(__dirname, "../..");

function manifest(dir: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
}

describe("package", () => {
  it("exports the public API", () => {
    expect(typeof pkg.CodexSDKInstrumentation).toBe("function");
    expect(typeof pkg.instrumentCodexSDK).toBe("function");
    expect(typeof pkg.shutdown).toBe("function");
    expect(typeof pkg.isPatched).toBe("function");
    expect(pkg.INSTRUMENTATION_NAME).toBe("@traceai/codex-sdk");
    expect(pkg.SpanNames.TURN).toBe("codex.turn");
    expect(pkg.VERSION).toBe(manifest(PKG_DIR).version);
  });

  it("points repository, bugs and homepage at future-agi/traceAI, as traceai_anthropic does", () => {
    const ours = manifest(PKG_DIR);
    const sibling = manifest(join(PKG_DIR, "../traceai_anthropic"));
    expect(ours.repository).toEqual({ ...(sibling.repository as object), directory: "typescript/packages/traceai_codex_sdk" });
    expect(ours.bugs).toEqual(sibling.bugs);
    expect(ours.homepage).toBe("https://docs.futureagi.com/docs/tracing");
  });

  it("keeps the SDK a peer dependency, never a runtime dependency", () => {
    const ours = manifest(PKG_DIR) as { dependencies: Record<string, string>; peerDependencies: Record<string, string> };
    expect(ours.dependencies["@openai/codex-sdk"]).toBeUndefined();
    expect(ours.peerDependencies["@openai/codex-sdk"]).toBe(">=0.44.0 <1");
  });

  it("defaults to hidden content", () => {
    delete process.env.FI_HIDE_INPUTS;
    delete process.env.FI_HIDE_OUTPUTS;
    expect(pkg.resolveContentPolicy()).toEqual({ hideInputs: true, hideOutputs: true, hideErrorDetails: true });
    expect(pkg.resolveContentPolicy({ hideInputs: false })).toEqual({ hideInputs: false, hideOutputs: true, hideErrorDetails: true });
  });
});
