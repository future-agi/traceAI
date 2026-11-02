import { context, propagation, SpanStatusCode, trace } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { wrapLangGraphClient, langGraphTraceRequestHook } from "../remoteSdk";

function setup() {
  context.disable();
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  return { exporter, provider };
}

describe("remote LangGraph SDK client", () => {
  afterEach(() => context.disable());
  it("traces wait once, preserves receiver, metadata and parent context without recording payloads", async () => {
    const { exporter, provider } = setup();
    const runs = { marker: "receiver", async wait(this: { marker: string }, thread: string, assistant: string, payload: unknown) {
      expect(this.marker).toBe("receiver");
      expect(thread).toBe("thread-1");
      expect(assistant).toBe("assistant-1");
      expect(payload).toEqual({ input: "private" });
      return { secret: "private" };
    } };
    const original = { runs };
    const client = wrapLangGraphClient(original, { tracerProvider: provider });
    expect(wrapLangGraphClient(client, { tracerProvider: provider })).toBe(client);
    expect(client.runs).toBe(client.runs);
    const parent = provider.getTracer("test").startSpan("parent");
    const result = await context.with(trace.setSpan(context.active(), parent), () => client.runs.wait("thread-1", "assistant-1", { input: "private" }));
    parent.end();
    expect(result).toEqual({ secret: "private" });
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(2);
    expect(spans[0].parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
    expect(spans[0].attributes).toMatchObject({ "session.id": "thread-1", "langgraph.assistant.id": "assistant-1", "fi.span.kind": "CHAIN" });
    expect(JSON.stringify({ attributes: spans[0].attributes, status: spans[0].status, events: spans[0].events })).not.toContain("private");
    await provider.shutdown();
  });

  it("keeps stream lazy, ends on consumption and closes on early return", async () => {
    const { exporter, provider } = setup();
    let opened = 0, closed = 0;
    const original = { runs: { async *stream(_thread: string, _assistant: string) { opened++; try { yield { event: "values", data: "private" }; yield { event: "values", data: 2 }; } finally { closed++; } } } };
    const client = wrapLangGraphClient(original, { tracerProvider: provider });
    const iterator = client.runs.stream("thread-2", "assistant-2");
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(opened).toBe(0);
    expect((await iterator.next()).value).toEqual({ event: "values", data: "private" });
    expect(opened).toBe(1);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await iterator.return(undefined);
    expect(closed).toBe(1);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["langgraph.stream.cancelled"]).toBe(true);
    expect(JSON.stringify({ attributes: spans[0].attributes, status: spans[0].status, events: spans[0].events })).not.toContain("private");
    await provider.shutdown();
  });

  it("preserves rejection and stream failures with safe status", async () => {
    const { exporter, provider } = setup();
    const failure = new Error("private failure");
    const client = wrapLangGraphClient({ runs: {
      async wait(_thread: string | null, _assistant: string) { throw failure; },
      async *stream(_thread: string | null, _assistant: string) { yield { event: "values", data: 1 }; throw failure; },
    } }, { tracerProvider: provider });
    await expect(client.runs.wait(null, "assistant")).rejects.toBe(failure);
    const stream = client.runs.stream(null, "assistant");
    await stream.next();
    await expect(stream.next()).rejects.toBe(failure);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    for (const span of exporter.getFinishedSpans()) {
      expect(span.status.code).toBe(SpanStatusCode.ERROR);
      expect(JSON.stringify({ attributes: span.attributes, status: span.status, events: span.events })).not.toContain("private failure");
    }
    await provider.shutdown();
  });

  it("propagates stream cleanup failure while ending its span", async () => {
    const { exporter, provider } = setup();
    const cleanupFailure = new Error("private cleanup failure");
    const client = wrapLangGraphClient({ runs: { stream(_thread: null, _assistant: string) {
      return {
        async next() { return { done: false as const, value: { event: "values", data: 1 } }; },
        async return(): Promise<IteratorReturnResult<unknown>> { throw cleanupFailure; },
      };
    } } }, { tracerProvider: provider });
    const stream = client.runs.stream(null, "assistant") as unknown as AsyncGenerator<unknown>;
    await stream.next();
    await expect(stream.return(undefined)).rejects.toBe(cleanupFailure);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(JSON.stringify({ status: spans[0].status, attributes: spans[0].attributes })).not.toContain("private cleanup failure");
    await provider.shutdown();
  });

  it("marks aborted wait and stream as cancelled without exposing an abort reason", async () => {
    const { exporter, provider } = setup();
    const aborted = new Error("private abort reason");
    aborted.name = "AbortError";
    const client = wrapLangGraphClient({ runs: {
      async wait(_thread: null, _assistant: string) { throw aborted; },
      async *stream(_thread: null, _assistant: string): AsyncGenerator<unknown> { throw aborted; },
    } }, { tracerProvider: provider });
    await expect(client.runs.wait(null, "assistant")).rejects.toBe(aborted);
    await expect(client.runs.stream(null, "assistant").next()).rejects.toBe(aborted);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    for (const span of exporter.getFinishedSpans()) {
      expect(span.status.code).not.toBe(SpanStatusCode.ERROR);
      expect(span.attributes["langgraph.cancelled"]).toBe(true);
      expect(JSON.stringify({ attributes: span.attributes, status: span.status, events: span.events })).not.toContain("private abort reason");
    }
    await provider.shutdown();
  });

  it("composes the official request hook without mutating headers", async () => {
    const original = { headers: { "x-existing": "ok" } };
    const hook = langGraphTraceRequestHook((_url, init) => ({ ...init, headers: { ...original.headers, "x-hook": "yes" } }));
    const init = await hook(new URL("https://example.test/runs"), { headers: original.headers });
    const headers = new Headers(init.headers);
    expect(headers.get("x-existing")).toBe("ok");
    expect(headers.get("x-hook")).toBe("yes");
    expect(original.headers).toEqual({ "x-existing": "ok" });
    // A no-op context never invents a trace header.
    expect(headers.get("traceparent")).toBeNull();
    expect(propagation).toBeDefined();
  });
});
