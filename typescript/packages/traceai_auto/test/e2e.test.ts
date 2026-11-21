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
      // A plain, non-AI application route: it answers with the headers it received.
      if (req.url === "/echo-headers") { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(req.headers)); return; }
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
  // Children run in the package root; a developer's futureagi.toml must not change the sampler.
  Object.assign(env, { FI_CONFIG_FILE: resolve(apps, "empty.toml"), FI_API_KEY: "mock-fi-api-key", FI_SECRET_KEY: "mock-fi-secret-key", FI_BASE_URL: url, MOCK_URL: url, FI_PROJECT_NAME: "auto-test", FI_INSTRUMENTATIONS: "wire" }, extra);
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
test.each(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"])("an application's own active FI-wrapped %s span does not hide the call from Future AGI", async kind => {
  // The application's model span stays with its own provider, so the wire layer
  // does not yield to it: Future AGI receives the call once, as a wire span.
  const output = await child("context.cjs", ["--import", "@traceai/auto/register"], { KIND: kind });
  const parent = JSON.parse(output.stdout.trim());
  expect(wireSpans()).toHaveLength(1); expect(allSpans()).toHaveLength(1); expect(requests).toBe(1);
  expect(wireSpans()[0].parentSpanId).toBe(parent.spanId);
});
test("CHAIN spans parent wire calls, including context attributes and explicit timing", async () => {
  const output = await child("context.cjs");
  const parent = JSON.parse(output.stdout.trim()), wire = wireSpans()[0];
  // Only the AI span reaches Future AGI; it parents under the application's active span.
  expect(allSpans()).toHaveLength(1);
  expect(wire.parentSpanId).toBe(parent.spanId); expect(wire.traceId).toBe(parent.traceId);
  // Session and user ids from the context are exported as set, without losing ancestry or timing.
  expect(attrs(wire)).toMatchObject({ "session.id": "session-test", "user.id": "user-test" });
  expect(BigInt(wire.startTimeUnixNano)).toBeLessThan(BigInt(wire.endTimeUnixNano));
});
test("OTel suppression prevents wire capture", async () => { await child("context.cjs", undefined, { SUPPRESS: "true" }); expect(wireSpans()).toHaveLength(0); });
test("without application OpenTelemetry, the agent's context helpers set session, user, metadata and tags on AI spans", async () => {
  const output = await child("context-helpers.cjs");
  expect(wireSpans()).toHaveLength(2);
  const spans = wireSpans().map(attrs), scoped = spans.find(a => a["session.id"] !== undefined)!, tagged = spans.find(a => a !== scoped)!;
  expect(scoped).toMatchObject({ "session.id": "session-test", "user.id": "user-test" });
  expect(JSON.parse(scoped.metadata)).toMatchObject({ tenant: "tenant-test" });
  expect(JSON.stringify(scoped)).not.toContain("tag-test");
  expect(JSON.stringify(tagged)).toContain("tag-test");
  expect(tagged["session.id"]).toBeUndefined();
  // The helpers register no context manager.
  expect(JSON.parse(output.stdout.trim())).toEqual({ context: "NoopContextManager" });
});
test("without application OpenTelemetry, the agent's suppression helper prevents wire capture", async () => {
  await child("context-helpers.cjs", undefined, { SUPPRESS: "true" });
  expect(wireSpans()).toHaveLength(0); expect(requests).toBe(1);
});
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
test("keeps an application provider registered first untouched and sends AI spans on its own provider", async () => {
  const output = await child("plain.cjs", ["-r", resolve(apps, "existing-provider.cjs"), "--import", "@traceai/auto/register"]);
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  // The app's provider and handlers stay; its exporter receives none of the agent's
  // spans. The agent only adds a SIGTERM listener that defers to the app's handlers.
  expect(info).toEqual({ existing: true, collected: 0, sigint: 1, sigterm: 2 });
  expect(wireSpans()).toHaveLength(1); expect(allSpans()).toHaveLength(1);
  const resources = collected.flatMap(e => e.resourceSpans).map(r => attrs({ attributes: r.resource.attributes }));
  expect(resources[0]).toMatchObject({ project_name: "auto-test", project_type: "observe" });
  expect(resources[0]["service.name"]).not.toBe("existing-app");
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
test("FI_HIDE_INPUTS=on and an unrecognised FI_HIDE_OUTPUTS value hide captured content in what Future AGI receives", async () => {
  const output = await child("plain.cjs", undefined, { FI_HIDE_INPUTS: "on", FI_HIDE_OUTPUTS: "maybe-later" });
  expect(wireSpans()).toHaveLength(1);
  const serialized = JSON.stringify(allSpans());
  expect(serialized).not.toContain("hello private text"); expect(serialized).not.toContain("hello world");
  // The warning names the variable and never repeats its value.
  expect(output.stderr).toContain("FI_HIDE_OUTPUTS");
  expect(output.stderr).not.toContain("maybe-later");
});
test.each(["library-error", "library-error-positive-cap", "library-hide", "owned-privacy", "wire-context-privacy"])("export privacy protects %s without changing app spans/errors", async mode => {
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
test.each(["--import", "-r"])("pure ESM incoming HTTP joins the remote parent through the application's propagator with only wire,http enabled (%s)", async flag => {
  // The application registers a context manager and propagator, but no tracer provider.
  const output = await child("incoming.mjs", ["-r", resolve(apps, "app-otel.cjs"), flag, "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "wire,http", APP_TRACE: "off", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-01" });
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
  expect(JSON.parse(output.stdout)).toEqual({ statuses: [status, "manual", status], selectedDestination: true, mastraReason: "Mastra zero-code bridge follows after 11-11; use createFIObservability / createFIMastraExporter from @traceai/mastra", mastraLoads: 0 });
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
test("incoming HTTP joins the remote W3C parent through the application's propagator and keeps the trace for AI children", async () => {
  // The agent registers no propagator; the application's own one extracts the parent.
  // The application has no tracer provider: beside one, the agent leaves HTTP to it.
  await child("incoming.cjs", ["-r", resolve(apps, "app-otel.cjs"), "--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "wire,http", APP_TRACE: "off", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-01" });
  const serverSpan = allSpans().find(s => s.kind === 2), wire = wireSpans()[0];
  expect(serverSpan.traceId).toBe("11111111111111111111111111111111"); expect(serverSpan.parentSpanId).toBe("2222222222222222");
  expect(wire.traceId).toBe(serverSpan.traceId); expect(wire.parentSpanId).toBe(serverSpan.spanId);
  expect(allSpans()).toHaveLength(2);
});
test("without application OpenTelemetry, incoming HTTP joins the remote W3C parent through the agent's private propagator and registers none", async () => {
  const output = await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-01", PRINT_GLOBALS: "true" });
  const serverSpan = allSpans().find(s => s.kind === 2), wire = wireSpans()[0];
  expect(serverSpan.traceId).toBe("11111111111111111111111111111111"); expect(serverSpan.parentSpanId).toBe("2222222222222222");
  expect(wire.traceId).toBe(serverSpan.traceId); expect(wire.parentSpanId).toBe(serverSpan.spanId);
  expect(allSpans()).toHaveLength(2);
  expect(JSON.parse(output.stdout.trim())).toEqual({ context: "NoopContextManager", propagation: "NoopTextMapPropagator" });
});
test("an application sampler that drops its own spans does not drop AI spans, which keep the application's parent ids", async () => {
  // Explicit rate retains legacy AI-only sampling; an unset rate now follows ParentBased.
  const output = await child("own-provider.cjs", ["--import", "@traceai/auto/register"], { MODE: "app-after", APP_SAMPLER: "off", FI_SAMPLE_RATE: "1" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(info.appSpans).toEqual([]);
  expect(allSpans()).toHaveLength(1); expect(wireSpans()).toHaveLength(1);
  expect(wireSpans()[0].traceId).toBe(info.work.traceId); expect(wireSpans()[0].parentSpanId).toBe(info.work.spanId);
});
test("the default parent-based sampler exports neither the unsampled incoming request nor its AI call", async () => {
  await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-00" });
  expect(requests).toBe(1);
  expect(allSpans()).toEqual([]);
});
test("an explicit full sample rate keeps the SERVER span and AI call for an unsampled incoming traceparent", async () => {
  await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http", FI_SAMPLE_RATE: "1", REMOTE_PARENT: "00-11111111111111111111111111111111-2222222222222222-00" });
  const serverSpan = allSpans().find(s => s.kind === 2), wire = wireSpans()[0];
  expect(allSpans()).toHaveLength(2);
  expect(serverSpan.traceId).toBe("11111111111111111111111111111111"); expect(serverSpan.parentSpanId).toBe("2222222222222222");
  expect(wire.parentSpanId).toBe(serverSpan.spanId);
});
test("an application NodeSDK started after the preload registers its globals and keeps its spans; Future AGI receives only the AI span", async () => {
  const output = await child("own-provider.cjs", ["--import", "@traceai/auto/register"], { MODE: "app-after" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(info.registered).toEqual({ trace: true, context: true, propagation: true, metrics: true });
  expect(info.appSpans).toEqual([{ name: "app-work", kind: 0, layer: null }]);
  expect(allSpans()).toHaveLength(1); expect(wireSpans()).toHaveLength(1);
  // The AI span still parents under the application's active span.
  expect(wireSpans()[0].traceId).toBe(info.work.traceId); expect(wireSpans()[0].parentSpanId).toBe(info.work.spanId);
});
test("an application span created inside an AI call keeps the AI trace id and has a parent that only Future AGI receives", async () => {
  // The application starts its own SDK, then makes one OpenAI call whose fetch creates
  // a span of its own. The control runs the same application without the preload.
  const run = async (args: string[]) => JSON.parse((await child("inside-ai-call.cjs", args, { FI_INSTRUMENTATIONS: "openai" })).stdout.trim().split("\n").at(-1)!).appSpans;
  // Each span the application's backend received, by name, with its parent's name as that backend shows it.
  const shape = (spans: any[]) => spans.map(span => ({ name: span.name, parent: span.parentSpanId === null ? null : spans.find(other => other.spanId === span.parentSpanId)?.name ?? "not received" }));
  const outside = (spans: any[]) => shape(spans).filter(span => span.name !== "app-provider-request");
  const control = await run([]);
  expect(allSpans()).toHaveLength(0);
  expect(shape(control)).toEqual([
    { name: "app-before", parent: "app-request" }, { name: "app-provider-request", parent: "app-request" },
    { name: "app-after", parent: "app-request" }, { name: "app-request", parent: null },
  ]);
  const app = await run(["--import", "@traceai/auto/register"]);
  expect(requests).toBe(2);
  // Spans created outside the AI call reach the application's backend as without the agent: same names, parents and count.
  expect(app).toHaveLength(control.length);
  expect(outside(app)).toEqual(outside(control));
  // Future AGI receives the AI span and none of the application's spans.
  expect(allSpans()).toHaveLength(1);
  const ai = allSpans()[0], aiAttributes = attrs(ai);
  expect(aiAttributes["gen_ai.span.kind"] ?? aiAttributes["fi.span.kind"]).toBe("LLM");
  for (const span of app) { expect(ai.spanId).not.toBe(span.spanId); expect(ai.name).not.toBe(span.name); }
  const request = app.find((span: any) => span.name === "app-request"), inside = app.find((span: any) => span.name === "app-provider-request");
  expect(ai.traceId).toBe(request.traceId); expect(ai.parentSpanId).toBe(request.spanId);
  // The span created inside the call keeps the AI span's trace id. Its parent is the AI span,
  // which the application's backend never received.
  expect(inside.traceId).toBe(ai.traceId); expect(inside.parentSpanId).toBe(ai.spanId);
  expect(app.map((span: any) => span.spanId)).not.toContain(inside.parentSpanId);
});
test("under a parent-based application sampler, a trace the application did not sample stays unsampled inside an AI call, and Future AGI still receives the AI span", async () => {
  // ParentBased(TraceIdRatioBased(0)): the application samples no new trace, and its child
  // spans follow their parent's sampled flag. The control runs without the preload.
  const run = async (args: string[]) => JSON.parse((await child("inside-ai-call.cjs", args, { FI_INSTRUMENTATIONS: "openai", APP_SAMPLER: "parentbased-zero", FI_SAMPLE_RATE: "1" })).stdout.trim().split("\n").at(-1)!);
  const sampledFlag = (traceparent: string | null) => traceparent?.split("-")[3];
  const control = await run([]);
  expect(allSpans()).toHaveLength(0);
  expect(control.appSpans).toEqual([]);
  expect(sampledFlag(control.providerTraceparent)).toBe("00");
  const app = await run(["--import", "@traceai/auto/register"]);
  expect(requests).toBe(2);
  // The application's backend receives what it receives without the agent: nothing for this trace.
  expect(app.appSpans).toEqual(control.appSpans);
  // Its own propagator gives the provider request the same sampled flag as without the agent.
  expect(sampledFlag(app.providerTraceparent)).toBe(sampledFlag(control.providerTraceparent));
  // Future AGI still receives the AI span, with the application's trace id and parent id.
  expect(allSpans()).toHaveLength(1);
  const ai = allSpans()[0], aiAttributes = attrs(ai);
  expect(aiAttributes["gen_ai.span.kind"] ?? aiAttributes["fi.span.kind"]).toBe("LLM");
  expect(ai.traceId).toBe(app.request.traceId); expect(ai.parentSpanId).toBe(app.request.spanId);
});
test("an AI call whose request runs inside a span from an application with no tracer provider reaches Future AGI once", async () => {
  // APP_TRACE=off: a context manager and propagator, but no tracer provider. The span the
  // application's fetch starts does not record and carries the AI span's own span context.
  const output = await child("inside-ai-call.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire", APP_TRACE: "off" });
  const app = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(app.appSpans).toEqual([]);
  expect(requests).toBe(1);
  expect(wireSpans()).toHaveLength(0);
  expect(allSpans()).toHaveLength(1);
  const ai = allSpans()[0], aiAttributes = attrs(ai);
  expect(aiAttributes["gen_ai.span.kind"] ?? aiAttributes["fi.span.kind"]).toBe("LLM");
  // The application's span inside the call names the AI span in its traceparent.
  expect(app.providerTraceparent.split("-")[1]).toBe(ai.traceId); expect(app.providerTraceparent.split("-")[2]).toBe(ai.spanId);
});
test("an AI call whose request runs inside an application span reaches Future AGI once, not again as a wire span", async () => {
  // The same application with wire capture on. Its own span sits between the AI span and
  // the provider request. The AI span reaches Future AGI, so the wire layer yields to it.
  const output = await child("inside-ai-call.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire" });
  const app = JSON.parse(output.stdout.trim().split("\n").at(-1)!).appSpans;
  expect(requests).toBe(1);
  expect(wireSpans()).toHaveLength(0);
  expect(allSpans()).toHaveLength(1);
  const ai = allSpans()[0], aiAttributes = attrs(ai);
  expect(aiAttributes["gen_ai.span.kind"] ?? aiAttributes["fi.span.kind"]).toBe("LLM");
  const inside = app.find((span: any) => span.name === "app-provider-request");
  expect(inside.parentSpanId).toBe(ai.spanId);
});
test("an AI call whose request runs inside two application spans in a row reaches Future AGI twice, and none of the application's spans reach it", async () => {
  // A stated limit. The wire layer follows one application span back to the AI span, not
  // two, so the same request is also sent as a wire span. Its parent is the inner
  // application span, which Future AGI never receives.
  const output = await child("inside-ai-call.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire", INSIDE_DEPTH: "2" });
  const app = JSON.parse(output.stdout.trim().split("\n").at(-1)!).appSpans;
  expect(requests).toBe(1);
  // The application's own spans stay with the application: none reaches Future AGI.
  const appIds = new Set(app.map((span: any) => span.spanId)), appNames = new Set(app.map((span: any) => span.name));
  expect(allSpans().filter(span => appIds.has(span.spanId) || appNames.has(span.name)).map(span => span.name)).toEqual([]);
  // Future AGI receives the AI span and one wire span for the same request.
  expect(allSpans()).toHaveLength(2);
  const wire = wireSpans();
  expect(wire).toHaveLength(1);
  const ai = allSpans().find(span => attrs(span)["traceai.capture.layer"] !== "wire");
  expect(attrs(ai)["gen_ai.span.kind"] ?? attrs(ai)["fi.span.kind"]).toBe("LLM");
  const outer = app.find((span: any) => span.name === "app-provider-request"), attempt = app.find((span: any) => span.name === "app-provider-attempt");
  expect(outer.parentSpanId).toBe(ai.spanId);
  expect(attempt.parentSpanId).toBe(outer.spanId);
  expect(wire[0].traceId).toBe(ai.traceId);
  expect(wire[0].parentSpanId).toBe(attempt.spanId);
});
test("under a parent-based application sampler, an AI call whose request runs inside an application span the sampler did not record reaches Future AGI twice, and none of the application's spans reach it", async () => {
  // A stated limit. ParentBased(TraceIdRatioBased(0)) with wire capture on. The application's
  // span around the provider request is not recorded: it keeps the trace id, and has a span id
  // of its own and no parent id, so nothing links it to the AI span. Matching it by trace id
  // alone could drop a separate AI request in the same trace (next test), so the same request
  // is also sent as a wire span.
  const output = await child("inside-ai-call.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire", APP_SAMPLER: "parentbased-zero", FI_SAMPLE_RATE: "1" });
  const app = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(requests).toBe(1);
  // The application's backend receives nothing for this trace.
  expect(app.appSpans).toEqual([]);
  // Future AGI receives the AI span and one wire span for the same request.
  expect(allSpans()).toHaveLength(2);
  const wire = wireSpans();
  expect(wire).toHaveLength(1);
  const ai = allSpans().find(span => attrs(span)["traceai.capture.layer"] !== "wire");
  expect(attrs(ai)["gen_ai.span.kind"] ?? attrs(ai)["fi.span.kind"]).toBe("LLM");
  expect(ai.traceId).toBe(app.request.traceId); expect(ai.parentSpanId).toBe(app.request.spanId);
  // The application's span around the request is in the AI span's trace, with its own span id.
  // It is the wire span's parent, and Future AGI never receives it.
  const [, insideTraceId, insideSpanId] = app.providerTraceparent.split("-");
  expect(insideTraceId).toBe(ai.traceId); expect(insideSpanId).not.toBe(ai.spanId);
  expect(wire[0].traceId).toBe(ai.traceId); expect(wire[0].parentSpanId).toBe(insideSpanId);
  // None of the application's spans reaches Future AGI, by span id or by name.
  const appIds = new Set([app.request.spanId, insideSpanId]), appNames = new Set(["app-request", "app-before", "app-provider-request", "app-after"]);
  expect(allSpans().filter(span => appIds.has(span.spanId) || appNames.has(span.name)).map(span => span.name)).toEqual([]);
});
test("under a parent-based application sampler, a plain fetch AI request beside an AI SDK call in the same trace is not lost", async () => {
  // ParentBased(TraceIdRatioBased(0)) with wire capture on. In one trace the application did
  // not sample, it starts one OpenAI call and, in the same tick, one plain fetch to the same
  // chat route. The plain fetch has no AI integration, so its wire span is its only record.
  const output = await child("beside-ai-call.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire", APP_SAMPLER: "parentbased-zero", FI_SAMPLE_RATE: "1" });
  const app = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(requests).toBe(2);
  // The application's backend receives nothing for this trace.
  expect(app.appSpans).toEqual([]);
  // Future AGI receives the plain fetch as a wire span, and the OpenAI call once, as its AI span.
  const wire = wireSpans();
  expect(wire).toHaveLength(1);
  expect(allSpans()).toHaveLength(2);
  const ai = allSpans().find(span => attrs(span)["traceai.capture.layer"] !== "wire");
  expect(attrs(ai)["gen_ai.span.kind"] ?? attrs(ai)["fi.span.kind"]).toBe("LLM");
  expect(ai.traceId).toBe(app.request.traceId); expect(ai.parentSpanId).toBe(app.request.spanId);
  // The wire span is the plain fetch's: it sits beside the AI span, under the application's
  // request span, not inside the AI call.
  expect(wire[0].traceId).toBe(app.request.traceId); expect(wire[0].parentSpanId).toBe(app.request.spanId);
  // None of the application's spans reaches Future AGI, by span id or by name.
  expect(allSpans().filter(span => span.spanId === app.request.spanId || span.name === "app-request").map(span => span.name)).toEqual([]);
});
test.each([
  ["in the same tick as an OpenAI SDK call", "same-tick"],
  ["while the application reads an OpenAI SDK stream", "stream"],
])("in an application with no OpenTelemetry of its own, a plain fetch AI request sent %s is not lost", async (_when, mode) => {
  // No context manager: the application loads no OpenTelemetry. It starts one OpenAI call
  // and one plain fetch to the same chat route, the plain fetch while the call is still open.
  // The plain fetch has no AI integration, so its wire span is its only record.
  const output = await child("beside-ai-call-no-otel.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire", ...(mode === "stream" ? { MODE: "stream" } : {}) });
  expect(JSON.parse(output.stdout.trim().split("\n").at(-1)!)).toEqual({ mode });
  expect(requests).toBe(2);
  // Future AGI receives the OpenAI call once, as its AI span, and the plain fetch as a wire span.
  const wire = wireSpans();
  expect(wire).toHaveLength(1);
  expect(allSpans()).toHaveLength(2);
  const ai = allSpans().find(span => attrs(span)["traceai.capture.layer"] !== "wire");
  expect(attrs(ai)["gen_ai.span.kind"] ?? attrs(ai)["fi.span.kind"]).toBe("LLM");
  // The wire span is the plain fetch's own request, not the SDK's request sent a second
  // time: it carries the plain fetch's message, and it is not inside the AI call.
  expect(JSON.stringify(wire[0])).toContain("a plain fetch beside the call");
  expect(JSON.stringify(ai)).not.toContain("a plain fetch beside the call");
  expect(wire[0].parentSpanId).not.toBe(ai.spanId);
});
test("in an application with no OpenTelemetry of its own, an OpenAI call's own request is sent once, a call made inside it nests under it, and a call made after it does not", async () => {
  // No context manager. The outer OpenAI call's transport makes a second OpenAI call before
  // it sends its own request; after the outer call returns, the application makes a third.
  const output = await child("inside-ai-call-no-otel.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire" });
  expect(JSON.parse(output.stdout.trim().split("\n").at(-1)!)).toEqual({ calls: 3 });
  expect(requests).toBe(3);
  // Each SDK request yields to its own model span: three model spans and no wire span.
  expect(wireSpans()).toHaveLength(0);
  expect(allSpans()).toHaveLength(3);
  const span = (question: string) => {
    const found = allSpans().filter(item => JSON.stringify(item).includes(question));
    expect(found).toHaveLength(1);
    expect(attrs(found[0])["gen_ai.span.kind"] ?? attrs(found[0])["fi.span.kind"]).toBe("LLM");
    return found[0];
  };
  const outer = span("the outer call"), inside = span("a call inside the call"), after = span("a call after the call");
  expect([undefined, ""]).toContain(outer.parentSpanId);
  // The call made inside the outer call is its child, in its trace.
  expect(inside.traceId).toBe(outer.traceId); expect(inside.parentSpanId).toBe(outer.spanId);
  // The call made after the outer call returned has no parent from it.
  expect([undefined, ""]).toContain(after.parentSpanId);
  expect(after.traceId).not.toBe(outer.traceId);
});
test("in an application with no OpenTelemetry of its own, two OpenAI SDK calls started in the same tick each keep their own AI span, and neither SDK request is sent again as a wire span", async () => {
  // No context manager: the application loads no OpenTelemetry. It starts two OpenAI calls
  // in the same tick, each with its own question (test/apps/two-calls-no-otel.cjs).
  const first = "first question started in the same tick", second = "second question started in the same tick";
  const output = await child("two-calls-no-otel.cjs", ["--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai,wire" });
  expect(JSON.parse(output.stdout.trim().split("\n").at(-1)!)).toEqual({ calls: 2 });
  expect(requests).toBe(2);
  // Each SDK request runs under its own call's AI span, so wire capture records neither.
  expect(wireSpans().map(span => span.name)).toEqual([]);
  // Future AGI receives two AI spans: one carries only the first question, the other only the second.
  const ai = allSpans().filter(span => attrs(span)["traceai.capture.layer"] !== "wire" && (attrs(span)["gen_ai.span.kind"] ?? attrs(span)["fi.span.kind"]) === "LLM");
  const questions = ai.map(span => ({ first: JSON.stringify(span).includes(first), second: JSON.stringify(span).includes(second) }));
  expect(questions).toHaveLength(2);
  expect(questions).toEqual(expect.arrayContaining([{ first: true, second: false }, { first: false, second: true }]));
});
test("in an application with no OpenTelemetry of its own, a plain fetch AI request sent in the same tick as an OpenAI Agents SDK run is not lost, and the run keeps its model span, when @traceai/auto is loaded before the Agents SDK", async () => {
  // No context manager: the application loads no OpenTelemetry. agents-source-preload.cjs lets
  // the agent load the integration from source and turns on the SDK's own tracing. The
  // application starts one agent run and one plain fetch to the same chat route in the same tick.
  const app = "beside-agents-run-no-otel.cjs", preload = ["-r", resolve(apps, "agents-source-preload.cjs")];
  const env = { FI_INSTRUMENTATIONS: "openai-agents,wire" };
  // First without the agent: both requests reach the provider and both answers come back.
  // A failure up to the loss checks below is in the fixture, not a lost span.
  const baseline = await child(app, preload, env);
  expect(JSON.parse(baseline.stdout.trim().split("\n").at(-1)!)).toEqual({ finalOutput: "hello world", plain: "hello world", status: null });
  expect(requests).toBe(2);
  expect(allSpans()).toHaveLength(0);
  requests = 0;
  // Then with the agent loaded first (--require runs in the order given): the source resolver, the agent, then the Agents SDK. The SDK's global
  // dispatcher is installed after the agent has captured Node's own, and the OpenAI Agents integration is loaded from source.
  const output = await child(app, ["-r", resolve(apps, "agents-source-resolve.cjs"), "--require", "@traceai/auto/register", "-r", resolve(apps, "agents-sdk-tracing.cjs")], { ...env, TRACED: "1", OPENAI_API_KEY: undefined });
  expect(JSON.parse(output.stdout.trim().split("\n").at(-1)!)).toEqual({ finalOutput: "hello world", plain: "hello world", status: "instrumented" });
  expect(requests).toBe(2);
  // What Future AGI received, shown when one of the two checks below fails.
  const kindOf = (span: any) => attrs(span)["gen_ai.span.kind"] ?? attrs(span)["fi.span.kind"];
  const got = allSpans().map(span => `${span.name} (${attrs(span)["traceai.capture.layer"]}, ${kindOf(span)})`);
  // The agent run arrives as the integration's model span, not from wire capture.
  const model = allSpans().filter(span => attrs(span)["traceai.capture.layer"] !== "wire" && kindOf(span) === "LLM");
  expect({ got, modelSpan: model.length > 0 }).toEqual({ got, modelSpan: true });
  // The plain fetch arrives as a wire span with its own message, so the SDK's own request,
  // sent a second time, could not stand in for it.
  const plain = wireSpans().filter(span => JSON.stringify(span).includes("a plain fetch beside the agent run"));
  expect({ got, plainFetchWireSpan: plain.length > 0 }).toEqual({ got, plainFetchWireSpan: true });
  expect(output.stderr).not.toMatch(/FI_DISPATCHER_UNKNOWN/);
}, 60000);
test("in an application with no OpenTelemetry of its own that loaded the OpenAI Agents SDK before @traceai/auto, the application is unaffected and the key-carrying export is refused with the dispatcher cause and the load-order fix, not sent through that dispatcher", async () => {
  // The same application, with the Agents SDK preload first and the agent after it: the Agents SDK's global dispatcher is in the undici slot
  // when the agent loads, so no key-carrying export is sent. The one collector warning names the refusal code and the fix, and no key, URL or header.
  const app = "beside-agents-run-no-otel.cjs", preload = ["-r", resolve(apps, "agents-source-preload.cjs")];
  const output = await child(app, [...preload, "--import", "@traceai/auto/register"], { FI_INSTRUMENTATIONS: "openai-agents,wire", TRACED: "1" });
  expect(JSON.parse(output.stdout.trim().split("\n").at(-1)!)).toEqual({ finalOutput: "hello world", plain: "hello world", status: "instrumented" });
  expect(requests).toBe(2);
  expect(allSpans()).toHaveLength(0);
  expect(output.stderr).toContain("Collector export failed: Future AGI keys were not sent (FI_DISPATCHER_UNKNOWN).");
  expect(output.stderr).toContain("load @traceai/auto first, for example `node --import @traceai/auto/register app.mjs`");
  expect(output.stderr.split("\n").filter(line => line.includes("Collector export failed"))).toHaveLength(1);
  for (const forbidden of ["mock-fi-api-key", "mock-fi-secret-key", url, "x-api-key", "x-secret-key"]) expect(output.stderr).not.toContain(forbidden);
}, 60000);
test.each([
  ["an AI call from an integration the agent does not scope", "unscoped", "unscoped model call"],
  ["fi-core's startActiveSpan, whose callback returns before its span ends", "active-span-left-open", "span left open"],
])("in an application with no OpenTelemetry of its own and incoming http listed, a plain fetch AI request a request handler sends after %s is not lost, and both nest under the SERVER span", async (_what, mode, name) => {
  // No context manager. Inside a request handler, the application starts an AI span with an
  // agent-bound tracer outside any patched SDK call, as Groq, BeeAI or the AI SDK 7 telemetry
  // do, and then sends a plain fetch to the chat route while that span still records
  // (test/apps/unscoped-in-request-no-otel.cjs).
  const output = await child("unscoped-in-request-no-otel.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http", MODE: mode });
  expect(JSON.parse(output.stdout.trim().split("\n").at(-1)!)).toEqual({ mode, answer: "hello world" });
  expect(requests).toBe(1);
  // What Future AGI received, shown when a check below fails.
  const got = allSpans().map(span => `${span.name} (kind ${span.kind}, ${attrs(span)["traceai.capture.layer"]})`);
  const server = allSpans().filter(span => span.kind === 2);
  const ai = allSpans().filter(span => span.name === name);
  expect({ got, server: server.length, ai: ai.length }).toEqual({ got, server: 1, ai: 1 });
  // The AI call keeps its span, a child of the request's SERVER span.
  expect(ai[0].parentSpanId).toBe(server[0].spanId);
  // The plain fetch arrives as its own wire span, also a child of the SERVER span, not of the AI span.
  const plain = wireSpans().filter(span => JSON.stringify(span).includes("a plain fetch after the AI call"));
  expect({ got, plainFetchWireSpan: plain.length }).toEqual({ got, plainFetchWireSpan: 1 });
  expect(plain[0].parentSpanId).toBe(server[0].spanId);
  expect(JSON.stringify(ai[0])).not.toContain("a plain fetch after the AI call");
});
test("with the application's provider first, Future AGI receives AI spans only: no HTTP server, client or custom spans", async () => {
  const output = await child("own-provider.cjs", ["-r", resolve(apps, "app-otel.cjs"), "--import", "@traceai/auto/register"], { MODE: "app-first", APP_HTTP: "true" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(info.registered).toEqual({ trace: true, context: true, propagation: true, metrics: true });
  // Two AI calls: one inside the custom span, one inside the incoming request.
  expect(allSpans()).toHaveLength(2); expect(wireSpans()).toHaveLength(2);
  expect(allSpans().map(span => span.name)).not.toContain("app-work");
  // The application's exporter keeps its own SERVER, CLIENT and custom spans, and no AI span.
  const kinds = info.appSpans.map((span: any) => span.kind).sort();
  expect(kinds).toEqual([0, 1, 2]);
  expect(info.appSpans.some((span: any) => span.layer === "wire")).toBe(false);
});
test("beside an application provider with its own HTTP instrumentation, listing http keeps the application's SERVER spans and warns once", async () => {
  const output = await child("own-provider.cjs", ["-r", resolve(apps, "app-otel.cjs"), "--import", "@traceai/auto/register"], { MODE: "app-first", APP_HTTP: "true", FI_INSTRUMENTATIONS: "wire,http" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  // The application's exporter keeps its own SERVER, CLIENT and custom spans.
  expect(info.appSpans.map((span: any) => span.kind).sort()).toEqual([0, 1, 2]);
  // Future AGI receives the two AI spans and no SERVER span; the agent's HTTP instrumentation is not installed.
  expect(allSpans()).toHaveLength(2); expect(wireSpans()).toHaveLength(2);
  expect(info.http).toMatchObject({ name: "http", status: "disabled" });
  expect(output.stderr.split("\n").filter(line => line.includes("[traceai:auto]"))).toHaveLength(1);
  expect(output.stderr).toContain("HTTP");
});
test("the forwarding switch sends application spans again, and FI_HIDE_* hides other AI tracers' content keys", async () => {
  const output = await child("own-provider.cjs", ["-r", resolve(apps, "app-otel.cjs"), "--import", "@traceai/auto/register"],
    { MODE: "forward", FI_FORWARD_APP_SPANS: "true", FI_HIDE_INPUTS: "true", FI_HIDE_OUTPUTS: "true" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  const work = allSpans().find(span => span.name === "app-work");
  expect(work).toBeDefined(); expect(wireSpans()).toHaveLength(1);
  expect(attrs(work)).toMatchObject({
    "ai.prompt.messages": "[REDACTED]", "ai.response.text": "[REDACTED]", "ai.toolCall.args": "[REDACTED]", "ai.toolCall.result": "[REDACTED]",
    "gen_ai.prompt.0.content": "[REDACTED]", "gen_ai.completion.0.content": "[REDACTED]", "llm.prompts": "[REDACTED]",
    "traceloop.entity.input": "[REDACTED]", "traceloop.entity.output": "[REDACTED]", "ai.response.model": "gpt-fixture", "app.order": "order-1",
  });
  const data = JSON.stringify(allSpans());
  for (const value of ["PRIVATE-FOREIGN-PROMPT", "PRIVATE-FOREIGN-ANSWER", "PRIVATE-FOREIGN-TOOL"]) expect(data).not.toContain(value);
  // The application's own span is unchanged.
  expect(info.appContent).toBe(true);
});
test("FI keys alone install nothing, and an explicit start registers no OpenTelemetry globals", async () => {
  const noops = { trace: "NoopTracerProvider", context: "NoopContextManager", propagation: "NoopTextMapPropagator" };
  const keysOnly = await child("own-provider.cjs", [], { MODE: "keys-only" });
  expect(JSON.parse(keysOnly.stdout.trim())).toEqual({ active: false, fetchUnchanged: true, emitUnchanged: true, sigterm: 0, globals: noops });
  expect(allSpans()).toHaveLength(0); expect(requests).toBe(1);
  const started = await child("own-provider.cjs", ["--import", "@traceai/auto/register"], { MODE: "globals" });
  expect(JSON.parse(started.stdout.trim())).toEqual({ active: true, globals: noops });
  expect(wireSpans()).toHaveLength(1);
});
test("with incoming http and outgoing propagation switched on, the agent still registers no propagator or other OpenTelemetry global", async () => {
  const output = await child("own-provider.cjs", ["--import", "@traceai/auto/register"], { MODE: "globals", FI_INSTRUMENTATIONS: "wire,http", TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: "true" });
  expect(JSON.parse(output.stdout.trim())).toEqual({ active: true, globals: { trace: "NoopTracerProvider", context: "NoopContextManager", propagation: "NoopTextMapPropagator" } });
  expect(wireSpans()).toHaveLength(1);
});
test("init() given a provider beside an FI route warns once that AI spans stay on the agent's own provider", async () => {
  const output = await child("own-provider.cjs", [], { MODE: "ignored-provider" });
  expect(JSON.parse(output.stdout.trim())).toEqual({ returnedSupplied: false, active: true });
  const warnings = output.stderr.split("\n").filter(line => line.includes("[traceai:auto]"));
  expect(warnings).toHaveLength(1); expect(warnings[0]).toContain("forwardApplicationSpans");
});
test("Genkit is reported as manual on the AI-only route, and as instrumented with the forwarding switch", async () => {
  const route = { MODE: "integration-status", STATUS_OF: "genkit", FI_INSTRUMENTATIONS: "genkit" };
  const aiOnly = JSON.parse((await child("own-provider.cjs", ["--import", "@traceai/auto/register"], route)).stdout.trim());
  expect(aiOnly.status).toBe("manual");
  expect(aiOnly.reason).toContain("FI_FORWARD_APP_SPANS");
  const forwarding = JSON.parse((await child("own-provider.cjs", ["--import", "@traceai/auto/register"], { ...route, FI_FORWARD_APP_SPANS: "true" })).stdout.trim());
  expect(forwarding.status).toBe("instrumented");
});
test("by default the agent adds no trace headers to a plain application request to a non-AI URL, next to a captured AI call", async () => {
  const output = await child("own-provider.cjs", ["-r", resolve(apps, "app-otel.cjs"), "--import", "@traceai/auto/register"], { MODE: "plain-request" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  expect(wireSpans()).toHaveLength(1);
  // The request ran inside the application's active span, so an addition would have had a parent.
  expect(info.work.plainHeaders).toMatchObject({ "x-plain": "application" });
  for (const name of ["traceparent", "tracestate", "baggage"]) expect(info.work.plainHeaders[name]).toBeUndefined();
});
test("explicitly enabled http sends a SERVER span only for a trace with AI work", async () => {
  await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: "wire,http", HEALTH: "true" });
  const servers = allSpans().filter(s => s.kind === 2), wire = wireSpans()[0];
  // The /health request made no AI call, so its SERVER span is not sent.
  expect(servers).toHaveLength(1); expect(allSpans()).toHaveLength(2);
  expect(wire.parentSpanId).toBe(servers[0].spanId);
});
test("http is off unless the allowlist names it", async () => {
  // Every other eligible integration is enabled; incoming requests create no SERVER span.
  await child("incoming.cjs", undefined, { FI_INSTRUMENTATIONS: undefined });
  expect(allSpans().filter(s => s.kind === 2)).toHaveLength(0); expect(wireSpans()).toHaveLength(1);
});
test("a partial FI key pair gives one warning in total and installs nothing", async () => {
  const output = await child("plain.cjs", undefined, { FI_SECRET_KEY: "" });
  const warnings = output.stderr.split("\n").filter(line => line.includes("[traceai:auto]"));
  expect(warnings).toHaveLength(1);
  expect(warnings[0]).toContain("Ignoring incomplete credentials"); expect(warnings[0]).toContain("nothing is sent to Future AGI");
  expect(output.stderr).not.toContain("mock-fi-api-key");
  expect(requests).toBe(1); expect(allSpans()).toHaveLength(0);
});
test("no FI keys with an application provider first gives one warning and leaves that provider alone", async () => {
  const output = await child("plain.cjs", ["-r", resolve(apps, "existing-provider.cjs"), "--import", "@traceai/auto/register"], { FI_API_KEY: "", FI_SECRET_KEY: "" });
  const info = JSON.parse(output.stdout.trim().split("\n").at(-1)!);
  // Nothing is installed: no AI spans on the app's provider and no SIGTERM listener.
  expect(info).toEqual({ existing: true, collected: 0, sigint: 1, sigterm: 1 });
  expect(output.stderr.split("\n").filter(Boolean)).toHaveLength(1);
  expect(output.stderr).toContain("auto-instrumentation is disabled");
  expect(allSpans()).toHaveLength(0);
});
test.each(["sigterm-idle", "sigterm-failed", "sigterm-record-only"])("SIGTERM reaches the application at once when the FI route has nothing to send (%s)", async mode => {
  const output = await child("review.cjs", [], { MODE: mode });
  expect(output.stdout.trim().split("\n")).toEqual(["application-once", "application-handler"]);
});
test("SIGTERM waits to send an AI span made under a parent the application did not sample", async () => {
  const output = await child("review.cjs", [], { MODE: "sigterm-unsampled-parent", FI_SAMPLE_RATE: "1" });
  expect(output.stdout.trim().split("\n")).toEqual(["flush-start", "flush-finished", "application-once", "application-handler", "exported:1"]);
});
test("shutdown removes the promise hook and gives back process.emit", async () => {
  const output = await child("review.cjs", [], { MODE: "promise-hooks" });
  // The hook fired while the agent ran, and never after shutdown stopped it.
  expect(JSON.parse(output.stdout)).toEqual({ installed: true, restored: true, hookFired: true, hookStopped: true });
});
