import { createServer, Server } from "http";
import { AddressInfo } from "net";
import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { ConverseStreamCommand, InvokeModelWithResponseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import { TraceConfigOptions } from "@traceai/fi-core";
import { BedrockInstrumentation } from "../instrumentation";

const SECRET_INPUT = "synthetic private input";
const SECRET_OUTPUT = "synthetic private output";
const SECRET_TOOL = "synthetic private argument";
const converseEvents = [
  { messageStart: { role: "assistant" } },
  { contentBlockDelta: { contentBlockIndex: 0, delta: { text: SECRET_OUTPUT } } },
  { contentBlockStart: { contentBlockIndex: 1, start: { toolUse: { toolUseId: "tool-1", name: "lookup" } } } },
  { contentBlockDelta: { contentBlockIndex: 1, delta: { toolUse: { input: '{"query":' } } } },
  { contentBlockDelta: { contentBlockIndex: 1, delta: { toolUse: { input: JSON.stringify(SECRET_TOOL) + "}" } } } },
  { contentBlockStop: { contentBlockIndex: 1 } },
  { messageStop: { stopReason: "tool_use" } },
  { metadata: { usage: { inputTokens: 7, outputTokens: 5, totalTokens: 12 }, metrics: { latencyMs: 10 } } },
];
const invokeEvents = [
  { chunk: { bytes: new TextEncoder().encode(JSON.stringify({ outputText: SECRET_OUTPUT })) } },
  { chunk: { bytes: new TextEncoder().encode(JSON.stringify({ "amazon-bedrock-invocationMetrics": { inputTokenCount: 7, outputTokenCount: 5 } })) } },
];

const cases = [
  { kind: "converse", key: "stream", events: converseEvents, command: () => new ConverseStreamCommand({ modelId: "amazon.nova-lite-v1:0", messages: [{ role: "user", content: [{ text: SECRET_INPUT }] }] }) },
  { kind: "invoke", key: "body", events: invokeEvents, command: () => new InvokeModelWithResponseStreamCommand({ modelId: "amazon.titan-text-express-v1", body: JSON.stringify({ inputText: SECRET_INPUT }) }) },
];

describe.each(cases)("$kind streaming lifecycle", ({ key, events, command }) => {
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: BedrockInstrumentation;
  let server: Server | undefined;
  let original: jest.Mock;
  let client: { send: (...args: any[]) => any };

  function setup(traceConfig: TraceConfigOptions = {}, wireExporter?: OTLPTraceExporter) {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), ...(wireExporter ? [new SimpleSpanProcessor(wireExporter)] : [])] });
    original = jest.fn();
    class BedrockRuntimeClient { send(...args: unknown[]) { return original.apply(this, args); } }
    instrumentation = new BedrockInstrumentation({ instrumentationConfig: { enabled: false }, tracerProvider: provider, traceConfig });
    instrumentation.manuallyInstrument({ BedrockRuntimeClient } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    client = new BedrockRuntimeClient();
  }
  const stream = () => (async function* () { yield* events; })();
  const consume = async (iterable: AsyncIterable<unknown>) => {
    const result = [];
    for await (const event of iterable) result.push(event);
    return result;
  };

  afterEach(async () => {
    instrumentation?.disable();
    await provider?.shutdown();
    if (server) await new Promise<void>((resolve, reject) => server!.close(error => error ? reject(error) : resolve()));
    server = undefined;
    context.disable();
  });

  it("finishes only after consumption and captures output and final usage", async () => {
    setup();
    const metadata = { requestId: "test" };
    original.mockResolvedValue({ [key]: stream(), $metadata: metadata });
    const response = await client.send(command());
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(response.$metadata).toBe(metadata);
    const observed = await consume(response[key]);
    expect(observed).toEqual(events);
    observed.forEach((event, index) => expect(event).toBe(events[index]));
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes[SC.OUTPUT_VALUE]).toContain(SECRET_OUTPUT);
    expect(spans[0].attributes[SC.LLM_TOKEN_COUNT_PROMPT]).toBe(7);
    expect(spans[0].attributes[SC.LLM_TOKEN_COUNT_COMPLETION]).toBe(5);
    expect(spans[0].attributes[SC.LLM_TOKEN_COUNT_TOTAL]).toBe(12);
    if (key === "stream") {
      const messages = JSON.parse(spans[0].attributes[SC.LLM_OUTPUT_MESSAGES] as string);
      expect(messages[0].tool_calls).toEqual([{ id: "tool-1", function: { name: "lookup", arguments: JSON.stringify({ query: SECRET_TOOL }) } }]);
    }
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("closes before first next without pulling and ends exactly once", async () => {
    setup();
    const iterator = { next: jest.fn(), return: jest.fn(async () => ({ done: true, value: "closed" })) };
    original.mockResolvedValue({ [key]: { [Symbol.asyncIterator]: () => iterator } });
    const response = await client.send(command());
    expect(await response[key].return()).toEqual({ done: true, value: "closed" });
    await response[key].return();
    expect(iterator.next).not.toHaveBeenCalled();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("forwards a mid-stream failure and retains partial output", async () => {
    setup();
    const error = new Error("stream disconnected");
    original.mockResolvedValue({ [key]: (async function* () { yield* events; throw error; })() });
    const response = await client.send(command());
    await expect(consume(response[key])).rejects.toBe(error);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(spans[0].attributes[SC.OUTPUT_VALUE]).toContain(SECRET_OUTPUT);
  });

  it("forwards iterator throw and close failures unchanged", async () => {
    setup();
    for (const method of ["throw", "return"] as const) {
      exporter.reset();
      const error = new Error(method);
      const iterator = {
        next: jest.fn(async () => ({ done: false, value: events[0] })),
        [method]: jest.fn(async () => { throw error; }),
      };
      original.mockResolvedValue({ [key]: { [Symbol.asyncIterator]: () => iterator } });
      const response = await client.send(command());
      await expect(response[key][method](error)).rejects.toBe(error);
      expect(iterator[method]).toHaveBeenCalledWith(error);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    }
  });

  it("preserves cleanup yields from iterator return until the source is done", async () => {
    setup();
    const source = (async function* () {
      try { yield events[0]; }
      finally { yield events[1]; }
    })();
    original.mockResolvedValue({ [key]: source });
    const response = await client.send(command());
    await response[key].next();
    expect(await response[key].return()).toEqual({ done: false, value: events[1] });
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await response[key].next();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("records even an undefined rejection as an error", async () => {
    setup();
    original.mockResolvedValue({ [key]: { [Symbol.asyncIterator]: () => ({ next: () => Promise.reject(undefined) }) } });
    const response = await client.send(command());
    await expect(response[key].next()).rejects.toBeUndefined();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("marks exception events as failures without replacing user events", async () => {
    setup();
    const event = { modelStreamErrorException: { message: "failed upstream" } };
    original.mockResolvedValue({ [key]: (async function* () { yield event; })() });
    const response = await client.send(command());
    expect(await consume(response[key])).toEqual([event]);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(spans[0].status.message).toBe("failed upstream");
  });

  it("does not make malformed capture data fail an otherwise successful stream", async () => {
    setup();
    const event = key === "stream" ? { contentBlockDelta: {} } : { chunk: { bytes: new TextEncoder().encode("not JSON") } };
    original.mockResolvedValue({ [key]: (async function* () { yield event; yield* events; })() });
    const response = await client.send(command());
    expect(await consume(response[key])).toEqual([event, ...events]);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("keeps the request parent and executes transport and iterator operations in the span context", async () => {
    setup();
    context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
    const parent = provider.getTracer("test").startSpan("parent");
    const activeIds: (string | undefined)[] = [];
    original.mockImplementation(async () => {
      activeIds.push(trace.getSpan(context.active())?.spanContext().spanId);
      return { [key]: (async function* () {
        activeIds.push(trace.getSpan(context.active())?.spanContext().spanId);
        yield* events;
      })() };
    });
    const response = await context.with(trace.setSpan(context.active(), parent), () => client.send(command()));
    await consume(response[key]);
    const span = exporter.getFinishedSpans()[0];
    expect(span.parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
    expect(activeIds).toEqual([span.spanContext().spanId, span.spanContext().spanId]);
    expect(trace.getSpan(context.active())).toBeUndefined();
    parent.end();
  });

  it.each([false, true])("exports correct OTLP shape and privacy (hidden=%s)", async (hidden) => {
    const payloads: any[] = [];
    const paths: string[] = [];
    server = createServer((request, response) => {
      paths.push(request.url!);
      const chunks: Buffer[] = [];
      request.on("data", chunk => chunks.push(chunk));
      request.on("end", () => {
        payloads.push(JSON.parse(Buffer.concat(chunks).toString()));
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end("{}");
      });
    });
    await new Promise<void>(resolve => server!.listen(0, "127.0.0.1", resolve));
    setup({ hideInputs: hidden, hideOutputs: hidden }, new OTLPTraceExporter({ url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/traces` }));
    original.mockResolvedValue({ [key]: stream() });
    const response = await client.send(command());
    await consume(response[key]);
    await provider.forceFlush();
    expect(paths).toEqual(["/v1/traces"]);
    const spans = payloads.flatMap(payload => payload.resourceSpans.flatMap((resource: any) => resource.scopeSpans.flatMap((scope: any) => scope.spans)));
    expect(spans).toHaveLength(1);
    expect(spans[0].name).toBe(key === "stream" ? "bedrock.converse" : "bedrock.invoke_model");
    expect(BigInt(spans[0].endTimeUnixNano)).toBeGreaterThan(BigInt(spans[0].startTimeUnixNano));
    const attrs = Object.fromEntries(spans[0].attributes.map((attribute: any) => [attribute.key, attribute.value]));
    expect(Number(attrs[SC.LLM_TOKEN_COUNT_TOTAL].intValue)).toBe(12);
    const wire = JSON.stringify(payloads);
    if (hidden) {
      expect(wire).not.toContain(SECRET_INPUT);
      expect(wire).not.toContain(SECRET_OUTPUT);
      expect(wire).not.toContain(SECRET_TOOL);
    } else {
      expect(wire).toContain(SECRET_INPUT);
      expect(wire).toContain(SECRET_OUTPUT);
      if (key === "stream") expect(wire).toContain(SECRET_TOOL);
    }
  });
});
