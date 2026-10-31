import { SpanStatusCode } from "@opentelemetry/api";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { createSpanExporter, FITracerProvider, GRPCSpanExporter, HTTPSpanExporter, register, Transport } from "../otel";
import { registerSecret, getRegisteredSecrets } from "../secrets";
import { privacyFilter } from "../export-privacy";

const delivered: any[][] = [];
jest.mock("@opentelemetry/exporter-trace-otlp-http", () => ({ OTLPTraceExporter: class {
  export(spans: any[], callback: Function) { delivered.push(spans); callback({ code: 0 }); }
  async shutdown() {} async forceFlush() {}
} }));
jest.mock("@opentelemetry/exporter-trace-otlp-grpc", () => ({ OTLPTraceExporter: class {
  export(spans: any[], callback: Function) { delivered.push(spans); callback({ code: 0 }); }
  async shutdown() {} async forceFlush() {}
} }));
const saved = { ...process.env };
beforeEach(() => { delivered.length = 0; process.env.FI_HIDE_INPUTS = "true"; });
afterEach(() => { process.env = { ...saved }; jest.restoreAllMocks(); });
const options = { endpoint: "http://127.0.0.1:1", headers: {}, projectName: "routing-project", setGlobalTracerProvider: false };
function fixture(): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "manual", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], droppedAttributesCount: 19,
    attributes: { database_password: "database-private-value", echoed: "registered-manual-private", "input.value": "private-input" },
    status: { code: 2, message: "registered-manual-private" }, events: [{ name: "registered-manual-private", time: [1, 2], attributes: { echoed: "registered-manual-private" } }], links: [],
    resource: resourceFromAttributes({ safe: "application" }), instrumentationScope: { name: "application" } };
}
test.each(["factory-http", "factory-grpc", "legacy-http", "legacy-grpc", "register-default", "register-batch", "direct-provider"])("A2 every owned %s route applies S1 on a detached span; A3 defaults batch", async route => {
  registerSecret("registered-manual-private");
  const fetch = jest.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
  const original = fixture(), before = JSON.stringify(original);
  let provider: FITracerProvider | undefined, exporter: any;
  if (route.startsWith("register")) provider = register({ ...options, batch: route === "register-batch" });
  else if (route === "direct-provider") provider = new FITracerProvider(options);
  else if (route === "factory-http") exporter = createSpanExporter(options);
  else if (route === "factory-grpc") exporter = createSpanExporter({ ...options, transport: Transport.GRPC });
  else if (route === "legacy-http") exporter = new HTTPSpanExporter(options);
  else exporter = new GRPCSpanExporter(options);
  if (provider) {
    expect((provider as any)._activeSpanProcessor._spanProcessors.map((p: any) => p.constructor.name)).toEqual(["BatchSpanProcessor"]);
    const span = provider.getTracer("manual").startSpan("manual", { attributes: original.attributes });
    span.setStatus({ code: SpanStatusCode.ERROR, message: "registered-manual-private" }); span.addEvent("registered-manual-private"); span.end();
    expect(delivered).toHaveLength(0); await provider.forceFlush();
  } else await new Promise<void>(done => exporter.export([original], () => done()));
  const spans = route === "legacy-http" ? JSON.parse(String(fetch.mock.calls[0][1]!.body)) : delivered[0];
  expect(spans).toHaveLength(1);
  expect(spans[0].attributes).toMatchObject({ database_password: "<redacted>", echoed: "<redacted>", "input.value": "__REDACTED__" });
  expect(JSON.stringify(spans)).not.toMatch(/registered-manual-private|private-input|database-private-value/);
  expect(JSON.stringify(original)).toBe(before);
  if (!provider && route !== "legacy-http") expect(spans[0].droppedAttributesCount).toBe(19);
  await (provider ?? exporter).shutdown();
});
test("A3 addSpanProcessor changes the live SDK processor list even for a previously acquired tracer", async () => {
  const provider = new FITracerProvider(options), tracer = provider.getTracer("before-replacement"), native = new InMemorySpanExporter();
  provider.addSpanProcessor(new SimpleSpanProcessor(native));
  tracer.startSpan("live").end(); await provider.forceFlush();
  expect(native.getFinishedSpans()).toHaveLength(1); expect(delivered).toHaveLength(0); await provider.shutdown();
});
test("A6 routing does not replace default/environment/application resource identity", async () => {
  process.env.OTEL_SERVICE_NAME = "application-service"; process.env.OTEL_RESOURCE_ATTRIBUTES = "custom.identity=environment-resource,service.version=1.2";
  for (const direct of [false, true]) {
    const native = new InMemorySpanExporter();
    const config = { ...options, resource: resourceFromAttributes({ "app.identity": "supplied" }), spanProcessors: [new SimpleSpanProcessor(native)] };
    const provider = direct ? new FITracerProvider(config) : register(config);
    provider.getTracer("identity").startSpan("identity").end(); await provider.forceFlush();
    expect(native.getFinishedSpans()[0].resource.attributes).toMatchObject({ "service.name": "application-service", "custom.identity": "environment-resource", "service.version": "1.2", "app.identity": "supplied", ...(direct ? {} : { project_name: "routing-project" }) });
    expect(privacyFilter().span(native.getFinishedSpans()[0]).resource.attributes).toMatchObject({ "service.name": "application-service", "custom.identity": "environment-resource", "app.identity": "supplied" });
    await provider.shutdown();
  }
});
test("S7 ignored explicit FI halves seed the shared registry independently of selected headers", async () => {
  process.env.FI_API_KEY = "environment-complete-fi-key"; process.env.FI_SECRET_KEY = "environment-complete-fi-secret";
  const exporter = createSpanExporter({ ...options, headers: { "x-api-key": "ignored-explicit-fi-key" } });
  expect(getRegisteredSecrets()).toContain("ignored-explicit-fi-key");
  const clean = privacyFilter().span({ ...fixture(), attributes: { echoed: "ignored-explicit-fi-key" }, status: { code: 2, message: "ignored-explicit-fi-key" }, events: [{ name: "ignored-explicit-fi-key", time: [1, 2] }] });
  expect(JSON.stringify(clean)).not.toContain("ignored-explicit-fi-key"); await exporter.shutdown();
});
