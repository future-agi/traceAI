import { spawn } from "node:child_process";
import { createServer, Server } from "node:http";
import { resolve } from "node:path";
import type { AddressInfo } from "node:net";

let server: Server, url: string, exported: any[];
const UNKNOWN = "<unknown: host-truncated>";
const closedEnums: Record<string, readonly string[]> = {
  "fi.span.kind": ["LLM","CHAIN","TOOL","AGENT","RETRIEVER","EMBEDDING","RERANKER","GUARDRAIL","EVALUATOR"],
  "gen_ai.span.kind": ["LLM","CHAIN","TOOL","AGENT","RETRIEVER","EMBEDDING","RERANKER","GUARDRAIL","EVALUATOR"],
  "gen_ai.operation.name": ["chat","text_completion","embeddings","create_agent","invoke_agent","execute_tool","generate_content","retrieval","text_to_speech","speech_to_text"],
};
const countKeys = new Set(["gen_ai.usage.input_tokens","gen_ai.usage.output_tokens","gen_ai.usage.total_tokens","gen_ai.usage.cache_read_tokens","gen_ai.usage.cache_creation_tokens","gen_ai.usage.input_tokens.audio","gen_ai.usage.output_tokens.audio","gen_ai.usage.output_tokens.reasoning","traceai.capture.dropped_content_count"]);
const booleanKeys = new Set(["traceai.wire.truncated","traceai.wire.incomplete","traceai.wire.body_unavailable","traceai.capture.host_data_unknown"]);
function nonContent(key: string, value: any): boolean {
  return Boolean(typeof value === "string" && closedEnums[key]?.includes(value) || typeof value === "number" && Number.isSafeInteger(value) && (countKeys.has(key) && value >= 0 || key === "http.response.status_code" && value >= 100 && value <= 599) || typeof value === "boolean" && booleanKeys.has(key));
}
function otlpValue(value: any): any {
  if (Object.prototype.hasOwnProperty.call(value, "stringValue")) return value.stringValue;
  if (Object.prototype.hasOwnProperty.call(value, "boolValue")) return value.boolValue;
  if (Object.prototype.hasOwnProperty.call(value, "intValue")) return Number(value.intValue);
  if (Object.prototype.hasOwnProperty.call(value, "doubleValue")) return value.doubleValue;
  if (value.arrayValue) return (value.arrayValue.values ?? []).map(otlpValue);
  throw new Error("Unexpected OTLP attribute representation");
}
function attributeMap(entries: any[] = []): Record<string, any> {
  expect(new Set(entries.map(entry => entry.key)).size).toBe(entries.length);
  return Object.fromEntries(entries.map(entry => [entry.key, otlpValue(entry.value)]));
}
function expectUnknownMap(entries: any[] = []) {
  for (const [key, value] of Object.entries(attributeMap(entries))) if (!nonContent(key, value)) expect(value).toBe(key === "session.id" ? "<redacted>" : UNKNOWN);
}
function expectLineage(item: any): any {
  expect(item.traceId).toMatch(/^[0-9a-f]{32}$/); expect(item.spanId).toMatch(/^[0-9a-f]{16}$/);
  const matches = exported.filter(span => span.traceId === item.traceId && span.spanId === item.spanId);
  expect(matches).toHaveLength(1);
  const span = matches[0], attrs = attributeMap(span.attributes);
  for (const key of ["droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"]) expect(Number(span[key] ?? 0)).toBe(item[key]);
  expect((span.events ?? []).length).toBe(item.events.length); expect((span.links ?? []).length).toBe(item.links.length);
  expect(String(span.startTimeUnixNano)).toBe(item.startTimeUnixNano); expect(String(span.endTimeUnixNano)).toBe(item.endTimeUnixNano);
  for (let i = 0; i < item.events.length; i++) { expect(String(span.events[i].timeUnixNano)).toBe(item.events[i].timeUnixNano); expect(Number(span.events[i].droppedAttributesCount ?? 0)).toBe(item.events[i].droppedAttributesCount); }
  for (let i = 0; i < item.links.length; i++) { expect(span.links[i].traceId).toBe(item.links[i].traceId); expect(span.links[i].spanId).toBe(item.links[i].spanId); expect(Number(span.links[i].droppedAttributesCount ?? 0)).toBe(item.links[i].droppedAttributesCount); }
  if (item.hostCountLoss || item.valueAtLimit) {
    expect(span.name).toBe(UNKNOWN); expect(attrs["traceai.capture.host_data_unknown"]).toBe(true);
    expectUnknownMap(span.attributes); expectUnknownMap(span.__resource); expectUnknownMap(span.__scope?.attributes);
    if (item.exactNativeKeys) {
      const ordinaryKeys = (keys: string[]) => keys.filter(key => key !== "traceai.capture.host_data_unknown" && key !== "traceai.capture.dropped_content_count").sort();
      expect(ordinaryKeys(Object.keys(attrs))).toEqual(ordinaryKeys(item.attributeKeys));
      expect(Object.keys(attributeMap(span.__resource))).toEqual(expect.arrayContaining(item.resourceKeys));
      expect(Object.keys(attributeMap(span.__scope?.attributes))).toEqual(expect.arrayContaining(item.scopeKeys));
      expect(Number(span.status?.code ?? 0)).toBe(item.statusCode);
      expect(span.status?.message).toBe(item.statusHasMessage ? UNKNOWN : undefined);
      for (let i = 0; i < item.events.length; i++) expect(Object.keys(attributeMap(span.events[i].attributes)).sort()).toEqual([...item.events[i].attributeKeys].sort());
      for (let i = 0; i < item.links.length; i++) expect(Object.keys(attributeMap(span.links[i].attributes)).sort()).toEqual([...item.links[i].attributeKeys].sort());
    }
    expect(span.__scope?.name).toBe(UNKNOWN);
    if (span.__scope?.version !== undefined) expect(span.__scope.version).toBe(UNKNOWN);
    if (span.__scopeSchemaUrl !== undefined && span.__scopeSchemaUrl !== "") expect(span.__scopeSchemaUrl).toBe(UNKNOWN);
    if (span.status?.message !== undefined) expect(span.status.message).toBe(UNKNOWN);
    for (const event of span.events ?? []) { expect(event.name).toBe(UNKNOWN); expectUnknownMap(event.attributes); }
    for (const link of span.links ?? []) { expectUnknownMap(link.attributes); expect(link.traceState ?? "").toBe(""); }
    expect(span.traceState ?? "").toBe("");
  } else {
    expect(span.name).toBe(item.name); expect(attrs["traceai.capture.host_data_unknown"]).toBeUndefined();
  }
  return span;
}
beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = []; req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString();
      if (req.url === "/tracer/v1/traces") {
        const data = JSON.parse(raw);
        exported.push(...(data.resourceSpans ?? []).flatMap((r: any) => (r.scopeSpans ?? []).flatMap((s: any) => (s.spans ?? []).map((span: any) => ({ ...span, __resource: r.resource?.attributes ?? [], __scope: s.scope, __scopeSchemaUrl: s.schemaUrl })))));
        res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); return;
      }
      res.writeHead(200, { "content-type": "application/json", "x-parity": "unchanged" });
      res.end(JSON.stringify({ received: raw, id: "fixture-response", model: "fixture-model", choices: [{ message: { role: "assistant", content: "ordinary answer" }, finish_reason: "stop" }], usage: { prompt_tokens: 11, completion_tokens: 3, total_tokens: 14 } }));
    });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); });
beforeEach(() => { exported = []; });

const limits = [{ name: "default", count: undefined, value: undefined }, ...[32, 128, 140].map(count => ({ name: `count-${count}`, count, value: undefined })), ...[8, 16, 24].map(value => ({ name: `value-${value}`, count: undefined, value }))];
const cases = ["register", "init"].flatMap(entry => ["owned", "host"].flatMap(owner => limits.map(limit => ({ entry, owner, ...limit }))));
cases.push(...["owned", "host"].flatMap(owner => limits.filter(limit => limit.count === 32 || limit.value === 8).map(limit => ({ entry: "preload", owner, ...limit }))));
test.each(cases)("RC2 $entry / $owner / $name protects attributes, events, links and application parity", async ({ entry, owner, count, value }) => {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(env)) if (/^(FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { ENTRY: entry, OWNER: owner, COUNT: String(count ?? 128), VALUE: String(value ?? 0), FI_API_KEY: "synthetic-fi-rc2-api", FI_SECRET_KEY: "synthetic-fi-rc2-secret", FI_PROJECT_NAME: "media-limits", FI_BASE_URL: url, FI_INSTRUMENTATIONS: "wire", FI_PROFILE: "traceai-rc2-synthetic-missing-profile" });
  if (count !== undefined) env.OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT = String(count);
  if (value !== undefined) env.OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT = String(value);
  const app = resolve(__dirname, "apps/media-limits.cjs");
  const preload = entry === "preload" ? resolve(__dirname, "../dist/agent/preload.cjs") : "@traceai/auto/register";
  const args = ["-r", app, ...(entry === "init" ? [] : ["-r", preload]), "-e", `require(${JSON.stringify(app)}).main().catch(e=>{console.error(e);process.exitCode=1})`];
  const output = await new Promise<string>((done, reject) => {
    const child = spawn(process.execPath, args, { cwd: resolve(__dirname, ".."), env }); let stdout = "", stderr = "";
    child.stdout.on("data", chunk => { stdout += chunk; }); child.stderr.on("data", chunk => { stderr += chunk; });
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error(`media limits child timeout: ${stderr}`)); }, 25000);
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.once("close", code => { clearTimeout(timer); code === 0 ? done(stdout) : reject(new Error(`${code}: ${stderr}\n${stdout}`)); });
  });
  const result = JSON.parse(output.trim());
  expect({ spans: result.spans, parity: result.parity, nativeUnchanged: result.nativeUnchanged }).toEqual({ spans: 30, parity: true, nativeUnchanged: true });
  expect(result.lineages).toHaveLength(30);
  expect(new Set(result.lineages.map((item: any) => `${item.traceId}/${item.spanId}`)).size).toBe(30);
  expect(result.lineages.some((item: any) => value === undefined ? item.hostCountLoss : item.valueAtLimit)).toBe(true);
  expect(exported.length).toBeGreaterThanOrEqual(31);
  expect(JSON.stringify(exported)).not.toContain("AQID");
  for (const item of result.lineages) {
    const span = expectLineage(item);
    expect(span.events).toHaveLength(1); expect(span.links).toHaveLength(3);
    expect(span.traceId).toBeTruthy(); expect(span.spanId).toBeTruthy();
  }
});

const contextCases: { entry: string; owner: string; value: number; source: string; scenario: string; sdk?: string }[] = [
  ...["register", "init"].flatMap(entry => ["owned", "host"].flatMap(owner => [0, 48, 64, 80, 96].map(value => ({ entry, owner, value, source: "span", scenario: "native" })))),
  ...["register", "init", "manual"].flatMap(entry => ["span", "general"].flatMap(source => [120, 300].map(value => ({ entry, owner: "host", value, source, scenario: "genkit" })))),
  ...["register", "init"].flatMap(entry => [120, 300].map(value => ({ entry, owner: "owned", value, source: "span", scenario: "genkit" }))),
];
contextCases.push(...["register", "init", "manual"].flatMap(entry => (entry === "manual" ? ["host"] : ["owned", "host"]).flatMap(owner =>
  (owner === "host" ? ["native", "core", "legacy"] : ["native"]).flatMap(sdk => [0, 120, 300].map(value =>
    ({ entry, owner, value, source: "span", scenario: "genkit-rewritten", sdk }))))));

test.each(contextCases)("RC2 embedded $scenario / $entry / $owner / $source / $value / $sdk retains loss evidence", async ({ entry, owner, value, source, scenario, sdk }) => {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(env)) if (/^(FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { ENTRY: entry, OWNER: owner, SCENARIO: scenario, COUNT: value ? "128" : "32", VALUE: String(value), LIMIT_SOURCE: source, SDK: sdk ?? "native",
    FI_API_KEY: "synthetic-fi-rc2-api", FI_SECRET_KEY: "synthetic-fi-rc2-secret", FI_PROJECT_NAME: "media-context-loss", FI_BASE_URL: url,
    FI_INSTRUMENTATIONS: scenario.startsWith("genkit") && entry !== "manual" ? "genkit" : "", FI_WIRE_CAPTURE: "false",
    FI_PROFILE: "traceai-rc2-synthetic-missing-profile", GENKIT_ENV: "prod" });
  delete env.GENKIT_TELEMETRY_SERVER;
  // Host cases deliberately have no OTEL_* limits: configuration in code must
  // survive traceAI's Genkit mapping rather than relying on an env fallback.
  if (owner === "owned") {
    if (value) env.OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT = String(value);
    else env.OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT = "32";
  }
  const app = resolve(__dirname, "apps/media-context-loss.cjs");
  const args = ["-r", app, ...(entry === "register" ? ["-r", "@traceai/auto/register"] : []), "-e", `require(${JSON.stringify(app)}).main().catch(e=>{console.error(e);process.exitCode=1})`];
  const output = await new Promise<string>((done, reject) => {
    const child = spawn(process.execPath, args, { cwd: resolve(__dirname, ".."), env }); let stdout = "", stderr = "";
    child.stdout.on("data", chunk => { stdout += chunk; }); child.stderr.on("data", chunk => { stderr += chunk; });
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error(`context loss child timeout: ${stderr}`)); }, 25000);
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.once("close", code => { clearTimeout(timer); code === 0 ? done(stdout) : reject(new Error(`${code}: ${stderr}\n${stdout}`)); });
  });
  const result = JSON.parse(output.trim());
  expect({ parity: result.parity, nativeUnchanged: result.nativeUnchanged }).toEqual({ parity: true, nativeUnchanged: true });
  expect(exported.length).toBeGreaterThanOrEqual(scenario === "genkit" ? 1 : 3);
  expect(JSON.stringify(exported)).not.toContain("AQIDAQID");
  if (scenario.startsWith("genkit")) {
    const names = scenario === "genkit-rewritten" ? ["output-home", "output-cwd", "error-user", "error-cwd", "error-email"].map(name => `rewritten-${name}`) : ["mediaContextFlow", "mapped-limit-evidence"];
    expect(result.lineages.map((item: any) => item.name)).toEqual(names);
    expect(new Set(result.lineages.map((item: any) => `${item.traceId}/${item.spanId}`)).size).toBe(names.length);
    for (const item of result.lineages) {
      if (value > 0 && !(scenario === "genkit" && value === 300 && item.name === "mediaContextFlow")) expect(item.valueAtLimit).toBe(true);
      if (scenario === "genkit" && value === 300 && item.name === "mediaContextFlow") { expect(item.hostCountLoss).toBe(false); expect(item.valueAtLimit).toBe(false); }
      expectLineage(item);
    }
  } else {
    for (const span of exported.filter(span => span.name.startsWith("media-context-"))) {
      expect(span.events).toHaveLength(1); expect(span.links).toHaveLength(1);
    }
  }
});
