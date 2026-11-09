import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { projectAttributes } from "../src/attribute-projection";
import { createFetchHook } from "../src/wire/fetch-runtime";
import { observeOutgoing } from "../src/wire/observe-node";
import { getRegisteredSecrets } from "@traceai/fi-core";
import { privacyFilter } from "../src/export-privacy";
import { streamingMilestones } from "../src/wire/stream-timing";

function child(mode: string, source: boolean, limit = 128) {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(env)) if (/^(?:FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { MODE: mode, LIMIT: String(limit), VERIFY_SOURCE: source ? "1" : "0", FI_PROFILE: "traceai-s6-synthetic-missing-profile", OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: String(limit), OTEL_SERVICE_NAME: "application-service", OTEL_RESOURCE_ATTRIBUTES: "custom.identity=environment-resource", TS_NODE_PROJECT: resolve(__dirname, "../tsconfig.json") });
  if (mode === "ignored-half") Object.assign(env, { FI_API_KEY: "environment-fi-api-key-private", FI_SECRET_KEY: "environment-fi-secret-key-private" });
  const args = [...(source ? ["-r", require.resolve("ts-node/register/transpile-only")] : []), resolve(__dirname, "apps/round6.cjs")];
  const result = spawnSync(process.execPath, args, { cwd: resolve(__dirname, ".."), env, encoding: "utf8", timeout: 20000 });
  if (result.status !== 0) throw new Error(`${mode}/${source}: ${result.error ?? ""}\n${result.stderr}\n${result.stdout}`);
  return JSON.parse(result.stdout.trim());
}
test.each([false, true])("S7 ignored explicit FI half seeds before selected config (%s source)", source => {
  expect(child("ignored-half", source)).toEqual({ ignoredRegistered: true, originalUnchanged: true, protected: true });
});
test.each([false, true].flatMap(source => ["exporter-only", "processor-only", "host-only"].map(mode => ({ source, mode }))))("A11 neutral $mode works without FI credentials ($source source)", ({ source, mode }) => {
  expect(child(mode, source)).toMatchObject({ active: true, received: true });
});
const projectionCases = [false, true].flatMap(source => [16, 128].flatMap(limit => ["owned-projection", "host-projection"].map(mode => ({ source, mode, limit }))));
// Spec A7 rule 4: the 17 protected wire attributes exceed a 16-slot host, so it keeps the
// family-ordered protected prefix (markers, identity, outcome, model, usage) and no content.
// NOT_RUN: fi-security Q9 Candidate 2 (6a3aa0a3); auto-mode projection withdrawn. Owner: fi-observe; 2026-11-05.
test.skip.each(projectionCases)("A3/A6/A7 $mode preserves markers, usage, drop counts and (capacity permitting) output at $limit ($source source)", ({ source, mode, limit }) => {
  expect(child(mode, source, limit)).toMatchObject({ projected: true, output: limit >= 128 ? "answer" : null, layer: limit >= 128 ? "wire" : null, incomplete: true, service: mode === "host-projection" ? "host-application" : "application-service" });
});
test("A7 projection is insertion-order-independent and preserves overflow metadata separately from hidden content", () => {
  const attrs = { ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${i}.message.content`, "private content"])), "output.value": "visible answer", "gen_ai.usage.input_tokens": 91, "traceai.wire.incomplete": true, "gen_ai.provider.name": "openai_compatible" };
  expect(projectAttributes(attrs, 16)).toEqual(projectAttributes(Object.fromEntries(Object.entries(attrs).reverse()), 16));
  const projected = projectAttributes(attrs, 16);
  expect(projected.attributes).toMatchObject({ "output.value": "visible answer", "traceai.wire.incomplete": true, "gen_ai.usage.input_tokens": 91 });
  expect(projected.dropped).toBeGreaterThan(0);
  expect(JSON.stringify(privacyFilter({ hideInputs: true }).attributes(projected.attributes))).not.toContain("private content");
});
test.each([16, 8192])("S5 materialized URLSearchParams below/above capture cap %i harvest uncropped and duplicate secrets", async max => {
  const original = globalThis.fetch, promise = Promise.resolve(new Response("unchanged")), secret = `form-credential-private-${max}`;
  globalThis.fetch = () => promise;
  const hook = createFetchHook(max, () => {}, { active: () => null, skip: () => false, run: (_p, work) => work(), observeRequest: observeOutgoing });
  try {
    const body = new URLSearchParams([["model", "fixture"], ["session_token", secret], ["session_token", secret + "-duplicate"]]);
    expect(fetch("http://127.0.0.1/v1/chat/completions", { method: "POST", body })).toBe(promise);
    expect(await (await promise).text()).toBe("unchanged");
    expect(getRegisteredSecrets()).toContain(secret); expect(getRegisteredSecrets()).toContain(secret + "-duplicate");
    expect(privacyFilter().attributes({ echo: secret }).echo).toBe("<redacted>");
  } finally { hook.remove(); globalThis.fetch = original; }
});
test("A10 heartbeat and split UTF-8 text retain separate transport arrivals and first-text time", async () => {
  const original = globalThis.fetch, spans: any[] = [], encoder = new TextEncoder();
  const heartbeat = encoder.encode('data: {"type":"heartbeat"}\r\n\r\n');
  const text = encoder.encode('data: {"choices":[{"delta":{"content":"héllo"}}]}\r\n\r\n');
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(new ReadableStream<Uint8Array>({ start(c) { controller = c; } }), { headers: { "content-type": "text/event-stream" } });
  globalThis.fetch = () => Promise.resolve(response);
  const hook = createFetchHook(8192, span => spans.push(span), { active: () => null, skip: () => false, run: (_p, work) => work() });
  try {
    const got = await fetch("http://127.0.0.1/v1/chat/completions", { method: "POST", body: '{"messages":[]}' });
    const reader = got.body!.getReader(), received: Uint8Array[] = [];
    controller.enqueue(heartbeat); received.push((await reader.read()).value!);
    await new Promise(done => setTimeout(done, 120));
    const split = text.indexOf(0xc3) + 1;
    controller.enqueue(text.subarray(0, split)); received.push((await reader.read()).value!);
    controller.enqueue(text.subarray(split)); received.push((await reader.read()).value!);
    controller.close(); expect((await reader.read()).done).toBe(true);
    expect(Buffer.concat(received).equals(Buffer.concat([heartbeat, text]))).toBe(true);
    expect(got).toBe(response); expect(spans).toHaveLength(1);
    const attrs = spans[0].attributes;
    expect(attrs["output.value"]).toBe("héllo");
    expect(attrs["gen_ai.response.time_to_first_text"] - attrs["gen_ai.response.time_to_first_chunk"]).toBeGreaterThan(0.09);
    expect(attrs["traceai.stream.event_arrival_seconds"]).toHaveLength(2);
    expect(attrs["traceai.stream.event_arrival_seconds"][1]).toBe(attrs["gen_ai.response.time_to_first_text"]);
  } finally { hook.remove(); globalThis.fetch = original; }
});
test.each([
  { type: "reasoning", body: 'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n', sse: true },
  { type: "tool", body: 'data: {"type":"content_block_delta","delta":{"partial_json":"{}"}}\n\n', sse: true },
  { type: "audio", body: 'data: {"type":"response.output_audio.delta","delta":"QUJD"}\n\n', sse: true },
  { type: "ndjson", body: '{"response":"hello"}\n', sse: false },
])("A10 first nonempty $type delta uses event-arrival timestamps", ({ body, sse }) => {
  const bytes = new TextEncoder().encode(body);
  expect(streamingMilestones(bytes, sse, [{ end: bytes.length, seconds: 0.123 }])).toEqual({ events: [0.123], firstText: 0.123 });
});
test("A10 bounded transport timestamps do not fabricate a later event's arrival", () => {
  const first = 'data: {"type":"heartbeat"}\n\n', second = 'data: {"choices":[{"delta":{"content":"late"}}]}\n\n';
  expect(streamingMilestones(new TextEncoder().encode(first + second), true, [{ end: Buffer.byteLength(first), seconds: 0.1 }])).toEqual({ events: [0.1], firstText: undefined, incomplete: true });
});
