import { readFileSync } from "fs";
import { join } from "path";
import * as api from "../index";

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
      directory: "typescript/packages/traceai_pi_coding_agent",
    });
    expect(ours.bugs).toEqual(sibling.bugs);
    expect(ours.homepage).toBe("https://docs.futureagi.com/docs/tracing");
  });

  it("has no runtime dependency on the Pi SDK", () => {
    const ours = manifest(PKG_DIR) as { dependencies: Record<string, string> };
    expect(Object.keys(ours.dependencies).filter((name) => name.includes("pi-coding-agent"))).toEqual([]);
  });

  it("keeps the version in src/version.ts in step with package.json", () => {
    expect(api.VERSION).toBe(manifest(PKG_DIR).version);
  });
});

describe("public API", () => {
  it("exports the entry points the README documents", () => {
    for (const name of [
      "PiCodingAgentInstrumentation",
      "instrumentPiCodingAgent",
      "instrumentSession",
      "wrapCreateAgentSession",
      "shutdown",
      "resolveContentPolicy",
    ]) {
      expect(typeof (api as Record<string, unknown>)[name]).toBe("function");
    }
  });

  it("hides content unless asked", () => {
    const saved = [process.env.FI_HIDE_INPUTS, process.env.FI_HIDE_OUTPUTS];
    delete process.env.FI_HIDE_INPUTS;
    delete process.env.FI_HIDE_OUTPUTS;
    try {
      expect(api.resolveContentPolicy()).toMatchObject({ hideInputs: true, hideOutputs: true });
      expect(api.resolveContentPolicy({ hideInputs: false })).toMatchObject({ hideInputs: false, hideOutputs: true });
    } finally {
      if (saved[0] === undefined) delete process.env.FI_HIDE_INPUTS;
      else process.env.FI_HIDE_INPUTS = saved[0];
      if (saved[1] === undefined) delete process.env.FI_HIDE_OUTPUTS;
      else process.env.FI_HIDE_OUTPUTS = saved[1];
    }
  });
});
