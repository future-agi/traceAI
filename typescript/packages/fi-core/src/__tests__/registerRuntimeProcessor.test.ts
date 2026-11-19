import { createServer, Server } from "node:http";
import type { AddressInfo } from "node:net";
import { ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { InMemorySpanExporter, SimpleSpanProcessor, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { register } from "../otel";
import { FutureAGIRuntimeAttributesSpanProcessor } from "../trace/runtimeAttributesProcessor";
import { hasAutoSpanSink } from "../auto-span-delivery";

const carrier = ROOT_CONTEXT.setValue(Symbol.for("futureagi.runtime.span_attributes.v1"), {
  v: 1, attrs: { "fi.context.name": "support", "fi.skills.count": 1, "gen_ai.agent.name": "refund-agent" }, reset_skills: false,
});
const env = { ...process.env };
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
});
afterEach(() => { jest.restoreAllMocks(); process.env = { ...env }; });

/** Cloud-only suite: the receiver is real loopback HTTP, not a mocked exporter. */
test.each([false, true])("register preserves default OTLP delivery and stamps first (batch=%s)", async batch => {
  const received: any[] = [];
  const server: Server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", chunk => chunks.push(Buffer.from(chunk)));
    request.on("end", () => {
      try { received.push(JSON.parse(Buffer.concat(chunks).toString("utf8"))); response.writeHead(200, { "Content-Type": "application/json" }); response.end("{}"); }
      catch { response.writeHead(400); response.end(); }
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const runtimeStart = jest.spyOn(FutureAGIRuntimeAttributesSpanProcessor.prototype, "onStart");
  const provider = register({ projectName: "runtime-test", endpoint: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, setGlobalTracerProvider: false, batch });
  try {
    provider.getTracer("runtime-test").startSpan("request", {}, carrier).end();
    await provider.forceFlush();
    expect(runtimeStart).toHaveBeenCalledTimes(1);
    const spans = received.flatMap(body => body.resourceSpans.flatMap((resource: any) => resource.scopeSpans.flatMap((scope: any) => scope.spans)));
    expect(spans).toHaveLength(1);
    const attrs = Object.fromEntries(spans[0].attributes.map((item: any) => [item.key, item.value]));
    expect(attrs["fi.context.name"]).toEqual({ stringValue: "support" });
    expect(attrs["gen_ai.agent.name"]).toEqual({ stringValue: "refund-agent" });
    expect(Number((attrs["fi.skills.count"] as any).intValue)).toBe(1);
  } finally {
    await provider.shutdown();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});

test.each([true, false])("constructor processors see stamped attributes before their onStart (enabled=%s)", async enabled => {
  const exporter = new InMemorySpanExporter();
  const onStart = jest.fn(span => ({ ...span.attributes }));
  const observer: SpanProcessor = { onStart, onEnd: () => {}, forceFlush: async () => {}, shutdown: async () => {} };
  const processors = [observer, new SimpleSpanProcessor(exporter)];
  const provider = register({ projectName: "runtime-test", setGlobalTracerProvider: false, runtimeAttributes: enabled, spanProcessors: processors });
  try {
    provider.getTracer("runtime-test").startSpan("request", {}, carrier).end();
    await provider.forceFlush();
    expect(onStart).toHaveBeenCalledTimes(1);
    expect(onStart.mock.results[0].value["fi.context.name"]).toBe(enabled ? "support" : undefined);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(processors).toHaveLength(2);
  } finally { await provider.shutdown(); }
});

test("an explicitly supplied runtime processor is moved first, not installed twice", async () => {
  const runtime = new FutureAGIRuntimeAttributesSpanProcessor();
  const onStart = jest.spyOn(runtime, "onStart");
  const observed: unknown[] = [];
  const observer: SpanProcessor = {
    onStart: span => { observed.push(span.attributes["fi.skills.count"]); },
    onEnd: () => {}, forceFlush: async () => {}, shutdown: async () => {},
  };
  const provider = register({ projectName: "runtime-test", setGlobalTracerProvider: false, spanProcessors: [observer, runtime] });
  try {
    provider.getTracer("runtime-test").startSpan("request", {}, carrier).end();
    expect(onStart).toHaveBeenCalledTimes(1);
    expect(observed).toEqual([1]);
  } finally { await provider.shutdown(); }
});

test("explicit empty processors do not construct a default exporter or replace global provider", async () => {
  const existing = trace.getTracerProvider();
  const provider = register({ projectName: "runtime-test", setGlobalTracerProvider: false, spanProcessors: [], endpoint: "http://127.0.0.1:1" });
  try {
    const span = provider.getTracer("runtime-test").startSpan("request", {}, carrier);
    expect((span as any).attributes["fi.skills.count"]).toBe(1);
    span.end();
    await provider.forceFlush();
    expect(trace.getTracerProvider()).toBe(existing);
    expect(hasAutoSpanSink(new FutureAGIRuntimeAttributesSpanProcessor())).toBe(false);
    expect(hasAutoSpanSink((provider as any)._activeSpanProcessor)).toBe(false);
  } finally { await provider.shutdown(); }
});
