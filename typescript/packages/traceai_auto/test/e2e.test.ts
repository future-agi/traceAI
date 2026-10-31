import { createServer, Server } from "node:http";
import { spawn, spawnSync } from "node:child_process";
import { resolve } from "node:path";
import type { AddressInfo } from "node:net";

const root = resolve(__dirname, ".."), apps = resolve(__dirname, "apps");
const completion = { id: "chat-fixture", model: "gpt-fixture", choices: [{ index: 0, message: { role: "assistant", content: "hello world" }, finish_reason: "stop" }], usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 } };
const sse = [
  { id: "chat-fixture", model: "gpt-fixture", choices: [{ index: 0, delta: { role: "assistant", content: "hello " }, finish_reason: null }] },
  { id: "chat-fixture", model: "gpt-fixture", choices: [{ index: 0, delta: { content: "world" }, finish_reason: "stop" }], usage: completion.usage },
].map(x => `data: ${JSON.stringify(x)}\n\n`).join("") + "data: [DONE]\n\n";
let server: Server, url: string, collected: any[], requests: number;
function attrs(span: any): Record<string, any> {
  return Object.fromEntries((span.attributes ?? []).map((a: any) => [a.key, a.value.arrayValue ? a.value.arrayValue.values.map((x: any) => x.stringValue) : a.value.stringValue ?? a.value.intValue ?? a.value.doubleValue ?? a.value.boolValue]));
}
function allSpans(): any[] { return collected.flatMap(e => e.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []); }
function wireSpans(): any[] { return allSpans().filter(s => attrs(s)["traceai.capture.layer"] === "wire"); }
beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", c => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString();
      if (req.url === "/tracer/v1/traces") { collected.push(JSON.parse(body)); res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); return; }
      if (req.url === "/reject-collector") { res.writeHead(401, { "content-type": "application/json" }); res.end('{"error":"rejected"}'); return; }
      requests++;
      if (req.url === "/v1/audio/speech") { res.writeHead(200, { "content-type": "audio/wav" }); res.end(Buffer.alloc(32)); return; }
      if (req.url === "/v1/audio/transcriptions") { res.writeHead(200, { "content-type": "application/json" }); res.end('{"text":"private transcript"}'); return; }
      const data = JSON.parse(body);
      if (data.stream) {
        res.writeHead(200, { "content-type": "text/event-stream" });
        // Include splits inside UTF-8/data lines, rather than one complete event per chunk.
        res.write(sse.slice(0, 23)); setImmediate(() => { res.write(sse.slice(23, 84)); setImmediate(() => res.end(sse.slice(84))); });
      } else { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(completion)); }
    });
  });
  await new Promise<void>(r => server.listen(0, "127.0.0.1", r));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => { server.closeAllConnections(); await new Promise<void>(r => server.close(() => r())); });
beforeEach(() => { collected = []; requests = 0; });
async function child(app = "plain.cjs", args = ["--import", "@traceai/auto/register"], extra: Record<string, string | undefined> = {}) {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(env)) if (key.startsWith("FI_") || key.startsWith("OTEL_") || ["NODE_OPTIONS", "KIND", "MODE", "SUPPRESS"].includes(key)) delete env[key];
  Object.assign(env, { FI_API_KEY: "mock-fi-api-key", FI_SECRET_KEY: "mock-fi-secret-key", FI_BASE_URL: url, MOCK_URL: url, FI_PROJECT_NAME: "auto-test", FI_INSTRUMENTATIONS: "wire" }, extra);
  return new Promise<{ stdout: string; stderr: string }>((resolveResult, reject) => {
    const process = spawn(global.process.execPath, [...args, resolve(apps, app)], { cwd: root, env });
    let stdout = "", stderr = "";
    process.stdout.on("data", c => { stdout += c; }); process.stderr.on("data", c => { stderr += c; });
    const timer = setTimeout(() => { process.kill("SIGKILL"); reject(new Error(`Child timed out: ${stderr}`)); }, 20000);
    process.on("error", reject);
    process.on("close", code => { clearTimeout(timer); code === 0 ? resolveResult({ stdout, stderr }) : reject(new Error(`Child exited ${code}: ${stderr}\n${stdout}`)); });
  });
}
test("bootstrap performs no startup network calls", async () => {
  const output = await child("noop.cjs");
  expect(output.stdout.trim()).toBe("application ran"); expect(requests).toBe(0); expect(allSpans()).toHaveLength(0);
});
test("default all-integration bootstrap also fails open without startup network I/O", async () => {
  const output = await child("noop.cjs", undefined, { FI_INSTRUMENTATIONS: undefined });
  expect(output.stdout).toContain("application ran"); expect(requests).toBe(0); expect(allSpans()).toHaveLength(0);
});
test("doctor checks only on demand, reports collector acceptance and masks credentials", async () => {
  const output = await child("noop.cjs", [resolve(root, "dist/src/cli.js"), "doctor"]);
  const report = JSON.parse(output.stdout); expect(report.collector).toBe("accepted"); expect(allSpans()).toHaveLength(1);
  expect(report.config).toMatchObject({ apiKey: "***", secretKey: "***", projectType: "observe" });
  expect(output.stdout).not.toContain("mock-fi-api-key"); expect(output.stdout).not.toContain("mock-fi-secret-key");
});
test("collector rejection warns once without breaking the plain application", async () => {
  const output = await child("plain.cjs", undefined, { FI_BASE_URL: url + "/reject-collector" });
  expect(JSON.parse(output.stdout).status).toBe(200); expect(requests).toBe(1);
  expect(output.stderr.split("\n").filter(line => line.includes("[traceai:auto]"))).toHaveLength(1);
  expect(output.stderr).not.toContain("mock-fi-api-key");
});
test("real --import entry traces an unchanged plain app and preserves response metadata", async () => {
  const output = await child(); const result = JSON.parse(output.stdout);
  expect(Buffer.from(result.bytes, "base64").toString()).toBe(JSON.stringify(completion));
  expect(result).toMatchObject({ status: 200, bodyUsed: true, headersImmutable: true, url: url + "/v1/chat/completions" });
  expect(wireSpans()).toHaveLength(1); const span = wireSpans()[0];
  expect(span.name).toBe("chat gpt-fixture"); expect(span.kind).toBe(3); expect(span.status.code).toBe(1);
  expect(attrs(span)).toMatchObject({ "gen_ai.provider.name": "openai_compatible", "output.value": "hello world", "gen_ai.usage.total_tokens": 6 });
  expect(requests).toBe(1); expect(output.stderr).toBe("");
});
test("native Response clones keep URL/type, immutable headers and identical bytes", async () => {
  const output = await child("clone.cjs"), metadata = JSON.parse(output.stdout);
  expect(metadata).toHaveLength(3); for (const item of metadata) expect(item).toEqual({ url: url + "/v1/chat/completions", type: "basic", redirected: false, immutable: true, body: JSON.stringify(completion) });
  expect(wireSpans()).toHaveLength(1);
});
test.each(["stalled-flush", "rejected-flush", "stalled-decoder"])("shutdown invokes exporter cleanup despite %s", async mode => {
  const output = await child("lifecycle.cjs", [], { MODE: mode }), result = JSON.parse(output.stdout);
  expect(result.shutdownCalls).toBe(1); expect(result.keepaliveDestroyed).toBe(true); expect(result.elapsed).toBeLessThan(5200);
});
test.each(["rollback", "rollback-existing"])("S2 failed initialization rolls back hooks but never reconfigures (%s)", async mode => {
  const output = await child("lifecycle.cjs", [], { MODE: mode }), result = JSON.parse(output.stdout);
  expect(result).toMatchObject({ first: false, second: false, rolledBack: true, contextPreserved: true, shutdownCalls: 1 });
  if (mode === "rollback-existing") expect(result).toMatchObject({ processorsAfterFailure: 1, processorsAfterShutdown: 1 });
});
test("pre-existing once SIGTERM handler finishes asynchronous app-owned shutdown", async () => {
  const output = await child("lifecycle.cjs", [], { MODE: "sigterm-once" });
  expect(output.stdout.trim().split("\n")).toEqual(["app-handler-start", "app-handler-finished"]);
});
test("stream is delivered byte-for-byte unchanged and produces a completed span", async () => {
  const output = await child("plain.cjs", ["--import", "@traceai/auto/register"], { MODE: "stream" });
  expect(Buffer.from(JSON.parse(output.stdout).bytes, "base64").toString()).toBe(sse);
  expect(wireSpans()).toHaveLength(1); expect(attrs(wireSpans()[0])).toMatchObject({ "gen_ai.request.stream": true, "output.value": "hello world", "gen_ai.usage.total_tokens": 6 });
  expect(attrs(wireSpans()[0])["gen_ai.response.time_to_first_chunk"]).toBeGreaterThanOrEqual(0);
});
test("CJS -r and NODE_OPTIONS entry points work", async () => {
  await child("plain.cjs", ["-r", "@traceai/auto/register"]);
  expect(wireSpans()).toHaveLength(1);
  collected = [];
  await child("plain.cjs", [], { NODE_OPTIONS: "--import @traceai/auto/register" });
  expect(wireSpans()).toHaveLength(1);
});
test("missing credentials fails open with exactly one warning", async () => {
  const output = await child("plain.cjs", ["--import", "@traceai/auto/register"], { FI_API_KEY: "", FI_SECRET_KEY: "" });
  expect(requests).toBe(1); expect(allSpans()).toHaveLength(0);
  expect(output.stderr.split("\n").filter(Boolean)).toHaveLength(1);
  expect(output.stderr).toContain("auto-instrumentation is disabled");
});
test.each(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"])("deduplicates active FI-wrapped %s library spans", async kind => {
  await child("context.cjs", ["--import", "@traceai/auto/register"], { KIND: kind });
  expect(wireSpans()).toHaveLength(0); expect(allSpans()).toHaveLength(1); expect(requests).toBe(1);
});
test("CHAIN spans parent wire calls, including context attributes and explicit timing", async () => {
  await child("context.cjs");
  const parent = allSpans().find(s => s.name === "framework parent"), wire = wireSpans()[0];
  expect(wire.parentSpanId).toBe(parent.spanId); expect(wire.traceId).toBe(parent.traceId);
  // S6 treats session identifiers as credential fields, without losing ancestry or timing.
  expect(attrs(wire)).toMatchObject({ "session.id": "<redacted>", "user.id": "user-test" });
  expect(BigInt(wire.startTimeUnixNano)).toBeLessThan(BigInt(wire.endTimeUnixNano));
});
test("OTel suppression prevents wire capture", async () => { await child("context.cjs", undefined, { SUPPRESS: "true" }); expect(wireSpans()).toHaveLength(0); });
test.each([["openai.mjs", "--import"], ["openai.cjs", "-r"]])("real OpenAI SDK is patched through %s %s", async (app, flag) => {
  const output = await child(app, [flag, "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire" });
  expect(output.stdout).toContain("hello world");
  expect(wireSpans()).toHaveLength(0); expect(allSpans()).toHaveLength(1);
  const attributes = attrs(allSpans()[0]);
  expect(attributes["gen_ai.span.kind"] ?? attributes["fi.span.kind"]).toBe("LLM");
  const report = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(report.integrations.find((s: any) => s.name === "openai").status).toBe("instrumented");
});
test("disabled shared-target integrations never load or register hooks", async () => {
  const output = await child("hooks.cjs", ["-r", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,deepseek,wire", FI_DISABLED_INSTRUMENTATIONS: "deepseek" });
  const info = JSON.parse(output.stdout.trim());
  expect(info.deepseekLoaded).toBe(false); expect(info.openaiLoaded).toBe(true);
  expect(info.report.integrations.find((integration: any) => integration.name === "deepseek").status).toBe("disabled");
  expect(info.report.integrations.find((integration: any) => integration.name === "openai").status).toBe("instrumented");
  expect(allSpans()).toHaveLength(1); expect(wireSpans()).toHaveLength(0);
});
test("head sampling zero and OTEL_SDK_DISABLED disable export without changing app", async () => {
  await child("plain.cjs", undefined, { FI_SAMPLE_RATE: "0" }); expect(allSpans()).toHaveLength(0); expect(requests).toBe(1);
  await child("plain.cjs", undefined, { OTEL_SDK_DISABLED: "true" }); expect(allSpans()).toHaveLength(0); expect(requests).toBe(2);
});
test("coexists with an existing global SDK, keeps its exporter and signal handlers", async () => {
  const output = await child("plain.cjs", ["-r", resolve(apps, "existing-provider.cjs"), "--import", "@traceai/auto/register"]);
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  // The app's own handlers stay; the agent only adds a SIGTERM listener that flushes and defers to them.
  expect(info).toEqual({ existing: true, collected: 1, sigint: 1, sigterm: 2 });
  expect(wireSpans()).toHaveLength(1);
  const resources = collected.flatMap(e => e.resourceSpans).map(r => attrs({ attributes: r.resource.attributes }));
  expect(resources[0]).toMatchObject({ project_name: "auto-test", project_type: "observe", "service.name": "existing-app" });
});
test("SIGTERM flushes and keeps the default exit for an app without its own handler", async () => {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(env)) if (key.startsWith("FI_") || key.startsWith("OTEL_") || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { FI_API_KEY: "mock-fi-api-key", FI_SECRET_KEY: "mock-fi-secret-key", FI_BASE_URL: url, MOCK_URL: url, FI_INSTRUMENTATIONS: "wire" });
  const proc = spawn(global.process.execPath, ["--import", "@traceai/auto/register", resolve(apps, "sigterm.cjs")], { cwd: root, env });
  const timer = setTimeout(() => proc.kill("SIGKILL"), 15000);
  try {
    await new Promise<void>((r, reject) => { proc.stdout.on("data", c => { if (String(c).includes("ready")) r(); }); proc.once("exit", () => reject(new Error("Signal fixture exited before ready"))); });
    const closed = new Promise(r => proc.on("close", (_code, sig) => r(sig)));
    proc.kill("SIGTERM");
    expect(await closed).toBe("SIGTERM");
    expect(wireSpans()).toHaveLength(1);
  } finally { clearTimeout(timer); if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL"); }
});
test("incoming HTTP gets SERVER spans and nests AI calls without outgoing non-AI spans", async () => {
  await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http" });
  const server = allSpans().find(s => s.kind === 2), wire = wireSpans()[0];
  expect(server).toBeDefined(); expect(wire.parentSpanId).toBe(server.spanId);
  expect(allSpans()).toHaveLength(2);
});
test("wire attributes never record credential headers, known keys, or inline audio", async () => {
  await child("sensitive.cjs");
  expect(wireSpans()).toHaveLength(1); const data = JSON.stringify(wireSpans());
  for (const secret of ["arbitrary-provider-token", "mock-fi-api-key", "private-header-token", "c2VjcmV0YXVkaW8="]) expect(data).not.toContain(secret);
});
test.each([
  ["tts", "FI_HIDE_INPUTS", true], ["tts", "FI_HIDE_INPUT_TEXT", true], ["tts", "FI_HIDE_OUTPUTS", false], ["tts", "FI_HIDE_OUTPUT_TEXT", false],
  ["stt", "FI_HIDE_OUTPUTS", true], ["stt", "FI_HIDE_OUTPUT_TEXT", true], ["stt", "FI_HIDE_INPUTS", false], ["stt", "FI_HIDE_INPUT_TEXT", false],
])("audio transcript respects its content side (%s, %s)", async (mode, flag, hidden) => {
  await child("audio.cjs", undefined, { MODE: String(mode), [String(flag)]: "true" });
  expect(wireSpans()).toHaveLength(1);
  expect(attrs(wireSpans()[0])["gen_ai.audio.transcript"]).toBe(hidden ? "[REDACTED]" : "private transcript");
  if (hidden) expect(JSON.stringify(wireSpans())).not.toContain("private transcript");
});
test("wire capture privacy applies to flattened messages and raw input/output", async () => {
  await child("plain.cjs", undefined, { FI_HIDE_INPUT_MESSAGES: "true", FI_HIDE_OUTPUTS: "true" });
  const serialized = JSON.stringify(wireSpans());
  expect(serialized).not.toContain("hello private text"); expect(serialized).not.toContain("hello world");
  expect(serialized).not.toContain("mock-fi-api-key"); expect(serialized).not.toContain("mock-fi-secret-key");
});
test.each(["library-error", "library-hide", "owned-privacy", "wire-context-privacy"])("export privacy protects %s without changing app spans/errors", async mode => {
  const output = await child("review.cjs", [], { MODE: mode });
  expect(JSON.parse(output.stdout)).toMatchObject({ protected: true, applicationUnchanged: true, exported: 1 });
});
test("shutdown drains completed asynchronous decompression before detaching the FI processor", async () => {
  const output = await child("review.cjs", [], { MODE: "shutdown-decode" });
  expect(JSON.parse(output.stdout)).toEqual({ exported: 1, appSpans: 1, processorsDuringDrain: 2, processorsAfter: 1, output: "answer" });
});
test("shutdown preserves manual OpenAI patches and subsequent application-owned spans", async () => {
  const output = await child("review.cjs", [], { MODE: "manual-ownership" });
  expect(JSON.parse(output.stdout)).toEqual({ sameWrapper: true, wrapped: true, manualEnabled: true, appSpans: 2, requests: 2 });
});
test("SIGTERM awaits exporter flush before preexisting once and later process.exit handlers", async () => {
  const output = await child("review.cjs", [], { MODE: "sigterm-order" });
  expect(output.stdout.trim().split("\n")).toEqual(["flush-start", "flush-finished", "application-once", "application-handler"]);
});
test.each(["X-API-Key", "x-api-key", "X-aPi-KeY"])("SDK error export removes case-insensitive request credential %s while preserving app errors", async name => {
  const output = await child("defects.cjs", [], { MODE: "library-header", HEADER_NAME: name });
  expect(JSON.parse(output.stdout)).toEqual({ protected: true, applicationUnchanged: true, exported: 1, requests: 1 });
});
test.each(["library-audio", "library-bracket-audio", "library-truncated-audio"])("SDK nested audio errors are scrubbed in FI status and exception events only (%s)", async mode => {
  const output = await child("defects.cjs", [], { MODE: mode });
  expect(JSON.parse(output.stdout)).toEqual({ protected: true, applicationUnchanged: true, exported: 1, requests: 1 });
});
test.each(["loader-control", "loader-retry", "loader-retry-twice"])("S2 immutable ESM loader keeps first-init targets after later attempts (%s)", async mode => {
  const output = await child("defects.cjs", [], { MODE: mode });
  const initial = mode === "loader-control";
  expect(JSON.parse(output.stdout)).toEqual({ failed: initial ? [] : mode === "loader-retry" ? [false] : [false, false], active: initial, serverSpans: initial ? 1 : 0, status: initial ? "instrumented" : "disabled" });
});
test("real OTel 1.30.1 provider exports parent/child through production OTLP without mutating app spans", async () => {
  const output = await child("defects.cjs", [], { MODE: "legacy-real" });
  expect(JSON.parse(output.stdout)).toEqual({ sameProvider: true, appSpans: 2, collector: "accepted" });
  expect(allSpans()).toHaveLength(2);
  const parent = allSpans().find(span => span.name === "legacy-parent"), childSpan = allSpans().find(span => span.name === "legacy-child");
  expect(childSpan.parentSpanId).toBe(parent.spanId); expect(childSpan.traceId).toBe(parent.traceId);
  expect(collected[0].resourceSpans[0].scopeSpans[0].scope).toMatchObject({ name: "legacy-app", version: "1.2.3" });
});
test.each(["--import", "-r"])("pure ESM incoming HTTP extracts remote parent with only wire,http enabled (%s)", async flag => {
  const output = await child("incoming.mjs", [flag, "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "wire,http", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-01" });
  expect(JSON.parse(output.stdout)).toMatchObject({ name: "http", status: "instrumented" });
  const incoming = allSpans().filter(span => span.kind === 2), wire = wireSpans()[0];
  expect(incoming).toHaveLength(1); expect(allSpans()).toHaveLength(2);
  expect(incoming[0].traceId).toBe("11111111111111111111111111111111"); expect(incoming[0].parentSpanId).toBe("2222222222222222");
  expect(wire.traceId).toBe(incoming[0].traceId); expect(wire.parentSpanId).toBe(incoming[0].spanId);
});
test("shutdown makes an owned SDK wrapper inert even underneath an application's outer wrapper", async () => {
  const output = await child("defects.cjs", [], { MODE: "library-ownership" });
  expect(JSON.parse(output.stdout)).toEqual({ outerPreserved: true, appSpans: 1, fiSpans: 1, calls: 2, requests: 2, active: false });
});
test("public shutdown makes retained fetch wrapper inert and preserves passthrough promise/argument/receiver identity", async () => {
  const output = await child("defects.cjs", [], { MODE: "fetch-ownership" });
  expect(JSON.parse(output.stdout)).toEqual({ outerPreserved: true, appSpans: 0, fiSpans: 0, identityPreserved: true, active: false });
});
test("two real SIGTERMs queued behind flush consume a once handler then restore default termination", async () => {
  const proc = spawn(global.process.execPath, [resolve(apps, "defects.cjs")], { cwd: root, env: { ...process.env, NODE_OPTIONS: "", MODE: "queued-signals" }, stdio: ["ignore", "pipe", "pipe", "ipc"] });
  let stderr = ""; const messages: any[] = [];
  proc.stderr!.on("data", chunk => { stderr += chunk; });
  const timer = setTimeout(() => proc.kill("SIGKILL"), 15000);
  const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveExit, reject) => {
    proc.once("error", reject); proc.once("close", (code, signal) => resolveExit({ code, signal }));
  });
  proc.on("message", (message: any) => {
    messages.push(message);
    if (message.type === "ready" || message.type === "flushing") proc.kill("SIGTERM");
    if (message.type === "queued") proc.send("release");
  });
  try {
    expect(await closed).toEqual({ code: null, signal: "SIGTERM" }); expect(stderr).toBe("");
    expect(messages.map(message => message.type)).toEqual(["ready", "flushing", "queued", "handler", "second-flush"]);
    expect(messages.at(-1)).toEqual({ type: "second-flush", flushes: 2, calls: 1 });
  } finally { clearTimeout(timer); if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL"); }
});
test("S1 real provider forwards sanitized app/resource/scope/link/tracestate snapshots only to FI", async () => {
  const output = await child("round4.cjs", [], { MODE: "export-surfaces" });
  expect(JSON.parse(output.stdout)).toEqual({ protected: true, unchanged: true, exported: 1 });
});
test.each(["body", "api-key", "query", "large-body"])("S1 native fetch retains %s request credentials for echoed HTTP errors", async source => {
  const output = await child("round4.cjs", [], { MODE: "request-credentials", CREDENTIAL_SOURCE: source, CREDENTIAL_LAYER: "wire" });
  expect(JSON.parse(output.stdout)).toEqual({ protected: true, applicationUnchanged: true, exported: 1 });
});
test.each(["body", "api-key", "query"])("S1 real OpenAI %s credentials are scrubbed only from FI span snapshots", async source => {
  const output = await child("round4.cjs", [], { MODE: "request-credentials", CREDENTIAL_SOURCE: source, CREDENTIAL_LAYER: "library" });
  expect(JSON.parse(output.stdout)).toEqual({ protected: true, applicationUnchanged: true, exported: 1 });
});
test("S2 repeated init keeps the same provider/configuration/hooks and emits one diagnostic", async () => {
  const output = await child("round4.cjs", [], { MODE: "init-once" });
  expect(JSON.parse(output.stdout)).toEqual({ sameProvider: true, oneWarning: true, unchangedHooks: true, exported: 1 });
});
test("S4 real fetch skips eight intermediate native spans covered by the model ancestor", async () => {
  const output = await child("round4.cjs", [], { MODE: "dedup-native" });
  expect(JSON.parse(output.stdout)).toEqual({ nativeSpans: 8, modelSpans: 1, wireSpans: 0 }); expect(requests).toBe(1);
});
test.each(["agents", "cursor", "cursor-outer"])("round3 real %s SDK preserves application telemetry and removes owned non-shimmer patches", async mode => {
  const output = await child("round4.cjs", [], { MODE: mode });
  expect(JSON.parse(output.stdout)).toEqual({ applicationTelemetryPreserved: true, ownedPatchRemoved: true });
});
test.each(["present", "absent", "throw"])("optional Vercel/Mastra/Genkit enable registry receives selected destination and fails open (%s)", async entry => {
  const output = await child("round4.cjs", [], { MODE: "registry", ENABLE_MODE: entry });
  const status = entry === "present" ? "instrumented" : entry === "absent" ? "manual" : "failed";
  expect(JSON.parse(output.stdout)).toEqual({ statuses: [status, status, status], selectedDestination: true });
});
test.each(["fetch-await", "fetch-unhandled", "read-await", "read-unhandled", "cancel-unhandled", "fetch-fatal", "read-fatal"])("R4-6 Node %s rejection lifecycle/exit matches an uninstrumented baseline", mode => {
  const outcomes = ["0", "1"].map(agent => spawnSync(process.execPath, [resolve(apps, "parity.cjs")], { cwd: root, env: { ...process.env, NODE_OPTIONS: "", OTEL_SDK_DISABLED: "false", FI_SAMPLE_RATE: "1", MODE: mode, AGENT: agent }, encoding: "utf8", timeout: 10000 }));
  for (const result of outcomes) expect(result.error).toBeUndefined();
  expect(outcomes[1].status).toBe(outcomes[0].status); expect(outcomes[1].signal).toBe(outcomes[0].signal);
  if (mode.endsWith("fatal")) { expect(outcomes[0].status).toBe(1); expect(outcomes[1].stderr).toContain("parity transport failure"); }
  else { expect(outcomes[0].status).toBe(0); expect(outcomes[1].stderr).toBe(""); expect(outcomes[1].stdout).toBe(outcomes[0].stdout); }
});
test("an application-installed propagator remains unchanged", async () => {
  const output = await child("review.cjs", [], { MODE: "propagator-preserved" });
  expect(JSON.parse(output.stdout)).toEqual({ preserved: true });
});
test("standalone incoming HTTP extracts the remote W3C parent and preserves the trace for AI children", async () => {
  await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-01" });
  const serverSpan = allSpans().find(s => s.kind === 2), wire = wireSpans()[0];
  expect(serverSpan.traceId).toBe("11111111111111111111111111111111"); expect(serverSpan.parentSpanId).toBe("2222222222222222");
  expect(wire.traceId).toBe(serverSpan.traceId); expect(wire.parentSpanId).toBe(serverSpan.spanId);
});
