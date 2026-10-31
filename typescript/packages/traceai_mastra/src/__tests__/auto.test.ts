import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(__dirname, "../..");
function run(scenario: string, format = "cjs", fixture = "auto-runtime.cjs"): { ok: boolean; text?: string; streamText?: string; spanCount?: number; requests?: unknown[]; wireCount?: number; output?: string } {
  // The whole suite, including these sequential native-loader subprocesses, is
  // run inside the review heavy.sh slot. No test starts concurrent runtimes.
  const output = execFileSync(process.execPath, [resolve(root, "test", fixture), scenario, format], {
    cwd: root,
    timeout: 25000,
    encoding: "utf8",
    env: { ...process.env, OTEL_SDK_DISABLED: "false", TMPDIR: resolve(root, ".tmp") },
    stdio: ["ignore", "pipe", "pipe"],
  });
  return JSON.parse(output.trim().split("\n").at(-1)!);
}

// Native load hooks require Node 22.15 / 23.5 or newer. An unsupported runtime
// must run the explicit fail-open unit path rather than pretend this is tested.
describe("Mastra zero-code native loader (real Mastra + mock model)", () => {
  it.each(["cjs", "esm"])("installs one exporter for generate and stream through %s", format => {
    const baseline = run("baseline", format);
    const instrumented = run("default", format);
    expect(instrumented.ok).toBe(true);
    expect(instrumented.text).toBe(baseline.text);
    expect(instrumented.streamText).toBe(baseline.streamText);
    expect(instrumented.spanCount).toBeGreaterThan(0);
  });

  it.each(["cjs", "esm"])("preserves configured exporters, frozen arrays and selector through %s", format => {
    expect(run("configured", format).ok).toBe(true);
  });

  it.each(["cjs", "esm"])("supports the @mastra/core/mastra entry through %s", format => {
    expect(run("subpath", format).ok).toBe(true);
  });

  it.each(["cjs", "esm"])("activates through the actual auto bootstrap without an application enable call through %s", format => {
    expect(run("real-agent-bootstrap", format).ok).toBe(true);
  });
  it.each(["cjs", "esm"])("uses the real auto-agent privacy boundary and skips duplicate wire spans through %s", format => {
    expect(run("real-agent", format).ok).toBe(true);
  });
  it.each(["real-agent-pre", "real-agent-bridge"])("preserves wire coverage with %s", scenario => {
    expect(run(scenario).ok).toBe(true);
  });
  it.each(["cjs", "esm"])("root import before init keeps model/native spans connected and wire deduplicated through %s", format => {
    expect(run("root-enable", format, "coverage-runtime.cjs").ok).toBe(true);
  });
  it.each(["cjs", "esm"])("context-setting native bridge keeps coverage and exports each identity once through %s", format => {
    expect(run("bridge-replace", format, "coverage-runtime.cjs").ok).toBe(true);
  });
  it.each(["cjs", "esm"])("replacement span output processors preserve native bridge deduplication and baseline output through %s", format => {
    const baseline = run("bridge-copy-baseline", format, "coverage-runtime.cjs");
    const instrumented = run("bridge-copy", format, "coverage-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.requests).toEqual(baseline.requests);
    expect(instrumented.wireCount).toBe(0);
    expect(baseline.wireCount).toBe(1);
  });
  it("replacement processors with a custom exportSpan retain exact native identity association", () => {
    expect(run("bridge-copy-custom", "cjs", "coverage-runtime.cjs").ok).toBe(true);
  });
  it.each(["cjs", "esm"])("cached root import plus application config inherits sampled upstream at a low ratio through %s", format => {
    expect(run("cached-config", format, "coverage-runtime.cjs").ok).toBe(true);
  });
  it("native bridge preserves recording span identity, attributes and events against baseline", () => {
    const baseline = run("bridge-baseline", "cjs", "coverage-runtime.cjs");
    const instrumented = run("bridge-replace", "cjs", "coverage-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.requests).toEqual(baseline.requests);
    expect(instrumented.wireCount).toBe(0);
    expect(baseline.wireCount).toBe(1);
  });
  it.each(["cjs", "esm"])("real workflow Date.toJSON is never called and agent-on/off returned data is identical through %s", format => {
    const baseline = run("snapshot-baseline", format, "snapshot-runtime.cjs");
    const instrumented = run("snapshot", format, "snapshot-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.spanCount).toBeGreaterThan(0);
    expect(baseline.spanCount).toBe(0);
  });
  it.each(["cjs", "esm"])("covered real OpenAI provider errors redact credentials without changing the application through %s", format => {
    const baseline = run("secret-baseline", format, "privacy-runtime.cjs");
    const instrumented = run("secret", format, "privacy-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.requests).toEqual(baseline.requests);
    expect(instrumented.wireCount).toBe(0);
    expect(baseline.wireCount).toBe(1);
  });
  it.each(["cjs", "esm"])("core S5 redacts queryParams and already-started application transaction errors through %s", format => {
    for (const scenario of ["secret-query", "secret-outer", "secret-query-outer"]) {
      const baseline = run(`${scenario}-baseline`, format, "privacy-runtime.cjs");
      const instrumented = run(scenario, format, "privacy-runtime.cjs");
      expect(instrumented.output).toBe(baseline.output);
      expect(instrumented.requests).toEqual(baseline.requests);
      expect(instrumented.wireCount).toBe(0);
      expect(baseline.wireCount).toBe(1);
      expect(instrumented.ok).toBe(true);
    }
  }, 150000);
  it("core S5 also redacts already-started native SDK status and exception events", () => {
    const baseline = run("secret-native-baseline", "cjs", "privacy-runtime.cjs");
    const instrumented = run("secret-native", "cjs", "privacy-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.requests).toEqual(baseline.requests);
    expect(instrumented.wireCount).toBe(0);
  });
  it.each(["params-agent", "params-direct"])("canonicalizes invocation aliases before the sole hide boundary with %s", scenario => {
    expect(run(scenario, "cjs", "privacy-runtime.cjs").ok).toBe(true);
    expect(run(`${scenario}-visible`, "cjs", "privacy-runtime.cjs").ok).toBe(true);
  });
  it.each(["cjs", "esm"])("real Mastra embedding output obeys vector hiding without losing spans or mutating application data through %s", format => {
    expect(run("embedding", format, "privacy-runtime.cjs").ok).toBe(true);
    expect(run("embedding-visible", format, "privacy-runtime.cjs").ok).toBe(true);
    expect(run("embedding-output-hidden", format, "privacy-runtime.cjs").ok).toBe(true);
  });
  it("workflow → agent → tool retains independent wire coverage and baseline output", () => {
    const baseline = run("tool-baseline", "cjs", "coverage-runtime.cjs");
    const instrumented = run("tool", "cjs", "coverage-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.requests).toEqual(baseline.requests);
    expect(instrumented.wireCount).toBe(1);
  });
  it("sampled-out Mastra preserves upstream identity and wire fallback against baseline", () => {
    const baseline = run("never-baseline", "cjs", "coverage-runtime.cjs");
    const instrumented = run("never", "cjs", "coverage-runtime.cjs");
    expect(instrumented.output).toBe(baseline.output);
    expect(instrumented.requests).toEqual(baseline.requests);
    expect(instrumented.wireCount).toBe(1);
  });
  it("sampled upstream keeps framework ancestors at a low root sampling ratio", () => {
    expect(run("sampled-parent", "cjs", "coverage-runtime.cjs").ok).toBe(true);
  });
  it("sampling decision survives export outside the upstream transaction context", () => {
    expect(run("detached-parent", "cjs", "coverage-runtime.cjs").ok).toBe(true);
  });
  it("zero head sampling never forces native descendants to record orphan spans", () => {
    expect(run("zero-native", "cjs", "coverage-runtime.cjs").ok).toBe(true);
  });
  it("keeps late-registered observability instances idempotent", () => {
    expect(run("late-instance").ok).toBe(true);
  });
  it("returns the original promise/error and never reads a configuration getter early", () => {
    expect(run("boundary-parity").ok).toBe(true);
  });
  it("fails open when synchronous Node loader hooks are unavailable", () => {
    expect(run("unsupported-node").ok).toBe(true);
  });
  it("also supports the enableMastra export on the package root", () => {
    expect(run("root-enable").ok).toBe(true);
  });
  it("keeps pre-instantiated observability instance identity and exporters", () => {
    expect(run("pre-instantiated").ok).toBe(true);
  });
  it("does not add a second exporter beside an explicitly configured Future AGI exporter", () => {
    expect(run("manual-dedup").ok).toBe(true);
  });
  it("respects explicitly empty observability without enabling it", () => {
    expect(run("empty").ok).toBe(true);
  });
  it("honors an unsampled upstream transaction", () => {
    expect(run("real-agent-parent-off").ok).toBe(true);
  });
  it.each(["allowlist", "denylist"])("honors the resolved integration %s", scenario => {
    expect(run(scenario).ok).toBe(true);
  });
  it("honors the agent sample rate of zero", () => {
    expect(run("sampling-zero").ok).toBe(true);
  });
  it("fails open when the forwarding processor throws", () => {
    expect(run("broken-processor").ok).toBe(true);
  });
  it("preserves Mastra's own configuration validation", () => {
    expect(run("failure-parity").ok).toBe(true);
  });
  it("does not enable without an initialized agent", () => {
    expect(run("missing-agent").ok).toBe(true);
  });
  it("respects OTEL_SDK_DISABLED", () => {
    expect(run("disabled").ok).toBe(true);
  });
});
