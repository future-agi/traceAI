import { createServer, type Server, type RequestListener } from "node:http";
import { createServer as createSecureServer } from "node:https";
import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import type { AddressInfo } from "node:net";
import { ROOT_CONTEXT, trace, SpanKind, defaultTextMapGetter } from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, AlwaysOnSampler } from "@opentelemetry/sdk-trace-base";

// Public acceptance population: runs against the base without importing new helpers.
// RED means the receiver/header/parentage assertion fails, never a missing module.
const root = resolve(__dirname, ".."), apps = resolve(__dirname, "apps");
let server: Server, secure: Server, url: string, secureUrl: string, dir: string;
let received: { host?: string; headers: Record<string, any>; raw: string[]; body: string; id: string; path: string }[];
const body = Buffer.from([0, 1, 127, 128, 255, 10]).toString("base64");
beforeAll(async () => {
  dir = mkdtempSync(join(tmpdir(), "traceai-f4-"));
  mkdirSync(join(dir, ".fi")); writeFileSync(join(dir, ".fi/config.toml"), "");
  writeFileSync(join(dir, "futureagi.toml"), "");
  // New, disposable, explicitly synthetic TLS identity; no real credential is used.
  // The CI image must provide openssl. Failure is a prerequisite failure, not RED.
  execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", join(dir, "loopback.key"), "-out", join(dir, "loopback.crt"), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,DNS:localhost.,IP:127.0.0.1,DNS:api.openai.com,DNS:api.openai.com.,DNS:xn--bcher-kva.internal,DNS:xn--bcher-kva.internal."], { stdio: "ignore", timeout: 15000 });
  const handler: RequestListener = (req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      const requestUrl = new URL(req.url!, "http://loopback.invalid");
      const row = { host: req.headers.host, headers: req.headers, raw: req.rawHeaders, body: Buffer.concat(chunks).toString("base64"), id: requestUrl.searchParams.get("id")!, path: requestUrl.pathname, wireTarget: req.url, localPort: req.socket.localPort };
      received.push(row);
      const next = requestUrl.searchParams.get("next");
      res.writeHead(next ? 307 : 200, { "content-type": "application/json", ...(next ? { location: next } : {}) }); res.end(JSON.stringify(row));
    });
  };
  server = createServer(handler);
  secure = createSecureServer({ key: readFileSync(join(dir, "loopback.key")), cert: readFileSync(join(dir, "loopback.crt")) }, handler);
  await Promise.all([new Promise<void>(r => server.listen(0, "127.0.0.1", r)), new Promise<void>(r => secure.listen(0, "127.0.0.1", r))]);
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  secureUrl = `https://127.0.0.1:${(secure.address() as AddressInfo).port}`;
}, 30000);
afterAll(async () => {
  for (const s of [server, secure]) if (s) { s.closeAllConnections(); await new Promise<void>(r => s.close(() => r())); }
  if (dir) rmSync(dir, { recursive: true, force: true });
});
beforeEach(() => { received = []; });
async function child(extra: Record<string, string> = {}, flag = "-r", extension = "cjs"): Promise<any> {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/^(TRACEAI_|FI_|OTEL_|F4_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { HOME: dir, XDG_CONFIG_HOME: dir, FI_PROFILE: "f4-isolated", FI_CONFIG_FILE: join(dir, "futureagi.toml"), FI_WIRE_CAPTURE: "false", FI_INSTRUMENTATIONS: "wire", TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: "true", F4_AUTO: require.resolve("@traceai/auto", { paths: [root] }), F4_URL: url, F4_CA: join(dir, "loopback.crt") }, extra);
  const args = ["-r", resolve(apps, "f4-outgoing-host.cjs")];
  if (extra.F4_MANUAL !== "1") args.push(flag, require.resolve("@traceai/auto/register", { paths: [root] }));
  args.push(resolve(apps, `f4-outgoing.${extension}`));
  return new Promise((resolveResult, reject) => {
    const proc = spawn(process.execPath, args, { cwd: dir, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    const timer = setTimeout(() => { proc.kill("SIGKILL"); reject(new Error("F4 child timeout")); }, 20000);
    proc.stdout.on("data", c => { stdout += c; }); proc.stderr.on("data", c => { stderr += c; });
    proc.on("error", error => { clearTimeout(timer); reject(error); });
    proc.on("close", code => {
      clearTimeout(timer);
      if (code !== 0) { reject(new Error(`F4 child prerequisite/runtime failure (${code}): ${stderr}`)); return; }
      try {
        const result = JSON.parse(stdout);
        expect(result.telemetryClean).toBe(true);
        resolveResult(result);
      } catch (error) { reject(error); }
    });
  });
}
function assertParent(row: any): void {
  expect(row.response.headers.traceparent).toBe(row.expected);
  const extracted = new W3CTraceContextPropagator().extract(ROOT_CONTEXT, row.response.headers, defaultTextMapGetter);
  const remote = trace.getSpanContext(extracted)!;
  expect(remote).toMatchObject({ traceId: row.expected.split("-")[1], spanId: row.expected.split("-")[2], isRemote: true });
  const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ sampler: new AlwaysOnSampler(), spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const downstream = provider.getTracer("f4-downstream").startSpan("receiver", { kind: SpanKind.SERVER }, extracted);
  expect(downstream.spanContext().traceId).toBe(remote.traceId); downstream.end();
  const ended: any = exporter.getFinishedSpans()[0];
  // OTel 1.x and 2.x represent the same parent via different properties.
  expect(ended.parentSpanContext?.spanId ?? ended.parentSpanId).toBe(remote.spanId);
  void provider.shutdown();
}
function noAdded(row: any): void {
  for (const name of ["traceparent", "tracestate", "baggage", "x-private-custom"]) expect(row.response.headers[name]).toBeUndefined();
}
test.each(["http", "https"])("R01-R05 %s real headers, body parity and downstream parentage", async transport => {
  const out = await child({ F4_TRANSPORT: transport, ...(transport === "https" ? { F4_URL: secureUrl } : {}) });
  for (const row of out.rows) {
    assertParent(row); expect(row.headersUnchanged).toBe(true);
    expect(row.response.body).toBe(row.variant === "get" ? "" : body);
    expect(row.response.raw.filter((_: string, i: number) => i % 2 === 0).filter((v: string) => v.toLowerCase() === "traceparent")).toHaveLength(1);
    expect(row.response.headers["x-private-custom"]).toBeUndefined();
    if (row.variant === "raw") expect(row.response.headers["x-duplicate"]).toBe("one, two");
    if (row.variant === "late") expect(row.response.headers["x-f4-late"]).toBe("kept");
  }
  expect(received).toHaveLength(out.rows.length); expect(out.globalsPreserved).toBe(true);
  expect(out.spanKinds.every((kind: number) => kind === SpanKind.INTERNAL)).toBe(true);
}, 30000);
test.each(["-r", "--import"])("R01 R04 named ESM imports and real %s preload", async flag => {
  const out = await child({ F4_TRANSPORT: "http" }, flag, "mjs");
  out.rows.forEach(assertParent);
});
test("R02 cached native functions captured before manual init", async () => {
  const out = await child({ F4_MANUAL: "1" }); out.rows.forEach(assertParent);
});
test.each(["http", "https"])("R06 R12 G01-G03 %s ownership, suppression, remote flags and safe baggage", async transport => {
  const out = await child({ F4_TRANSPORT: transport, F4_CASE: "ownership", ...(transport === "https" ? { F4_URL: secureUrl } : {}) });
  const rows = Object.fromEntries(out.rows.map((row: any) => [row.id, row]));
  for (const name of ["eligible", "other", "remote", "baggage"]) assertParent(rows[name]);
  for (const name of ["client", "suppressed", "root", "opaque"]) noAdded(rows[name]);
  for (const name of ["existing", "empty"]) {
    expect(rows[name].response.headers.traceparent).toBe(name === "empty" ? "" : "00-11111111111111111111111111111111-2222222222222222-01");
    expect(rows[name].response.headers.baggage).toBeUndefined();
  }
  expect(rows.other.response.headers).toMatchObject({ baggage: "app=owned", tracestate: "app=owned" });
  expect(rows.remote.response.headers.traceparent).toMatch(/-00$/);
  for (const name of ["baggage", "baggage-only"]) {
    expect(rows[name].response.headers.baggage).toBe("region=west");
    expect(JSON.stringify(rows[name].response)).not.toMatch(/synthetic-private|synthetic-signed/);
  }
  expect(rows["baggage-only"].response.headers.traceparent).toBeUndefined();
  expect(rows.baggage.response.headers.tracestate).toBe("region=west");
});
test.each(["http", "https"])("R13 %s concurrent parents remain isolated", async transport => {
  const out = await child({ F4_TRANSPORT: transport, F4_CASE: "concurrent", ...(transport === "https" ? { F4_URL: secureUrl } : {}) });
  out.rows.forEach(assertParent); expect(new Set(out.rows.map((r: any) => r.expected)).size).toBe(8);
});
test.each(["api.openai.com", "example.openai.azure.com", "us-aiplatform.googleapis.com", "bedrock-runtime.us-east-1.amazonaws.com", "tenant.endpoints.huggingface.cloud"])("R08 path-independent default catalog exclusion at %s", async host => {
  const allowed = await child({ F4_HOST: "orders.internal" }); assertParent(allowed.rows[0]);
  const denied = await child({ F4_HOST: host }); noAdded(denied.rows[0]);
});
test.each([
  ["none", "+api.openai.com", false], ["all", "", false], ["default", "+api.openai.com", false],
  ["all", "-api.openai.com,+api.openai.com", false], ["default", "+orders.internal", false],
  ["default", "-orders.internal", false], ["bogus", "", false], ["all", "-bad/path", false],
])("R09 mode=%s rules=%s policy", async (mode, hosts, allowed) => {
  const out = await child({ F4_HOST: "api.openai.com", TRACEAI_PROPAGATE_OUTGOING: String(mode), TRACEAI_PROPAGATE_HOSTS: String(hosts) });
  if (allowed) assertParent(out.rows[0]); else noAdded(out.rows[0]);
});
test("R16 public statuses reflect actual additions and shutdown removes hooks", async () => {
  const out = await child({ F4_CASE: "lifecycle" });
  for (const row of out.rows.filter((r: any) => r.id !== "after-shutdown")) assertParent(row);
  noAdded(out.rows.find((r: any) => r.id === "after-shutdown"));
  expect(out.pending.integrations.find((s: any) => s.name === "outgoing-http").status).toBe("pending");
  expect(out.beforeShutdown.integrations.find((s: any) => s.name === "outgoing-http").status).toBe("instrumented");
});

// Preserve the native controls; unsupported fetch/Undici checks are in the adapter suite.
test.each(["http", "https"])("R2 %s adapters remain off without explicit opt-in", async transport => {
  for (const flag of ["", "false", "invalid"]) {
    const out = await child({ F4_TRANSPORT: transport, ...(transport === "https" ? { F4_URL: secureUrl } : { F4_HOST: "orders.internal" }), TRACEAI_PROPAGATE_OUTGOING_ADAPTERS: flag, TRACEAI_PROPAGATE_OUTGOING: "all" });
    noAdded(out.rows[0]);
    expect(out.pending.config.propagateOutgoingAdapters).toBe(false);
    expect(out.beforeShutdown.integrations.find((s: any) => s.name === "outgoing-http").status).toBe("disabled");
    expect(out.beforeShutdown.integrations.some((s: any) => s.name === "outgoing-undici")).toBe(false);
  }
}, 30000);
// Round-3 RED/GREEN uses the SAME fixture bytes against round 2 and round 3.
// All destinations resolve to loopback; never contact a real provider.
test.each(["http"])("R3 %s literal backslash paths cannot authorize a denied vendor", async transport => {
  const common = { F4_CASE: "authority", F4_TRANSPORT: transport };
  const positive = await child({ ...common, F4_HOST: "orders.internal" });
  assertParent(positive.rows[0]);
  expect(positive.rows[0].response.headers.baggage).toBe("region=west");
  for (const mode of ["default", "all"]) {
    const out = await child({ ...common, F4_HOST: "api.openai.com", F4_LITERAL_PATH: "/\\orders.internal/ordinary", TRACEAI_PROPAGATE_OUTGOING: mode,
      TRACEAI_PROPAGATE_HOSTS: mode === "all" ? "-api.openai.com,+orders.internal" : "" });
    const row = out.rows[0];
    noAdded(row);
    expect(row.response.host).toBe(`api.openai.com:${new URL(url).port}`);
    expect(row.response.wireTarget).toBe("/\\orders.internal/ordinary");
    expect(row.response.body).toBe(body);
    expect(row.response.headers["x-f4-original"]).toBe("preserved");
    expect(row.headersUnchanged).toBe(true);
  }
}, 90000);

test.each(["http", "https"])("R3 %s physical Ollama port cannot be hidden by Host", async transport => {
  const common = { F4_CASE: "authority", F4_TRANSPORT: transport };
  const positive = await child({ ...common, F4_URL: transport === "https" ? secureUrl : url });
  assertParent(positive.rows[0]);
  const handler = server.listeners("request")[0] as RequestListener;
  const receiver = transport === "https" ? createSecureServer({ key: readFileSync(join(dir, "loopback.key")), cert: readFileSync(join(dir, "loopback.crt")) }, handler) : createServer(handler);
  // A port collision is a prerequisite failure, never a skipped privacy test.
  await new Promise<void>((resolveListen, reject) => { receiver.once("error", reject); receiver.listen(11434, "127.0.0.1", resolveListen); });
  try {
    const hostname = transport === "https" ? "127.0.0.1" : "orders.internal";
    const target = `${transport}://${hostname}:11434`;
    const cases: Record<string, string>[] = [
      { F4_URL: `${transport}://127.0.0.1:11434`, F4_HOST_HEADER: "127.0.0.1" },
      { F4_URL: target, F4_HOST_HEADER: hostname },
      { F4_URL: target, F4_HOST_HEADER: `${hostname}:80`, F4_EARLY_HEADERS: "raw" },
      { F4_URL: target, F4_HOST_HEADER: `${hostname}:443`, F4_EARLY_HEADERS: "expect" },
      { F4_URL: target, F4_HOST_HEADER: `${hostname}:11434` },
      { F4_URL: `${transport}://${hostname}`, F4_DEFAULT_PORT: "11434", F4_HOST_HEADER: hostname },
      { F4_URL: `${transport}://${hostname}`, F4_AGENT_PORT: "11434", F4_HOST_HEADER: hostname },
    ];
    for (const scenario of cases) {
      const out = await child({ ...common, ...scenario });
      const row = out.rows[0]; noAdded(row);
      expect(row.response.localPort).toBe(11434);
      expect(row.response.host).toBe(scenario.F4_HOST_HEADER);
      expect(row.response.body).toBe(body);
      expect(row.response.headers["x-f4-original"]).toBe("preserved");
      expect(row.headersUnchanged).toBe(true);
    }
    for (const mode of ["default", "all"]) for (const early of ["", "raw", "expect"]) {
      const out = await child({ ...common, F4_URL: target, F4_EARLY_HEADERS: early, TRACEAI_PROPAGATE_OUTGOING: mode, TRACEAI_PROPAGATE_HOSTS: `+${hostname}` });
      noAdded(out.rows[0]);
      expect(out.rows[0].response.localPort).toBe(11434);
      expect(out.rows[0].response.body).toBe(body);
      expect(out.rows[0].headersUnchanged).toBe(true);
    }
    const owned = await child({ ...common, F4_URL: target, F4_HOST_HEADER: hostname, F4_OWNERSHIP: "existing" });
    expect(owned.rows[0].response.headers.traceparent).toBe("00-11111111111111111111111111111111-2222222222222222-01");
    expect(owned.rows[0].response.headers.baggage).toBeUndefined();
    expect(owned.rows[0].response.headers.tracestate).toBeUndefined();
    expect(owned.rows[0].headersUnchanged).toBe(true);
  } finally { receiver.closeAllConnections(); await new Promise<void>(r => receiver.close(() => r())); }
}, 180000);

test.each(["http", "https"])("R3 %s early headers wait for matching transport authority", async transport => {
  for (const early of ["raw", "expect"]) {
    const out = await child({ F4_CASE: "authority", F4_TRANSPORT: transport, F4_URL: transport === "https" ? secureUrl : url, F4_EARLY_HEADERS: early });
    const row = out.rows[0]; assertParent(row);
    expect(row.response.headers.baggage).toBe("region=west");
    expect(row.response.headers.tracestate).toBe("region=west");
    expect(row.response.body).toBe(body);
    expect(row.headersUnchanged).toBe(true);
    expect(row.response.raw.filter((_: string, i: number) => i % 2 === 0).filter((v: string) => v.toLowerCase() === "traceparent")).toHaveLength(1);
  }
}, 60000);

test.each(["http", "https"])("R6 %s all and allow rules never waive vendor exclusion", async transport => {
  for (const mode of ["default", "all"]) {
    const common = { F4_CASE: "authority", F4_TRANSPORT: transport, F4_URL: transport === "https" ? secureUrl : url, TRACEAI_PROPAGATE_OUTGOING: mode };
    const positive = await child({ ...common, TRACEAI_PROPAGATE_HOSTS: "+127.0.0.1" });
    assertParent(positive.rows[0]);
    // The disposable certificate includes the synthetic vendor name; ordinary
    // TLS verification remains enabled and the child DNS routes only to loopback.
    for (const hosts of ["", "+api.openai.com"]) for (const early of ["", "raw", "expect"]) {
      const out = await child({ ...common, F4_HOST: "api.openai.com", F4_EARLY_HEADERS: early, TRACEAI_PROPAGATE_HOSTS: hosts });
      noAdded(out.rows[0]); expect(out.rows[0].response.body).toBe(body); expect(out.rows[0].headersUnchanged).toBe(true);
    }
  }
}, 180000);

test.each(["http", "https"])("R6 %s Unicode metadata never adds a header or breaks normal/raw/Expect requests", async transport => {
  for (const early of ["", "raw", "expect"]) {
    const out = await child({ F4_CASE: "authority", F4_TRANSPORT: transport, F4_URL: transport === "https" ? secureUrl : url, F4_EARLY_HEADERS: early, F4_METADATA: "alias" });
    const row = out.rows[0]; assertParent(row);
    expect(row.response.headers.baggage).toBe("region=west");
    expect(row.response.headers["x-f4-probe"]).toBeUndefined();
    expect(row.response.raw.some((value: string) => /probe|note=/.test(value))).toBe(false);
    expect(row.response.body).toBe(body); expect(row.headersUnchanged).toBe(true);
  }
}, 90000);

test.each(["http", "https"])("R6 %s cross-origin redirect and return stay closed for callbacks/await/new child spans", async transport => {
  const first = transport === "https" ? secureUrl : url;
  // Same-scheme, different host; both hostnames route to this loopback receiver.
  const next = new URL(first); next.hostname = "localhost";
  for (const flow of ["callback", "await", "child", "precreated"]) for (const early of ["", "raw", "expect"]) {
    const out = await child({ F4_CASE: "redirect", F4_TRANSPORT: transport, F4_URL: first, F4_REDIRECT_URL: next.href, F4_REDIRECT_FLOW: flow, F4_EARLY_HEADERS: early });
    expect(out.rows).toHaveLength(4);
    assertParent(out.rows[0]); expect(out.rows[0].response.headers.baggage).toBe("region=west");
    expect(out.rows[0].response.headers.tracestate).toBe("region=west");
    noAdded(out.rows[1]); noAdded(out.rows[2]); assertParent(out.rows[3]);
    for (const row of out.rows) { expect(row.response.body).toBe(body); expect(row.headersUnchanged).toBe(true); }
    expect(out.globalsPreserved).toBe(true);
  }
}, 180000);

test("R6 scheme/port redirect fence, application ownership and same-origin positive control", async () => {
  for (const [first, next] of [[url, secureUrl], [secureUrl, url]]) {
    for (const owned of ["0", "1"]) {
      const out = await child({ F4_CASE: "redirect", F4_URL: first, F4_REDIRECT_URL: next, F4_REDIRECT_FLOW: "await", F4_REDIRECT_OWNED: owned });
      assertParent(out.rows[0]); assertParent(out.rows[3]);
      for (const row of out.rows.slice(1, 3)) {
        if (owned === "1") expect(row.response.headers).toMatchObject({ traceparent: "00-11111111111111111111111111111111-2222222222222222-01", tracestate: "app=owned", baggage: "app=owned" });
        else noAdded(row);
        expect(row.headersUnchanged).toBe(true); expect(row.response.body).toBe(body);
      }
    }
  }
  for (const first of [url, secureUrl]) {
    const out = await child({ F4_CASE: "redirect", F4_URL: first, F4_REDIRECT_URL: first, F4_REDIRECT_FLOW: "await" });
    out.rows.forEach(assertParent);
  }
}, 120000);

// R7: keep these exact fixture/test bytes for predecessor RED and final GREEN.
// Every synthetic name resolves to loopback and TLS verification stays enabled.
test.each(["http", "https"])("R7 %s ambiguous same-scheme Location strings close callbacks/await and return hops", async transport => {
  const first = transport === "https" ? secureUrl : url;
  const next = new URL(first); next.hostname = "localhost";
  for (const form of ["scheme-bare", "scheme-slash"]) for (const flow of ["callback", "await"]) for (const early of ["", "raw", "expect"]) {
    const out = await child({ F4_CASE: "redirect", F4_URL: first, F4_REDIRECT_URL: next.href, F4_REDIRECT_FORM: form, F4_REDIRECT_FLOW: flow, F4_EARLY_HEADERS: early });
    expect(out.rows).toHaveLength(4);
    assertParent(out.rows[0]); expect(out.rows[0].response.headers).toMatchObject({ baggage: "region=west", tracestate: "region=west" });
    expect(out.rows[1].requestInputType).toBe("string");
    expect(out.rows[1].response.host).toBe(next.host);
    expect(out.rows[1].response.path).toBe("/redirect-middle");
    expect(out.rows[2].response.host).toBe(new URL(first).host);
    noAdded(out.rows[1]); noAdded(out.rows[2]); assertParent(out.rows[3]);
    for (const row of out.rows) { expect(row.response.body).toBe(body); expect(row.headersUnchanged).toBe(true); }
    expect(out.globalsPreserved).toBe(true);
  }
}, 180000);

test.each(["http", "https"])("R7 %s dotted and undotted origins close both directions and return hops", async transport => {
  const plain = new URL(transport === "https" ? secureUrl : url); plain.hostname = "localhost";
  const dotted = new URL(plain.href); dotted.hostname = "localhost.";
  expect(plain.origin).not.toBe(dotted.origin);
  for (const [first, next] of [[plain, dotted], [dotted, plain]]) for (const flow of ["callback", "await"]) for (const early of ["", "raw", "expect"]) {
    const out = await child({ F4_CASE: "redirect", F4_URL: first.href, F4_REDIRECT_URL: next.href, F4_REDIRECT_FLOW: flow, F4_EARLY_HEADERS: early });
    expect(out.rows).toHaveLength(4);
    assertParent(out.rows[0]); expect(out.rows[0].response.headers).toMatchObject({ baggage: "region=west", tracestate: "region=west" });
    expect(out.rows[0].response.host).toBe(first.host); expect(out.rows[1].response.host).toBe(next.host);
    expect(out.rows[2].response.host).toBe(first.host);
    noAdded(out.rows[1]); noAdded(out.rows[2]); assertParent(out.rows[3]);
    for (const row of out.rows) { expect(row.response.body).toBe(body); expect(row.headersUnchanged).toBe(true); }
    expect(out.globalsPreserved).toBe(true);
  }
}, 180000);

test.each(["http", "https"])("R7 %s WHATWG same-origin relative/case/IDNA/dotted positives remain eligible", async transport => {
  for (const hostname of ["localhost", "localhost.", "BÜCHER.internal."]) {
    const first = new URL(transport === "https" ? secureUrl : url); first.hostname = hostname;
    for (const form of ["absolute", "relative-path", "relative-parent", "relative-query", "network-path", "uppercase"]) {
      const out = await child({ F4_CASE: "redirect", F4_URL: first.href, F4_REDIRECT_URL: first.href, F4_REDIRECT_FORM: form, F4_REDIRECT_FLOW: "await" });
      expect(out.rows).toHaveLength(4); out.rows.forEach(assertParent);
      const expectedPath = form === "relative-path" ? "/nested/redirect-middle" : form === "relative-query" ? "/nested/redirect-start" : "/redirect-middle";
      expect(out.rows[1].response.path).toBe(expectedPath);
      for (const row of out.rows.slice(0, 3)) {
        expect(row.response.host).toBe(first.host);
        expect(row.response.headers).toMatchObject({ baggage: "region=west", tracestate: "region=west" });
        expect(row.response.body).toBe(body); expect(row.headersUnchanged).toBe(true);
      }
    }
  }
}, 180000);

test.each(["http", "https"])("R7 %s normalized vendor spelling cannot waive exclusion", async transport => {
  for (const mode of ["default", "all"]) for (const early of ["", "raw", "expect"]) {
    const common = { F4_CASE: "authority", F4_URL: transport === "https" ? secureUrl : url, F4_TRANSPORT: transport, F4_EARLY_HEADERS: early, TRACEAI_PROPAGATE_OUTGOING: mode };
    const positive = await child({ ...common, F4_HOST: "LOCALHOST.", TRACEAI_PROPAGATE_HOSTS: "+localhost" });
    assertParent(positive.rows[0]);
    const denied = await child({ ...common, F4_HOST: "API.OPENAI.COM.", TRACEAI_PROPAGATE_HOSTS: "+api.openai.com" });
    noAdded(denied.rows[0]); expect(denied.rows[0].response.host).toBe(`api.openai.com.:${new URL(common.F4_URL).port}`);
    expect(denied.rows[0].response.body).toBe(body); expect(denied.rows[0].headersUnchanged).toBe(true);
  }
}, 180000);

test("R3 native agent host override cannot authorize a different destination", async () => {
  const out = await child({ F4_CASE: "authority", F4_HOST: "orders.internal", F4_AGENT_HOST: "api.openai.com" });
  noAdded(out.rows[0]);
  expect(out.rows[0].response.host).toBe(`orders.internal:${new URL(url).port}`);
  expect(out.rows[0].response.body).toBe(body);
  expect(out.rows[0].headersUnchanged).toBe(true);
});
