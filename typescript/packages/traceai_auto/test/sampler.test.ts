import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

interface Snapshot {
  spans: { name: string; recording: boolean; sampled: boolean }[];
  exported: { name: string; sampled: boolean }[];
  warnings: string[];
  diagnostics: { source: "console" | "otel"; level: "warn" | "error"; message: string }[];
  sameProvider: boolean;
  hostCalls: number;
}

function probe(extra: NodeJS.ProcessEnv = {}, fileRate?: number): Snapshot {
  const cwd = mkdtempSync(join(tmpdir(), "traceai-sampler-"));
  try {
    const env: NodeJS.ProcessEnv = { ...process.env };
    for (const key of Object.keys(env)) {
      if (key.startsWith("FI_") || key.startsWith("OTEL_") || key.startsWith("TRACEAI_") || key.startsWith("TEST_") || key === "NODE_OPTIONS") delete env[key];
    }
    Object.assign(env, { HOME: cwd, XDG_CONFIG_HOME: cwd, FI_CONFIG_FILE: join(cwd, "futureagi.toml") }, extra);
    if (fileRate !== undefined) writeFileSync(env.FI_CONFIG_FILE!, `sample_rate = ${fileRate}\n`);
    const child = spawnSync(process.execPath, [resolve(__dirname, "apps/sampler.cjs")], { cwd, env, encoding: "utf8", timeout: 15000 });
    expect(child.error).toBeUndefined();
    expect(child.status).toBe(0);
    expect(child.stderr).toBe("");
    return JSON.parse(child.stdout);
  } finally { rmSync(cwd, { recursive: true, force: true }); }
}

function decisions(result: Snapshot, recording: boolean[], sampled = recording): void {
  expect(result.spans.map(span => span.recording)).toEqual(recording);
  expect(result.spans.map(span => span.sampled)).toEqual(sampled);
  expect(result.exported).toEqual(result.spans.filter(span => span.recording).map(({ name, sampled }) => ({ name, sampled })));
}

// The OpenTelemetry SDK reads OTEL_TRACES_SAMPLER/_ARG itself in several places and reports invalid or blank
// values through the application's diagnostic logger; those messages are the SDK's, not the agent's.
const isSdkSamplerDiagnostic = (entry: { source: string; message: string }) =>
  entry.source === "otel" && /OTEL_TRACES_SAMPLER(_ARG)?\b/.test(entry.message);

function diagnostics(result: Snapshot, warning?: string): void {
  expect(result.warnings).toEqual(warning ? [warning] : []);
  // Compare every other warn/error from both console and the application's OTel logger.
  // Sentinels prove that logger was enabled before init and survives shutdown.
  expect(result.diagnostics.filter(entry => !isSdkSamplerDiagnostic(entry))).toEqual([
    { source: "otel", level: "warn", message: "sampler fixture: before init warning" },
    { source: "otel", level: "error", message: "sampler fixture: before init error" },
    ...(warning ? [{ source: "console", level: "warn", message: warning }] : []),
    { source: "otel", level: "warn", message: "sampler fixture: after shutdown warning" },
    { source: "otel", level: "error", message: "sampler fixture: after shutdown error" },
  ]);
}

// Integration tests exercise the actual sampler passed by init(), not a separate
// test-only factory. Build the package before running these tests, as for e2e.test.ts.
test.each([
  ["always_on", undefined, [true, true, true]],
  ["always_off", undefined, [false, false, false]],
  ["traceidratio", "0", [false, false, false]],
  ["parentbased_always_on", undefined, [true, true, false]],
  ["parentbased_always_off", undefined, [false, true, false]],
  ["parentbased_traceidratio", "0", [false, true, false]],
] as const)("standard env %s honors root and parent decisions when the rate is unset", (sampler, arg, expected) => {
  const result = probe({ OTEL_TRACES_SAMPLER: sampler, OTEL_TRACES_SAMPLER_ARG: arg });
  decisions(result, [...expected]);
  expect(result.warnings).toEqual([]);
});

test.each(["true", "false"])("parentbased ratio zero preserves the W3C sampled flag for remote=%s", remote => {
  const result = probe({ OTEL_TRACES_SAMPLER: "parentbased_traceidratio", OTEL_TRACES_SAMPLER_ARG: "0", TEST_REMOTE: remote });
  decisions(result, [false, true, false]);
});

test("an unset rate and sampler default to ParentBased(TraceIdRatio(1))", () => {
  const result = probe();
  decisions(result, [true, true, false]);
  expect(result.warnings).toEqual([]);
});

test.each(["traceidratio", "parentbased_traceidratio"])("%s accepts the ratio-one boundary", sampler => {
  const result = probe({ OTEL_TRACES_SAMPLER: sampler, OTEL_TRACES_SAMPLER_ARG: "1" });
  decisions(result, sampler === "traceidratio" ? [true, true, true] : [true, true, false]);
  expect(result.warnings).toEqual([]);
});

test.each(["", "   "])("a blank sampler %j is unset, as in the OTel SDK", sampler => {
  const result = probe({ OTEL_TRACES_SAMPLER: sampler });
  decisions(result, [true, true, false]);
  expect(result.warnings).toEqual([]);
});

test.each([undefined, "", "   "])("traceidratio with an absent or blank argument %j uses the SDK ratio-one default", arg => {
  const result = probe({ OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: arg });
  decisions(result, [true, true, true]);
  expect(result.warnings).toEqual([]);
});

test.each([
  ["environment", { FI_SAMPLE_RATE: "1" }, undefined],
  ["code option", { TEST_SAMPLE_RATE: "1" }, undefined],
  ["config file", {}, 1],
] as const)("explicit rate from %s wins over always_off and preserves record-only parent flags", (_source, env, fileRate) => {
  const result = probe({ OTEL_TRACES_SAMPLER: "always_off", ...env }, fileRate);
  // Existing aiSampler behavior: record under an unsampled parent, but never
  // change the application's flag on either the live span or its export.
  decisions(result, [true, true, true], [true, true, false]);
  expect(result.warnings).toEqual([]);
});

test("explicit zero wins over always_on and drops even a sampled parent's child", () => {
  const result = probe({ FI_SAMPLE_RATE: "0", OTEL_TRACES_SAMPLER: "always_on" });
  decisions(result, [false, false, false]);
  expect(result.warnings).toEqual([]);
});

test("the code rate wins over the environment and file rates", () => {
  decisions(probe({ TEST_SAMPLE_RATE: "0", FI_SAMPLE_RATE: "1", OTEL_TRACES_SAMPLER: "always_on" }, 1), [false, false, false]);
});

test.each(["unsupported_sampler", "ALWAYS_OFF", "always_off "])("invalid sampler %j falls back with one value-free warning", sampler => {
  const result = probe({ OTEL_TRACES_SAMPLER: sampler });
  decisions(result, [true, true, false]);
  expect(result.warnings).toHaveLength(1);
  expect(result.warnings[0]).toContain("OTEL_TRACES_SAMPLER");
  expect(result.warnings[0]).not.toContain(sampler);
});

test.each(["not-a-ratio", "NaN", "Infinity", "-0.1", "1.1"])("invalid ratio argument %j falls back with one warning", arg => {
  const result = probe({ OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: arg });
  decisions(result, [true, true, false]);
  expect(result.warnings).toHaveLength(1);
  expect(result.warnings[0]).toContain("OTEL_TRACES_SAMPLER_ARG");
  expect(result.warnings[0]).not.toContain(arg);
});

describe.each(["traceidratio", "parentbased_traceidratio"])("enabled OTel diagnostics for %s", sampler => {
  test.each(["not-a-ratio", "NaN", "Infinity", "-0.1", "1.1"])("invalid argument %j emits exactly one warning and no errors", arg => {
    const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", OTEL_TRACES_SAMPLER: sampler, OTEL_TRACES_SAMPLER_ARG: arg });
    decisions(result, [true, true, false]);
    diagnostics(result, "[traceai:auto] Invalid OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIdRatio(1)).");
  });
});

test("unsupported sampler emits exactly one warning with OTel diagnostics enabled", () => {
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", OTEL_TRACES_SAMPLER: "unsupported_sampler", OTEL_TRACES_SAMPLER_ARG: "not-a-ratio" });
  decisions(result, [true, true, false]);
  diagnostics(result, "[traceai:auto] Invalid or unsupported OTEL_TRACES_SAMPLER; using ParentBased(TraceIdRatio(1)).");
});

test("explicit rate ignores invalid OTel input without warn/error diagnostics", () => {
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", FI_SAMPLE_RATE: "1", OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: "not-a-ratio" });
  decisions(result, [true, true, true], [true, true, false]);
  diagnostics(result);
});

test("a zero ratio argument stays diagnostic-free and its environment is preserved", () => {
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: "0" });
  decisions(result, [false, false, false]);
  diagnostics(result);
});

// The SDK's own factory reports a missing or blank argument as an error and defaults the ratio to 1.
test.each([undefined, "", "   "])("missing or blank ratio argument %j keeps the SDK's defaulting error and samples at 1", arg => {
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: arg });
  decisions(result, [true, true, true]);
  expect(result.diagnostics).toContainEqual({ source: "otel", level: "error", message: "OTEL_TRACES_SAMPLER_ARG is blank, defaulting to 1." });
  diagnostics(result);
});

test("provider construction still delivers unrelated application OTel diagnostics", () => {
  const endpoint = "https://collector.invalid/sampler-fixture";
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", TEST_ENDPOINT: endpoint, OTEL_TRACES_SAMPLER: "always_off" });
  decisions(result, [false, false, false]);
  expect(result.warnings).toEqual([]);
  expect(result.diagnostics).toEqual([
    { source: "otel", level: "warn", message: "sampler fixture: before init warning" },
    { source: "otel", level: "error", message: "sampler fixture: before init error" },
    { source: "otel", level: "warn", message: `Using custom endpoint as full URL: ${endpoint}` },
    { source: "otel", level: "warn", message: "sampler fixture: after shutdown warning" },
    { source: "otel", level: "error", message: "sampler fixture: after shutdown error" },
  ]);
});

test("failed provider construction restores the sampler environment and application logger", () => {
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", TEST_REGISTER_FAILURE: "true", FI_SAMPLE_RATE: "1", OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: "not-a-ratio" });
  decisions(result, []);
  diagnostics(result, "[traceai:auto] Auto-instrumentation could not initialize; the application will continue.");
});

test("an explicit rate ignores invalid OTel settings without a sampler warning", () => {
  const result = probe({ FI_SAMPLE_RATE: "1", OTEL_TRACES_SAMPLER: "unsupported_sampler", OTEL_TRACES_SAMPLER_ARG: "not-a-ratio" });
  decisions(result, [true, true, true], [true, true, false]);
  expect(result.warnings).toEqual([]);
});

test("invalid sampler fallback survives a throwing warning logger", () => {
  const result = probe({ OTEL_TRACES_SAMPLER: "unsupported_sampler", TEST_THROW_WARNING: "true" });
  decisions(result, [true, true, false]);
  expect(result.warnings).toHaveLength(1);
});

test.each(["only", "supplied", "global"])("application-owned %s provider keeps its own sampler despite environment and explicit rate", mode => {
  const result = probe({ TEST_HOST: mode, TEST_HOST_SAMPLER: "off", FI_SAMPLE_RATE: "1", OTEL_TRACES_SAMPLER: "always_on" });
  decisions(result, [false, false, false]);
  expect(result.sameProvider).toBe(true);
  expect(result.hostCalls).toBe(3);
  expect(result.warnings).toEqual([]);
});

test("application-owned always-on provider ignores the agent's always_off setting", () => {
  const result = probe({ TEST_HOST: "only", OTEL_TRACES_SAMPLER: "always_off" });
  decisions(result, [true, true, true]);
  expect(result.sameProvider).toBe(true);
  expect(result.hostCalls).toBe(3);
  expect(result.warnings).toEqual([]);
});

test("application-owned provider does not parse invalid agent sampler settings", () => {
  const result = probe({ TEST_HOST: "only", OTEL_TRACES_SAMPLER: "unsupported_sampler" });
  decisions(result, [true, true, true]);
  expect(result.sameProvider).toBe(true);
  expect(result.warnings).toEqual([]);
});
