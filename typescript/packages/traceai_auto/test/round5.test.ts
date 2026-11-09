import { createServer, request } from "node:http";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { getRegisteredSecrets } from "@traceai/fi-core";
import { createFetchHook } from "../src/wire/fetch-runtime";
import { observeOutgoing, observeTransports } from "../src/wire/observe-node";
import { CREDENTIAL_HEADERS } from "../src/wire/privacy-runtime";
import { privacyFilter } from "../src/export-privacy";

const root = resolve(__dirname, "..");
test("ROUND5 structural media scope includes indexed audio and flattened inlineData MIME siblings", () => {
  const bytes = "U0VDUkVU", clean = privacyFilter().attributes({ "input.audio.0": bytes, "output.audio.1.0": bytes, "input.parts.0.inlineData.data": bytes, "input.parts.0.inlineData.mimeType": "audio/wav" });
  expect(JSON.stringify(clean)).not.toContain(bytes);
  expect(clean["input.parts.0.inlineData.mimeType"]).toBe("audio/wav");
});
test.each(["mimeType", "mime_type", "mediaType", "media_type"].flatMap(mime => [
  { mime, key: "input.parts.0.inlineData.data.0", array: false },
  { mime, key: "input.parts.0.inlineData.data.0", array: true },
  { mime, key: "output.parts.1.inlineData.bytes.0.1", array: false },
  { mime, key: "output.parts.1.inlineData.bytes.0.1", array: true },
]))("ROUND5 indexed MIME-qualified audio $mime/$key (array $array) is scrubbed on every attribute surface", ({ mime, key, array }) => {
  const bytes = "U0VDUkVU", mimeKey = key.replace(/(?:data|bytes)(?:\.\d+)+$/, mime);
  const attrs = { [key]: array ? [bytes, bytes] : bytes, [mimeKey]: "audio/wav", safe: "ordinary application text" };
  const source = fake(attrs); source.resource.attributes = attrs; source.instrumentationScope.attributes = attrs;
  source.events = [{ name: "payload", attributes: attrs }]; source.links = [{ context: source._spanContext, attributes: attrs }];
  const before = JSON.stringify(source), clean = privacyFilter().span(source);
  expect(JSON.stringify(clean)).not.toContain(bytes); expect(JSON.stringify(source)).toBe(before);
  for (const actual of [clean.attributes, clean.resource.attributes, (clean.instrumentationScope as any).attributes, clean.events[0].attributes!, clean.links[0].attributes!]) {
    expect(actual[key]).toEqual(array ? ["<omitted: 8 base64 chars>", "<omitted: 8 base64 chars>"] : "<omitted: 8 base64 chars>");
    expect(actual[mimeKey]).toBe("audio/wav"); expect(actual.safe).toBe("ordinary application text");
  }
});
test.each(["audio", "input_audio"].flatMap(key => [false, true].map(array => ({ key, array }))))("ROUND5 JSON audio leaves $key (array $array) are scrubbed from every forwarded surface", ({ key, array }) => {
  const bytes = "U0VDUkVU", payload = JSON.stringify({ [key]: array ? [bytes, bytes] : bytes, safe: "ordinary application text" });
  const source = fake({ "input.value": payload }); source.status = { code: 2, message: `400 ${payload}` };
  source.events = [{ name: payload, attributes: { "exception.stacktrace": payload } }]; source.links = [{ context: source._spanContext, attributes: { payload } }];
  source.resource.attributes = { payload }; source.instrumentationScope = { name: payload, version: payload, attributes: { payload } };
  const before = JSON.stringify(source), clean = privacyFilter().span(source);
  expect(JSON.stringify(clean)).not.toContain(bytes); expect(JSON.stringify(source)).toBe(before);
  expect(JSON.parse(String(clean.attributes["input.value"]))).toEqual({ [key]: array ? ["<omitted: 8 base64 chars>", "<omitted: 8 base64 chars>"] : "<omitted: 8 base64 chars>", safe: "ordinary application text" });
});
test("S6 supersedes ROUND5: image MIME payload is omitted while audio metadata retains ordinary content", () => {
  const attrs = { "input.parts.0.inlineData.data.0": "U0VDUkVU", "input.parts.0.inlineData.mimeType": "image/png", "gen_ai.audio.transcript": "ordinary transcript", "gen_ai.audio.mime_type": "audio/wav", "gen_ai.audio.language": "en", "gen_ai.audio.duration_secs": 1.25 };
  expect(privacyFilter().span(fake(attrs)).attributes).toEqual({ ...attrs, "input.parts.0.inlineData.data.0": "<omitted: 8 base64 chars>" });
});
test.each(["input.images.0", "input.images.0.0", "output.images.1", "input.parts.0.bytes.0"])("ROUND5 structural media scrubs indexed image field %s at the export boundary without mutating app spans", key => {
  const bytes = "QUJD".repeat(100), source = fake({ [key]: bytes, ordinary: "safe application text" });
  const clean = privacyFilter().span(source);
  expect(clean.attributes[key]).toBe("<omitted: 400 base64 chars>");
  expect(clean.attributes.ordinary).toBe("safe application text"); expect(source.attributes[key]).toBe(bytes);
});
test.each(["data:image/svg+xml,%3Csvg%3Eprivate-image%3C%2Fsvg%3E", "data:image/png;charset=utf-8,percent%2Cencoded-image", "DATA:image/svg+xml,%3Csvg%2F%3E", "data:image/png;base64,QUJD%2Bprivate-image", "data:text/plain,private%20inline%20data"].flatMap(uri =>
  [undefined, 4096].map(base64ImageMaxLength => ({ uri, base64ImageMaxLength }))
))("ROUND5 non-base64 data URI $uri is scrubbed from every forwarded surface (image cap $base64ImageMaxLength)", ({ uri, base64ImageMaxLength }) => {
  const source = fake({ echoed: uri }); source.status = { code: 2, message: uri };
  source.events = [{ name: uri, attributes: { echoed: uri } }]; source.links = [{ context: source._spanContext, attributes: { echoed: uri } }];
  source.resource.attributes = { echoed: uri }; source.instrumentationScope = { name: uri, version: uri, attributes: { echoed: uri } };
  const before = JSON.stringify(source), clean = privacyFilter({ base64ImageMaxLength }).span(source);
  expect(JSON.stringify(clean)).not.toContain(uri); expect(JSON.stringify(source)).toContain(uri);
  // F2 cap-0 whole-scope replacement precedes I1 media markers for images.
  // Non-image data URIs keep S6 at both caps. At the explicit positive cap,
  // S6 still counts once-percent-decoded base64, including URL-safe characters.
  const expected = base64ImageMaxLength === undefined && /^data:image\//i.test(uri)
    ? "[image-redacted]" : uri.includes("QUJD%2B") ? "<omitted: 18 base64 chars>" : "<omitted: data URI>";
  expect(clean.attributes.echoed).toBe(expected);
  for (const value of [clean.resource.attributes.echoed, (clean.instrumentationScope as any).attributes.echoed,
    clean.events[0].attributes!.echoed, clean.links[0].attributes!.echoed, clean.status.message,
    clean.events[0].name, clean.instrumentationScope.name, clean.instrumentationScope.version]) expect(value).toBe(expected);
  for (const payload of [uri.slice(uri.indexOf(",") + 1), decodeURIComponent(uri.slice(uri.indexOf(",") + 1))]) expect(JSON.stringify(clean)).not.toContain(payload);
  expect(JSON.stringify(source)).toBe(before);
});
function fake(attributes: any): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext() { return ctx; }, name: "application", kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], attributes, status: { code: 1 }, events: [], links: [], resource: { attributes: {} }, instrumentationScope: { name: "app" } };
}
test.each(CREDENTIAL_HEADERS)("S5 suppressed fetch harvests %s on unrelated routes without inspecting a body or changing identity", async name => {
  const original = globalThis.fetch, response = new Response("untouched"), promise = Promise.resolve(response), body = jest.fn(() => { throw new Error("body must not be inspected"); });
  const secret = `suppressed-transport-private-${name}`;
  const init = Object.defineProperty({ method: "POST", headers: { [name]: /authorization$/.test(name) ? `Bearer ${secret}` : secret } }, "body", { get: body });
  const target = jest.fn(() => promise); globalThis.fetch = target;
  const emit = jest.fn(), hook = createFetchHook(4096, emit, { active: () => null, skip: () => true, run: (_p, work) => work(), observeRequest: observeOutgoing });
  try {
    expect(fetch("http://127.0.0.1/unrelated?%74oken=query-private-token", init)).toBe(promise);
    expect(await promise).toBe(response); expect(response.bodyUsed).toBe(false); expect(response.body!.locked).toBe(false);
    expect(getRegisteredSecrets()).toContain(secret); expect(getRegisteredSecrets()).toContain("query-private-token");
    expect(privacyFilter().attributes({ echoed: secret }).echoed).toBe("<redacted>");
    expect(body).not.toHaveBeenCalled(); expect(emit).not.toHaveBeenCalled(); expect(target).toHaveBeenCalledTimes(1);
  } finally { hook.remove(); globalThis.fetch = original; }
});
test.each(["client_secret", "password", "authorization_token", ...CREDENTIAL_HEADERS])("S5 credential-named query %s uses the shared discovery/sanitizer policy", name => {
  const secret = `query-credential-private-${name}`;
  const url = `http://127.0.0.1/plain?${encodeURIComponent(name)}=${encodeURIComponent(secret)}`;
  observeOutgoing({ method: "GET", url, headers: {} });
  expect(getRegisteredSecrets()).toContain(secret);
  expect(privacyFilter().attributes({ url, echoed: secret }).echoed).toBe("<redacted>");
});
test("S5 suppressed synchronous fetch failure preserves exact error while harvesting headers", () => {
  const original = globalThis.fetch, error = new Error("original transport error"); globalThis.fetch = () => { throw error; };
  const hook = createFetchHook(4096, () => {}, { active: () => null, skip: () => true, run: (_p, work) => work(), observeRequest: observeOutgoing });
  try { expect(() => fetch("http://127.0.0.1/", { headers: { "x-api-key": "sync-failure-private-token" } })).toThrow(error); expect(getRegisteredSecrets()).toContain("sync-failure-private-token"); }
  finally { hook.remove(); globalThis.fetch = original; }
});
test.each(["http", "undici-fetch", "undici-request"])("S5 direct cached %s credentials are harvested without outgoing spans or changed response bytes", async mode => {
  const undici = require("undici");
  const server = createServer((req, res) => { res.end(req.headers["anthropic-api-key"] + " " + req.url); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  const address = server.address() as { port: number }, url = `http://127.0.0.1:${address.port}/plain?token=direct-query-private-token`;
  const call = async (secret: string) => {
    if (mode === "http") return new Promise<string>((done, reject) => {
      const req = request(url, { headers: { "anthropic-api-key": "initial-unused-private-token" } }, res => { let text = ""; res.setEncoding("utf8"); res.on("data", chunk => { text += chunk; }); res.on("end", () => done(text)); });
      req.setHeader("anthropic-api-key", secret); req.on("error", reject); req.end();
    });
    const response = mode === "undici-fetch" ? await undici.fetch(url, { headers: { "anthropic-api-key": secret } }) : await undici.request(url, { headers: { "anthropic-api-key": secret } });
    return mode === "undici-fetch" ? response.text() : response.body.text();
  };
  let remove: (() => void) | undefined;
  try {
    const secret = `direct-${mode}-private-token`, baseline = await call(secret);
    const originalRequest = request, originalFetch = undici.fetch; remove = observeTransports();
    expect(await call(secret)).toBe(baseline); expect(request).toBe(originalRequest); expect(undici.fetch).toBe(originalFetch);
    expect(getRegisteredSecrets()).toContain(secret); expect(getRegisteredSecrets()).toContain("direct-query-private-token");
    remove(); remove = undefined; const after = `after-cleanup-${mode}-private-token`; await call(after); expect(getRegisteredSecrets()).not.toContain(after);
  } finally { remove?.(); server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); }
});
test.each(["clone", "released-reader"])("documented OTHER %s cancellation keeps application baseline parity despite premature capture finalization", async mode => {
  const original = globalThis.fetch, text = '{"choices":[{"message":{"content":"answer"}}],"usage":{"prompt_tokens":1,"completion_tokens":2}}';
  const run = async (instrumented: boolean) => {
    const response = new Response(text, { headers: { "content-type": "application/json" } }), spans: any[] = [];
    globalThis.fetch = () => Promise.resolve(response);
    const hook = instrumented ? createFetchHook(4096, span => spans.push(span), { active: () => null, skip: () => false, run: (_p, work) => work() }) : undefined;
    try {
      const got = await fetch("https://api.openai.com/v1/chat/completions", { method: "POST", body: '{"messages":[]}' });
      let error: { name: string; message: string } | undefined;
      if (mode === "clone") { const promise = got.clone().body!.cancel("unused clone"); void promise.catch(() => {}); }
      else {
        const reader = got.body!.getReader(); reader.releaseLock();
        try { await reader.cancel("released reader"); } catch (e: any) { error = { name: e.name, message: e.message }; }
      }
      const beforeReading = spans.length, body = await got.text();
      return { application: { body, error, bodyUsed: got.bodyUsed }, beforeReading, spans };
    } finally { hook?.remove(); }
  };
  try {
    const baseline = await run(false), actual = await run(true);
    expect(actual.application).toEqual(baseline.application); expect(actual.beforeReading).toBe(1); expect(actual.spans).toHaveLength(1);
    expect(actual.spans[0].attributes["traceai.wire.incomplete"]).toBe(true); expect(actual.spans[0].attributes["output.value"]).toBeUndefined();
  } finally { globalThis.fetch = original; }
});
test("documented OTHER HTTP-200 provider-failure stream preserves baseline bytes but retains HTTP-derived successful status", async () => {
  const original = globalThis.fetch, text = 'data: {"type":"response.failed","response":{"status":"failed","error":{"message":"provider failed"}}}\n\n';
  const baseline = await new Response(text).text(), spans: any[] = [];
  globalThis.fetch = () => Promise.resolve(new Response(text, { headers: { "content-type": "text/event-stream" } }));
  const hook = createFetchHook(4096, span => spans.push(span), { active: () => null, skip: () => false, run: (_p, work) => work() });
  try {
    expect(await (await fetch("https://api.openai.com/v1/responses", { method: "POST", body: '{"model":"fixture"}' })).text()).toBe(baseline);
    expect(spans).toHaveLength(1); expect(spans[0].ok).toBe(true); expect(spans[0].description).toBeUndefined();
    expect(spans[0].attributes["gen_ai.response.finish_reasons"]).toEqual(["failed"]);
  } finally { hook.remove(); globalThis.fetch = original; }
});
function child(mode: string, agent = true) {
  const env = { ...process.env, NODE_OPTIONS: "", OTEL_SDK_DISABLED: "false", MODE: mode, AGENT: agent ? "true" : "false", SYNTHETIC_AUTH: "synthetic-environment-credential" };
  const result = spawnSync(process.execPath, [resolve(__dirname, "apps/round5.cjs")], { cwd: root, env, encoding: "utf8", timeout: 20000 });
  expect(result.error).toBeUndefined(); expect(result.status).toBe(0);
  return JSON.parse(result.stdout.trim());
}
test("ROUND5 initialized exporter scrubs structural media while retaining baseline application spans and content defaults", () => {
  const baseline = child("structural-media", false), actual = child("structural-media");
  expect(actual.application).toEqual(baseline.application); expect(actual.appUnchanged).toBe(true);
  expect(actual.fiSpans).toBe(10); expect(actual.protected).toBe(true); expect(actual.safeRetained).toBe(true);
});
test.each(["openai-owned", "openai-preinstalled"])("S5 %s SDK 401 matches baseline and scrubs unrelated spans after covered transport observation", mode => {
  const baseline = child(mode, false), actual = child(mode);
  expect(actual.application).toEqual(baseline.application); expect(actual.sameWrapper).toBe(true);
  expect(actual.fiSpans).toBe(2); expect(actual.wireSpans).toBe(0); expect(actual.protected).toBe(true); expect(actual.appUnchanged).toBe(true);
});
test("S5 CJS, ESM and embedded package copies use the same registry and explicit API", () => {
  expect(child("copies")).toEqual({ shared: true, exported: true, protected: true });
});
test.each(["early-body-duplicate", "early-body-cropped", "early-body-truncated"])("S5 %s registers parsed body credentials before any response consumption and matches baseline", mode => {
  const baseline = child(mode, false), actual = child(mode);
  expect(actual.application).toEqual(baseline.application); expect(actual.appUnchanged).toBe(true);
  expect(actual.beforeKnown).toBe(true); expect(actual.beforeProtected).toBe(true); expect(actual.beforeCount).toBe(1); expect(actual.safeRetained).toBe(true);
  // A truncated body on an unknown loopback provider correctly creates no wire
  // span; its already-parsed credentials must still protect application spans.
  expect(actual.wire).toBe(mode === "early-body-truncated" ? 0 : 1);
});
test.each(["fetch-long-query", "http-long-query", "undici-long-query", "undici-request-long-query"])("S5 %s protects oversized query credentials process-wide before response consumption without changing baseline", mode => {
  const baseline = child(mode, false), actual = child(mode);
  expect(actual.application).toEqual(baseline.application); expect(actual.appUnchanged).toBe(true);
  expect(actual.beforeKnown).toBe(true); expect(actual.beforeProtected).toBe(true); expect(actual.beforeCount).toBe(1); expect(actual.safeRetained).toBe(true); expect(actual.wire).toBe(0);
});
test.each(["request", "stream"])("round4 #2 opaque %s bodies preserve baseline bytes and remain unparsed (ROUND5 scoped limitation)", mode => {
  const baseline = child(mode, false), actual = child(mode);
  expect(actual.application).toEqual(baseline.application); expect(actual.unparsed).toBe(true); expect(actual.bodyUnavailable).toBe(true);
});
