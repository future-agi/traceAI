import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { BasicTracerProvider, SimpleSpanProcessor, type ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { FITracer, rememberSpanValueLimit } from "@traceai/fi-core";
import { privacyFilter } from "../src/export-privacy";
import { build, match } from "../src/wire/core";

// Synthetic fixture data only; no request is sent to this URL.
const secret = "k9V7h2qR4p8Z_I1_R4_SYNTHETIC_CREDENTIAL";
function candidate(message: Record<string, unknown> = { role: "assistant", content: secret }) {
  const url = "https://api.openai.com/v1/chat/completions";
  const body = Buffer.from(JSON.stringify({
    id: "response-fixture", model: "fixture-model",
    choices: [{ message, finish_reason: "stop" }],
    usage: { prompt_tokens: 101, completion_tokens: 7, total_tokens: 108 },
  }));
  const result = build(match("POST", url)!, {
    method: "POST", url, headers: { "content-type": "application/json" }, secrets: [secret],
    body: Buffer.from(JSON.stringify({ model: "fixture-model", messages: Array.from({ length: 70 }, (_, i) => ({ role: "user", content: `message ${i}` })) })),
  }, { status: 200, headers: { "content-type": "application/json" }, body, size: body.length });
  expect(result).toBeDefined();
  return result!;
}

function expectNoCredential(value: unknown) {
  const serialized = JSON.stringify(value);
  expect(serialized).not.toContain(secret);
  expect(serialized).not.toContain("k9V7h2qR4p8Z");
}

test("I1 R4 canonical text follows protected usage and precedes message fan-out", () => {
  const result = candidate(), attrs = result.attributes;
  expect(Object.keys(attrs).slice(0, 19)).toEqual([
    "gen_ai.span.kind", "gen_ai.operation.name", "gen_ai.provider.name", "traceai.capture.layer",
    "traceai.wire.endpoint", "server.address", "server.port", "http.request.method",
    "http.response.status_code", "gen_ai.request.model", "gen_ai.response.model", "gen_ai.response.id",
    "gen_ai.response.finish_reasons", "gen_ai.request.parameters", "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens", "output.value", "output.mime_type",
  ]);
  expect(attrs).toMatchObject({
    "output.value": "<redacted>", "output.mime_type": "text/plain",
    "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108,
    "gen_ai.input.messages.0.message.content": "message 0",
    "gen_ai.input.messages.69.message.content": "message 69",
    "gen_ai.output.messages.0.message.content": "<redacted>",
  });
  expect(Object.keys(attrs).filter(key => /^gen_ai\.input\.messages\.\d+\.message\.(role|content)$/.test(key))).toHaveLength(140);
  expectNoCredential(result);
});

test("I1 R4 tool-only canonical JSON keeps its literal value and priority", () => {
  const result = candidate({ role: "assistant", tool_calls: [{ id: "call-1", type: "function", function: { name: "lookup", arguments: secret } }] });
  const keys = Object.keys(result.attributes);
  expect(result.attributes["output.value"]).toBe('[{"id":"call-1","name":"lookup","arguments":"<redacted>"}]');
  expect(result.attributes["output.mime_type"]).toBe("application/json");
  expect(keys.indexOf("gen_ai.usage.total_tokens")).toBeLessThan(keys.indexOf("output.value"));
  expect(keys.indexOf("output.value")).toBeGreaterThanOrEqual(0);
  expect(keys.indexOf("output.mime_type")).toBe(keys.indexOf("output.value") + 1);
  expect(keys.indexOf("output.mime_type")).toBeLessThan(keys.indexOf("gen_ai.input.messages.0.message.role"));
  expect(keys.indexOf("output.mime_type")).toBeLessThan(keys.indexOf("gen_ai.output.messages.0.message.role"));
  expectNoCredential(result);
});

test.each([128, 1])("I1 R4 native and detached output honor real host capacity %s without replay", async limit => {
  const app: ReadableSpan[] = [];
  const provider = new BasicTracerProvider({
    spanLimits: { attributeCountLimit: limit, attributeValueLengthLimit: 12 },
    spanProcessors: [new SimpleSpanProcessor({ export(spans, done) { app.push(...spans); done({ code: 0 }); }, async shutdown() {} })],
  });
  try {
    const result = candidate(), beforeCandidate = JSON.stringify(result);
    const privacy = privacyFilter(undefined, [secret]);
    // Use the production admission/privacy/facade APIs, never a test-side count projection.
    const admitted = privacy.attributes(result.attributes, undefined, result.attributes["gen_ai.operation.name"]);
    const tracer = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("i1-r4-wire") });
    const span = tracer.startSpan(result.name, { attributes: admitted });
    const native = span.getContextSpan() as unknown as ReadableSpan;
    rememberSpanValueLimit(native);
    span.end();
    await provider.forceFlush();
    expect(app).toHaveLength(1);
    expect(app[0]).toBe(native);
    const beforeNative = JSON.stringify({ attributes: native.attributes, dropped: native.droppedAttributesCount });
    const detached = privacy.span(native, []);
    expect(detached).not.toBe(native);
    expect(detached.attributes).not.toBe(native.attributes);
    expect(Object.keys(native.attributes)).toHaveLength(limit);
    expect(native.droppedAttributesCount).toBeGreaterThan(0);
    expect(detached.droppedAttributesCount).toBe(native.droppedAttributesCount);
    if (limit === 128) {
      expect(native.attributes).toMatchObject({ "output.value": "<redacted>", "output.mime_type": "text/plain" });
      const unknown = "<unknown: host-truncated>";
      const enums: Record<string, readonly string[]> = {
        "fi.span.kind": ["LLM", "CHAIN", "TOOL", "AGENT", "RETRIEVER", "EMBEDDING", "RERANKER", "GUARDRAIL", "EVALUATOR"],
        "gen_ai.span.kind": ["LLM", "CHAIN", "TOOL", "AGENT", "RETRIEVER", "EMBEDDING", "RERANKER", "GUARDRAIL", "EVALUATOR"],
        "gen_ai.operation.name": ["chat", "text_completion", "embeddings", "create_agent", "invoke_agent", "execute_tool", "generate_content", "retrieval", "text_to_speech", "speech_to_text"],
      };
      const counts = new Set(["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens", "gen_ai.usage.cache_read_tokens", "gen_ai.usage.cache_creation_tokens", "gen_ai.usage.input_tokens.audio", "gen_ai.usage.output_tokens.audio", "gen_ai.usage.output_tokens.reasoning", "traceai.capture.dropped_content_count"]);
      const booleans = new Set(["traceai.wire.truncated", "traceai.wire.incomplete", "traceai.wire.body_unavailable", "traceai.capture.host_data_unknown"]);
      const nonContent = (key: string, value: unknown) => typeof value === "string" && enums[key]?.includes(value) || typeof value === "number" && Number.isSafeInteger(value) && (counts.has(key) && value >= 0 || key === "http.response.status_code" && value >= 100 && value <= 599) || typeof value === "boolean" && booleans.has(key);
      const unknownMap = (source: Record<string, any>) => Object.fromEntries(Object.entries(source).map(([key, value]) => [key, key === "session.id" ? "<redacted>" : nonContent(key, value) ? value : unknown]));
      expect(detached.spanContext()).toEqual(native.spanContext());
      expect(detached.name).toBe(unknown);
      expect(detached.attributes).toEqual({ ...unknownMap(native.attributes), "traceai.capture.host_data_unknown": true });
      expect(detached.attributes["output.value"]).toBe(unknown);
      expect(detached.attributes).toMatchObject({ "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108 });
      expect(detached.resource.attributes).toEqual(unknownMap(native.resource.attributes));
      expect(detached.instrumentationScope).toEqual({ name: unknown, version: unknown, schemaUrl: unknown, attributes: unknownMap((native.instrumentationScope as { attributes?: Parameters<typeof unknownMap>[0] }).attributes ?? {}) });
      expect(detached.status).toEqual({ code: native.status.code, ...(native.status.message === undefined ? {} : { message: unknown }) });
      expect(native.events).toEqual([]); expect(detached.events).toEqual([]);
      expect(native.links).toEqual([]); expect(detached.links).toEqual([]);
      expect(detached.startTime).toEqual(native.startTime); expect(detached.endTime).toEqual(native.endTime);
      expect(detached.droppedEventsCount).toBe(native.droppedEventsCount); expect(detached.droppedLinksCount).toBe(native.droppedLinksCount);
    } else {
      // The producer has output, but the host genuinely dropped it. Export must not invent it.
      expect(result.attributes["output.value"]).toBe("<redacted>");
      for (const attrs of [native.attributes, detached.attributes]) {
        expect(Object.prototype.hasOwnProperty.call(attrs, "output.value")).toBe(false);
        expect(Object.prototype.hasOwnProperty.call(attrs, "output.mime_type")).toBe(false);
      }
    }
    for (const copy of [native, detached]) expectNoCredential({ name: copy.name, attributes: copy.attributes, status: copy.status, events: copy.events, links: copy.links });
    expect(JSON.stringify({ attributes: native.attributes, dropped: native.droppedAttributesCount })).toBe(beforeNative);
    expect(JSON.stringify(result)).toBe(beforeCandidate);
  } finally { await provider.shutdown(); }
});

// A child isolates auto's process-wide initialization. The actual enable.cjs fixture
// is used through the production registry, with real target visibility as in round4.
const registryScript = String.raw`
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { FITracer } = require('@traceai/fi-core');
const selectedSpans = [], globalSpans = [], fi = [], received = [];
const sink = list => ({ export(spans, done) { list.push(...spans); done({ code: 0 }); }, async shutdown() {} });
const selected = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(sink(selectedSpans))] });
const unrelated = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(sink(globalSpans))] });
assert.equal(trace.setGlobalTracerProvider(unrelated), true);
const auto = process.env.VERIFY_SOURCE === '1' ? require('./src/index.ts') : require('@traceai/auto');
const fixturePath = path.resolve('test/apps/enable.cjs');
const fixture = require(fixturePath), originalEnable = fixture.enable;
if (originalEnable) fixture.enable = function(options) { received.push(options); return Reflect.apply(originalEnable, this, arguments); };
const originalResolve = Module._resolveFilename;
let mastraLoads = 0;
(async () => {
  try {
    require.resolve('genkit'); require.resolve('ai');
    assert(['@mastra/core', '@mastra/observability', '@mastra/otel-exporter'].some(name => { try { require.resolve(name); return true; } catch { return false; } }), 'Mastra target visibility required');
    Module._resolveFilename = function(request, ...args) {
      if (request === '@traceai/mastra' || request === '@traceai/mastra/auto') mastraLoads++;
      return ['@traceai/vercel', '@traceai/mastra', '@traceai/genkit'].some(name => request === name || request === name + '/auto') ? fixturePath : Reflect.apply(originalResolve, this, [request, ...args]);
    };
    const returned = auto.init({ provider: selected, exporter: sink(fi), apiKey: '', secretKey: '', configFile: '/does-not-exist/i1-r4-wire-config', instrumentations: ['vercel', 'mastra', 'genkit'] });
    const agent = globalThis[Symbol.for('traceai.auto.agent.v1')];
    assert.equal(returned, selected); assert.equal(agent.provider, selected); assert.equal(auto.report().active, true);
    assert.equal(trace.getTracerProvider().getDelegate(), unrelated);
    const rows = auto.report().integrations.filter(row => ['vercel', 'mastra', 'genkit'].includes(row.name)).sort((a, b) => a.name.localeCompare(b.name));
    assert.deepEqual(rows.map(row => row.name), ['genkit', 'mastra', 'vercel']);
    const expected = process.env.ENABLE_MODE === 'present' ? 'instrumented' : process.env.ENABLE_MODE === 'absent' ? 'manual' : 'failed';
    assert.deepEqual(rows.map(row => row.status), [expected, 'manual', expected]);
    assert.equal(rows[1].reason, 'Mastra zero-code bridge follows after 11-11; use createFIObservability / createFIMastraExporter from @traceai/mastra');
    assert.equal(mastraLoads, 0);
    const calls = process.env.ENABLE_MODE === 'absent' ? 0 : 2;
    assert.equal(globalThis.__traceai_enable_calls || 0, calls); assert.equal(received.length, calls);
    for (const [i, options] of received.entries()) {
      assert.notEqual(options.tracerProvider, selected);
      assert.equal(Object.getPrototypeOf(options.tracerProvider), selected);
      assert.equal(options.spanProcessor, agent.processor);
      const tracer = options.tracerProvider.getTracer('i1-r4-selected');
      assert.equal(Object.getPrototypeOf(tracer), selected.getTracer('i1-r4-selected'));
      tracer.startSpan('optional-selected-' + i).end();
    }
    if (originalEnable) {
      // Both controls must fail the fixture assertion, not its intentional throw-mode error.
      for (const provider of [selected, FITracer.autoProvider(unrelated)]) {
        assert.throws(() => originalEnable({ spanProcessor: agent.processor, tracerProvider: provider }), { code: 'ERR_ASSERTION' });
      }
      assert.equal(globalThis.__traceai_enable_calls, 2);
    }
    await auto.forceFlush();
    assert.equal(selectedSpans.length, calls); assert.equal(fi.length, calls); assert.equal(globalSpans.length, 0);
    console.log(JSON.stringify({ statuses: rows.map(row => row.status), calls: globalThis.__traceai_enable_calls || 0, mastraLoads, returnedNative: returned === selected, selectedSpans: selectedSpans.length, globalSpans: globalSpans.length, fiSpans: fi.length }));
  } finally {
    Module._resolveFilename = originalResolve;
    await auto.shutdown(); await selected.shutdown(); await unrelated.shutdown(); trace.disable();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
`;

const registryCases = [
  { mode: "present", statuses: ["instrumented", "manual", "instrumented"], calls: 2, selectedSpans: 2, fiSpans: 2 },
  { mode: "absent", statuses: ["manual", "manual", "manual"], calls: 0, selectedSpans: 0, fiSpans: 0 },
  { mode: "throw", statuses: ["failed", "manual", "failed"], calls: 2, selectedSpans: 2, fiSpans: 2 },
];
test.each([false, true].flatMap(source => registryCases.map(entry => ({ source, ...entry }))))(
  "I1 R4 optional registry binds a selected facade and fails open ($mode, source=$source)",
  ({ source, mode, statuses, calls, selectedSpans, fiSpans }) => {
    const env = { ...process.env };
    for (const key of Object.keys(env)) if (/^(?:FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
    Object.assign(env, { ENABLE_MODE: mode, VERIFY_SOURCE: source ? "1" : "0", FI_PROFILE: "i1-r4-wire-synthetic-missing-profile", TS_NODE_PROJECT: resolve(__dirname, "../tsconfig.json") });
    const result = spawnSync(process.execPath, [...(source ? ["-r", require.resolve("ts-node/register/transpile-only")] : []), "-e", registryScript], {
      cwd: resolve(__dirname, ".."), env, encoding: "utf8", timeout: 25000,
    });
    if (result.error || result.status !== 0) throw new Error(`${mode}/${source}: ${result.error ?? ""}\n${result.stderr}\n${result.stdout}`);
    expect(JSON.parse(result.stdout.trim())).toEqual({ statuses, calls, mastraLoads: 0, returnedNative: true, selectedSpans, globalSpans: 0, fiSpans });
  }, 30000,
);
