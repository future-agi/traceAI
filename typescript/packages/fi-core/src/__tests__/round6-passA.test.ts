import { createServer } from "node:http";
import type { Attributes } from "@opentelemetry/api";
import type { AddressInfo } from "node:net";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { createSpanExporter, HTTPSpanExporter, register, FITracerProvider, Transport } from "../otel";
import { registerSecret } from "../secrets";
import { DROPPED_CONTENT_COUNT, PROJECTION_METADATA, projectAttributes } from "../attribute-projection";

// ROUND6 pass A (2026-11-01). The A7 cases assert the spec contract
// (spec/auto-instrumentation/README.md "A7 deterministic attribute projection"),
// not the previous Node capsule behaviour.
const LEGACY_COUNT = "traceai.attribute_projection.dropped_count";

const keys = (attrs: object) => Object.keys(attrs).sort();
function conversation(count: number): Record<string, string> {
  return Object.fromEntries(Array.from({ length: count }, (_, i) => [`gen_ai.input.messages.${i}.message.content`, `private message ${i}`]));
}

test("A7 protected families are kept as plain attributes with the spec content marker, never a capsule", () => {
  const protectedSet = {
    "gen_ai.span.kind": "LLM", "traceai.capture.layer": "wire", "traceai.wire.endpoint": "openai.chat", "server.address": "127.0.0.1", "server.port": 8080,
    "http.request.method": "POST", "http.response.status_code": 500, "error.type": "500", "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7,
    "gen_ai.usage.total_tokens": 108, "traceai.wire.timing_incomplete": true,
  };
  const attrs = { ...conversation(150), "output.value": "answer", ...protectedSet };
  const projected = projectAttributes(attrs, 16);
  expect(projected.attributes).toMatchObject(protectedSet);
  expect(projected.attributes).not.toHaveProperty([PROJECTION_METADATA]);
  expect(projected.attributes).not.toHaveProperty([LEGACY_COUNT]);
  expect(projected.attributes["output.value"]).toBe("answer");
  expect(keys(projected.attributes).length).toBeLessThanOrEqual(16);
  const contentKept = keys(projected.attributes).filter(key => key.startsWith("gen_ai.input.") || key === "output.value").length;
  expect(projected.attributes[DROPPED_CONTENT_COUNT]).toBe(151 - contentKept);
  // OTel's all-attribute counter is separate: every candidate not emitted.
  expect(projected.dropped).toBe(keys(attrs).length - (keys(projected.attributes).length - 1));
});

test("A7 output groups, then output value, then a contiguous newest input suffix; atomic groups; aggregate input follows its messages", () => {
  const attrs: Attributes = {
    "gen_ai.span.kind": "LLM", "gen_ai.usage.input_tokens": 3, "session.id": "application-session",
    "gen_ai.output.messages.0.message.role": "assistant", "gen_ai.output.messages.0.message.content": "answer",
    "output.value": "answer", "output.mime_type": "text/plain", "input.value": "{\"messages\":[]}", "input.mime_type": "application/json",
    "gen_ai.input.messages.9.message.role": "user", "gen_ai.input.messages.9.message.content": "newest",
    "gen_ai.input.messages.8.message.role": "assistant", "gen_ai.input.messages.8.message.content": "middle",
    "gen_ai.input.messages.8.message.tool_calls.0.tool_call.id": "call-1", "gen_ai.input.messages.8.message.tool_calls.0.tool_call.function.name": "lookup",
    "gen_ai.input.messages.7.message.content": "older and smaller",
  };
  const projected = projectAttributes(attrs, 11).attributes;
  expect(keys(projected)).toEqual(keys({
    "gen_ai.span.kind": 1, "gen_ai.usage.input_tokens": 1, [DROPPED_CONTENT_COUNT]: 1, "session.id": 1,
    "gen_ai.output.messages.0.message.role": 1, "gen_ai.output.messages.0.message.content": 1, "output.value": 1, "output.mime_type": 1,
    "gen_ai.input.messages.9.message.role": 1, "gen_ai.input.messages.9.message.content": 1,
  }));
  // Message 8 did not fit, so the smaller message 7 cannot fill the gap and the
  // aggregate input cannot restore the evicted conversation.
  expect(projected[DROPPED_CONTENT_COUNT]).toBe(7);
  for (let i = 0; i < 10; i++) {
    const kept = keys(projected).filter(key => key.startsWith(`gen_ai.input.messages.${i}.`));
    expect(kept.length === 0 || kept.length === keys(attrs).filter(key => key.startsWith(`gen_ai.input.messages.${i}.`)).length).toBe(true);
  }
  // A larger host keeps every group, the aggregate input and no marker.
  expect(projectAttributes(attrs, 64)).toEqual({ attributes: attrs, dropped: 0 });
});

test("A7 indexed tool definitions are atomic groups and never displace output", () => {
  const attrs = { "gen_ai.span.kind": "LLM", "output.value": "answer", "output.mime_type": "text/plain",
    "gen_ai.tool.definitions.0.tool.name": "lookup", "gen_ai.tool.definitions.0.tool.json_schema": "{}",
    "gen_ai.tool.definitions.1.tool.name": "search", "gen_ai.tool.definitions.1.tool.json_schema": "{}" };
  const projected = projectAttributes(attrs, 6).attributes;
  expect(projected).toMatchObject({ "output.value": "answer", "output.mime_type": "text/plain", "gen_ai.tool.definitions.0.tool.name": "lookup", "gen_ai.tool.definitions.0.tool.json_schema": "{}", [DROPPED_CONTENT_COUNT]: 2 });
  expect(projected).not.toHaveProperty(["gen_ai.tool.definitions.1.tool.name"]);
});

test("A7 a protected set larger than the host limit admits no content and keeps the family-ordered prefix", () => {
  const attrs = { "traceai.capture.layer": "wire", "gen_ai.usage.input_tokens": 9, "gen_ai.provider.name": "openai", "error.type": "500",
    "gen_ai.response.id": "response-fixture", "gen_ai.span.kind": "LLM", "traceai.wire.incomplete": true, "output.value": "answer" };
  const expected = { [DROPPED_CONTENT_COUNT]: 1, "traceai.wire.incomplete": true, "gen_ai.response.id": "response-fixture", "gen_ai.span.kind": "LLM", "error.type": "500" };
  expect(projectAttributes(attrs, 5).attributes).toEqual(expected);
  expect(projectAttributes(Object.fromEntries(Object.entries(attrs).reverse()), 5).attributes).toEqual(expected);
  expect(projectAttributes(attrs, 0)).toEqual({ attributes: {}, dropped: 8 });
});

test("A7 projection is insertion-order independent and accumulates a pre-existing content count", () => {
  const attrs = { ...conversation(40), "output.value": "answer", "gen_ai.usage.total_tokens": 4, [DROPPED_CONTENT_COUNT]: 5 };
  const forward = projectAttributes(attrs, 16), reverse = projectAttributes(Object.fromEntries(Object.entries(attrs).reverse()), 16);
  expect(reverse).toEqual(forward);
  const keptInput = keys(forward.attributes).filter(key => key.startsWith("gen_ai.input.")).length;
  expect(forward.attributes[DROPPED_CONTENT_COUNT]).toBe(5 + 40 - keptInput);
  // Newest suffix: the highest retained index is the newest message.
  expect(forward.attributes["gen_ai.input.messages.39.message.content"]).toBe("private message 39");
  expect(forward.attributes).not.toHaveProperty(["gen_ai.input.messages.0.message.content"]);
});

test("A7 the fail-closed candidate budget keeps protected fields, never content", () => {
  const attrs = { ...Object.fromEntries(Array.from({ length: 4100 }, (_, i) => [`gen_ai.input.messages.${i}.message.content`, "x"])), "gen_ai.span.kind": "LLM", "gen_ai.usage.input_tokens": 1 };
  expect(projectAttributes(attrs, 32)).toEqual({ attributes: { "gen_ai.span.kind": "LLM", "gen_ai.usage.input_tokens": 1, [DROPPED_CONTENT_COUNT]: 4100 }, dropped: 4100 });
});

// A2 hardening: the mocked-route test (round6.test.ts) cannot see the real OTLP
// serializer. These export through actual loopback collectors and scan raw bytes.
const savedEnv = { ...process.env };
afterEach(() => { process.env = { ...savedEnv }; });
async function collector(): Promise<{ url: string; bodies: Buffer[]; close: () => Promise<void> }> {
  const bodies: Buffer[] = [];
  const server = createServer((req, res) => {
    const parts: Buffer[] = [];
    req.on("data", part => parts.push(part)); req.on("end", () => { bodies.push(Buffer.concat(parts)); res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, bodies, close: () => new Promise(done => { server.closeAllConnections(); server.close(() => done()); }) };
}
function manualSpan(): any {
  const ctx = { traceId: "3".repeat(32), spanId: "4".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "manual-loopback", kind: 2, startTime: [1, 0], endTime: [2, 0], duration: [1, 0], _duration: [1, 0], ended: true,
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0, attributes: { echoed: "registered-loopback-private", "input.value": "private-loopback-input", safe: "visible-loopback" },
    status: { code: 2, message: "registered-loopback-private" }, events: [{ name: "registered-loopback-private", time: [1, 2], attributes: { echoed: "registered-loopback-private" } }], links: [],
    resource: resourceFromAttributes({ "service.name": "application-service" }), instrumentationScope: { name: "application" } };
}
test.each(["factory-http", "legacy-http", "register-default"])("A2 real loopback %s export carries no registered secret or hidden input", async route => {
  process.env.FI_HIDE_INPUTS = "true";
  registerSecret("registered-loopback-private");
  const sink = await collector();
  try {
    if (route === "register-default") {
      const provider: FITracerProvider = register({ endpoint: `${sink.url}/tracer/v1/traces`, headers: {}, projectName: "routing-project", setGlobalTracerProvider: false });
      const span = provider.getTracer("manual").startSpan("manual-loopback", { attributes: manualSpan().attributes });
      span.setStatus({ code: 2, message: "registered-loopback-private" }); span.addEvent("registered-loopback-private"); span.end();
      await provider.forceFlush(); await provider.shutdown();
    } else {
      const exporter = route === "legacy-http" ? new HTTPSpanExporter({ endpoint: `${sink.url}/tracer/v1/traces`, headers: {} }) : createSpanExporter({ endpoint: `${sink.url}/tracer/v1/traces`, headers: {}, transport: Transport.HTTP });
      await new Promise<void>(done => exporter.export([manualSpan()], () => done()));
      await exporter.shutdown();
    }
    const raw = Buffer.concat(sink.bodies).toString("latin1");
    expect(sink.bodies.length).toBeGreaterThan(0);
    expect(raw).toContain("visible-loopback");
    expect(raw).not.toContain("registered-loopback-private");
    expect(raw).not.toContain("private-loopback-input");
  } finally { await sink.close(); }
});

test("R-C07", async () => {
  for (const provider of [register({ endpoint: "http://127.0.0.1:1", headers: {}, projectName: "routing-project", setGlobalTracerProvider: false, batch: true }),
    register({ endpoint: "http://127.0.0.1:1", headers: {}, projectName: "routing-project", setGlobalTracerProvider: false, transport: Transport.GRPC, batch: true }),
    new FITracerProvider({ endpoint: "http://127.0.0.1:1", headers: {}, batch: true })]) {
    const processors = (provider as any)._activeSpanProcessor._spanProcessors;
    // RL2299h: explicit batch routes remain bounded through the dev registry.
    expect(processors[0]).toBeInstanceOf(require("@opentelemetry/sdk-trace-base").BatchSpanProcessor);
    expect(Number.isFinite(processors[0]._maxQueueSize) && processors[0]._maxQueueSize > 0).toBe(true);
    expect(Number.isFinite(processors[0]._maxExportBatchSize) && processors[0]._maxExportBatchSize > 0).toBe(true);
    await provider.shutdown();
  }
});
