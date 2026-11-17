import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

interface Snapshot {
  spans: { name: string; recording: boolean; sampled: boolean }[];
  exported: { name: string; sampled: boolean; attributes: Record<string, unknown>; traceState?: string }[];
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
  expect(result.exported.map(({ name, sampled }) => ({ name, sampled }))).toEqual(result.spans.filter(span => span.recording).map(({ name, sampled }) => ({ name, sampled })));
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

// Assert the fallback, not the SDK's version-dependent diagnostic wording.
test.each([undefined, "", "   "])("missing or blank ratio argument %j samples at 1", arg => {
  const result = probe({ TEST_OTEL_DIAGNOSTICS: "true", OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: arg });
  decisions(result, [true, true, true]);
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

const ratioKey = "fi.sampling.ratio";
const keptTraceId = "00000000000000000000000000000001";
const ratioEnv = { OTEL_TRACES_SAMPLER: "parentbased_traceidratio", OTEL_TRACES_SAMPLER_ARG: "0.25", TEST_TRACE_ID: keptTraceId };
const exportedSpan = (result: Snapshot, name: string) => result.exported.find(span => span.name === name)!;
// The existing export privacy boundary may reorder vendor members. Compare
// membership here; sampler unit tests exercise the actual propagated state.
const expectTraceState = (actual: string | undefined, expected: string | undefined) =>
  expect(actual?.split(",").sort()).toEqual(expected?.split(",").sort());

test("agent-created parentbased ratio stamps a kept root and its local child", () => {
  const result = probe({ ...ratioEnv, TEST_LOCAL_CHILD: "true" });
  decisions(result, [true, true, false, true]);
  for (const name of ["root", "local-child"]) {
    expect(exportedSpan(result, name).attributes[ratioKey]).toBe(0.25);
    expect(exportedSpan(result, name).traceState).toBe("fi=r:0.25");
  }
  expect(exportedSpan(result, "sampled-parent").attributes[ratioKey]).toBeUndefined();
});

test.each(["true", "false"])("sampled parent metadata is inherited with other vendors intact (remote=%s)", remote => {
  const result = probe({ ...ratioEnv, TEST_REMOTE: remote, TEST_TRACESTATE: "vendor=opaque,fi=r:0.5" });
  decisions(result, [true, true, false]);
  const span = exportedSpan(result, "sampled-parent");
  expect(span.attributes[ratioKey]).toBe(0.5);
  expectTraceState(span.traceState, "vendor=opaque,fi=r:0.5");
});

test.each(["vendor=opaque", ...["2", "abc", "NaN", "Infinity", "0", "-0.1"].map(value => `vendor=opaque,fi=r:${value}`)])("parent tracestate %s without a valid ratio does not invent metadata", traceState => {
  const result = probe({ ...ratioEnv, TEST_TRACESTATE: traceState });
  const span = exportedSpan(result, "sampled-parent");
  expect(span.attributes[ratioKey]).toBeUndefined();
  expectTraceState(span.traceState, traceState);
});

test("plain ratio uses its local probability for every kept span and replaces fi", () => {
  const result = probe({ ...ratioEnv, OTEL_TRACES_SAMPLER: "traceidratio", TEST_TRACESTATE: "vendor=opaque,fi=r:0.5" });
  decisions(result, [true, true, true]);
  for (const span of result.exported) {
    expect(span.attributes[ratioKey]).toBe(0.25);
    expectTraceState(span.traceState, span.name === "root" ? "fi=r:0.25" : "fi=r:0.25,vendor=opaque");
  }
});

test.each([
  {},
  { OTEL_TRACES_SAMPLER: "always_on" },
  { OTEL_TRACES_SAMPLER: "parentbased_always_on" },
  { OTEL_TRACES_SAMPLER: "always_off" },
  { OTEL_TRACES_SAMPLER: "parentbased_always_off" },
  { OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: "1" },
  { OTEL_TRACES_SAMPLER: "parentbased_traceidratio", OTEL_TRACES_SAMPLER_ARG: "1" },
  { FI_SAMPLE_RATE: "1" },
  { OTEL_TRACES_SAMPLER: "unsupported_sampler" },
  { OTEL_TRACES_SAMPLER: "traceidratio", OTEL_TRACES_SAMPLER_ARG: "NaN" },
])("non-ratio/disabled/fallback selection %j never adds or inherits ratio attributes", env => {
  const result = probe({ ...env, TEST_TRACESTATE: "vendor=opaque,fi=r:0.5" });
  for (const span of result.exported) {
    expect(span.attributes[ratioKey]).toBeUndefined();
    // SDK propagation is unchanged; no fi entry is added or replaced by the agent.
    expectTraceState(span.traceState, span.name === "root" ? undefined : "vendor=opaque,fi=r:0.5");
  }
});

test.each(["traceidratio", "parentbased_traceidratio"])("%s dropped roots are not exported or stamped", sampler => {
  const result = probe({ ...ratioEnv, OTEL_TRACES_SAMPLER: sampler, TEST_TRACE_ID: "ffffffff000000000000000000000000", TEST_TRACESTATE: "fi=r:0.5" });
  decisions(result, sampler === "traceidratio" ? [false, false, false] : [false, true, false]);
  expect(result.exported.some(span => span.name === "root")).toBe(false);
});

test.each(["only", "supplied", "global"])("application-owned %s provider is never stamped or wrapped", mode => {
  const result = probe({ ...ratioEnv, TEST_HOST: mode, FI_SAMPLE_RATE: "0.25", TEST_TRACESTATE: "vendor=opaque,fi=r:0.5" });
  decisions(result, [true, true, true]);
  expect(result.sameProvider).toBe(true);
  expect(result.hostCalls).toBe(3);
  for (const span of result.exported) {
    expect(span.attributes[ratioKey]).toBeUndefined();
    expectTraceState(span.traceState, span.name === "root" ? undefined : "vendor=opaque,fi=r:0.5");
  }
});

test.each([
  ["environment", { FI_SAMPLE_RATE: "0.25" }, undefined],
  ["code option", { TEST_SAMPLE_RATE: "0.25" }, undefined],
  ["config file", {}, 0.25],
] as const)("explicit %s rate stamps only sampled decisions with the local ratio", (_source, env, fileRate) => {
  const result = probe({ ...ratioEnv, ...env, OTEL_TRACES_SAMPLER: "always_off", TEST_TRACESTATE: "vendor=opaque,fi=r:0.5" }, fileRate);
  decisions(result, [true, true, true], [true, true, false]);
  expect(exportedSpan(result, "root").attributes[ratioKey]).toBe(0.25);
  expect(exportedSpan(result, "root").traceState).toBe("fi=r:0.25");
  expect(exportedSpan(result, "sampled-parent").attributes[ratioKey]).toBe(0.25);
  expectTraceState(exportedSpan(result, "sampled-parent").traceState, "fi=r:0.25,vendor=opaque");
  expect(exportedSpan(result, "unsampled-parent").attributes[ratioKey]).toBeUndefined();
  expectTraceState(exportedSpan(result, "unsampled-parent").traceState, "vendor=opaque,fi=r:0.5");
});

describe.each(["false", "true"])("saturated admission (auto FITracer=%s)", auto => {
  test.each([1, 16, 128])("sampled metadata survives a full %s-slot native budget and export privacy", limit => {
    const result = probe({ ...ratioEnv, OTEL_TRACES_SAMPLER: "traceidratio", TEST_AUTO_TRACER: auto,
      OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: String(limit), TEST_ATTRIBUTES_COUNT: String(limit) });
    expect(result.spans.map(span => span.sampled)).toEqual([true, true, true]);
    expect(result.exported).toHaveLength(3);
    for (const span of result.exported) {
      expect(span.attributes[ratioKey]).toBe(0.25);
      expect(span.attributes["traceai.capture.host_data_unknown"]).toBe(true);
      // Existing fail-closed export semantics still hide arbitrary user content.
      expect(Object.values(span.attributes)).not.toContain("value");
    }
  });

  test("inherited parent ratio survives a full budget, while unsampled and rejected spans remain absent", () => {
    const result = probe({ ...ratioEnv, TEST_AUTO_TRACER: auto, TEST_TRACE_ID: "ffffffff000000000000000000000000",
      TEST_TRACESTATE: "fi=r:0.5", OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: "1", TEST_ATTRIBUTES_COUNT: "1" });
    expect(result.spans.map(span => span.recording)).toEqual([false, true, false]);
    expect(result.exported).toHaveLength(1);
    expect(result.exported[0].attributes[ratioKey]).toBe(0.5);
  });

  test("record-only and missing parent ratios do not displace native attributes", () => {
    const env = { ...ratioEnv, TEST_AUTO_TRACER: auto, OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: "1", TEST_ATTRIBUTES_COUNT: "1" };
    const parentBased = probe(env);
    expect(parentBased.exported[0].attributes[ratioKey]).toBe(0.25);
    expect(parentBased.exported[1].attributes).toEqual({ "existing.0": "value" });
    const explicit = probe({ ...env, FI_SAMPLE_RATE: "0.25" });
    expect(explicit.exported[2].sampled).toBe(false);
    expect(explicit.exported[2].attributes).toEqual({ "existing.0": "value" });
  });

  test.each([undefined, "always_on", "traceidratio"])("unweighted %s keeps the full native budget", sampler => {
    const result = probe({ TEST_AUTO_TRACER: auto, OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: "1", TEST_ATTRIBUTES_COUNT: "1",
      OTEL_TRACES_SAMPLER: sampler, OTEL_TRACES_SAMPLER_ARG: "1" });
    for (const span of result.exported) expect(span.attributes).toEqual({ "existing.0": "value" });
  });

  test.each(["only", "supplied", "global"])("borrowed %s provider keeps the full native budget", host => {
    const result = probe({ ...ratioEnv, TEST_HOST: host, TEST_AUTO_TRACER: auto,
      OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: "1", TEST_ATTRIBUTES_COUNT: "1" });
    expect(result.sameProvider).toBe(true);
    for (const span of result.exported) expect(span.attributes).toEqual({ "existing.0": "value" });
  });
});

test("a zero native attribute budget remains authoritative", () => {
  const result = probe({ ...ratioEnv, OTEL_TRACES_SAMPLER: "traceidratio", OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: "0", TEST_ATTRIBUTES_COUNT: "1" });
  expect(result.exported).toHaveLength(3);
  for (const span of result.exported) expect(span.attributes[ratioKey]).toBeUndefined();
});

test("explicit ratio still drops a sampled parent's child when its trace ID is rejected", () => {
  const result = probe({ ...ratioEnv, FI_SAMPLE_RATE: "0.25", TEST_TRACE_ID: "ffffffff000000000000000000000000", TEST_TRACESTATE: "fi=r:0.5" });
  decisions(result, [false, false, false]);
});
