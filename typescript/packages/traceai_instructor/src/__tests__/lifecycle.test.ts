import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { JsonTraceSerializer } from "@opentelemetry/otlp-transformer";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { z } from "zod";
import { InstructorInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { SemanticConventions as S, FISpanKind } from "@traceai/fi-semantic-conventions";

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
provider.register();
const tracer = provider.getTracer("synthetic-provider");
const usage = { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 };
const request = () => ({
  model: "synthetic", messages: [{ role: "user", content: "private input" }],
  response_model: { name: "Person", schema: z.object({ name: z.string() }) },
  max_retries: 0,
});
const completion = (name: unknown = "Ada") => ({
  id: "offline", object: "chat.completion", model: "synthetic", created: 0, usage,
  choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: JSON.stringify({ name }) } }],
});
function chunks() {
  return ['{"name":', '"Ada"}'].map((content, index) => ({
    id: "offline", object: "chat.completion.chunk", model: "synthetic", created: 0,
    choices: [{ index: 0, delta: { content }, finish_reason: index === 1 ? "stop" : null }],
  }));
}
const extractionSpans = () => exporter.getFinishedSpans().filter(s => s.name.startsWith("Instructor Extract:"));
const wire = () => Buffer.from(JsonTraceSerializer.serializeRequest(exporter.getFinishedSpans())!).toString();
let inst: InstructorInstrumentation;
function instrument(module: any, traceConfig = {}) {
  inst = new InstructorInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  inst.setTracerProvider(provider);
  inst.enable();
  inst.manuallyInstrument(module);
  return inst;
}
beforeEach(() => { _resetPatchedStateForTesting(); });
afterEach(() => { inst?.disable(); exporter.reset(); jest.restoreAllMocks(); });
afterAll(async () => { await provider.shutdown(); context.disable(); trace.disable(); });

describe.each([
  ["floor 1.0.0", "instructor-floor"], ["current 1.7.0", "@instructor-ai/instructor"],
])("real Instructor %s", (_version, packageName) => {
  // Real Instructor + Zod + zod-stream; only the provider transport is synthetic.
  function setup(create: (...args: any[]) => any, traceConfig = {}) {
    const module = { ...require(packageName) };
    const factory = module.default;
    instrument(module, traceConfig);
    const client = module.default({ client: { baseURL: "https://api.openai.com", chat: { completions: { create } } }, mode: "JSON" });
    return { module, factory, client };
  }

  it("exports one extraction parent, parsed output and namespaced usage without duplicate LLM accounting", async () => {
    const childIds: string[] = [];
    const { client } = setup(async () => {
      const child = tracer.startSpan("provider request", { attributes: { [S.FI_SPAN_KIND]: FISpanKind.LLM } });
      childIds.push(child.spanContext().spanId); child.end();
      return completion();
    });
    const result = await client.chat.completions.create(request());
    expect(result.name).toBe("Ada");
    const [parent] = extractionSpans();
    expect(parent.attributes[S.FI_SPAN_KIND]).toBe(FISpanKind.CHAIN);
    expect(parent.attributes[S.LLM_PROVIDER]).toBeUndefined();
    expect(parent.attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBeUndefined();
    // 1.0 strips metadata during Zod validation; 1.7 returns _meta.usage.
    expect(parent.attributes["instructor.usage.total_tokens"]).toBe(packageName === "instructor-floor" ? undefined : 6);
    expect(JSON.parse(parent.attributes[S.OUTPUT_VALUE] as string)).toEqual({ name: "Ada" });
    expect(JSON.parse(parent.attributes[S.INPUT_VALUE] as string)).toEqual(request().messages);
    const child = exporter.getFinishedSpans().find(s => s.spanContext().spanId === childIds[0])!;
    expect(child.parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
    const payload = JSON.parse(wire());
    expect(payload.resourceSpans.length).toBeGreaterThan(0);
    expect(wire()).not.toContain('"llm.provider"');
  });

  it("keeps a lazy stream open and runs provider work under its parent", async () => {
    let parentId: string | undefined;
    const create = jest.fn(async () => {
      parentId = trace.getActiveSpan()?.spanContext().spanId;
      return (async function* () { for (const chunk of chunks()) yield chunk; })();
    });
    const { client } = setup(create);
    const stream = await client.chat.completions.create({ ...request(), stream: true });
    // 1.0 opens the provider stream eagerly; 1.7 defers it until next().
    expect(create).toHaveBeenCalledTimes(packageName === "instructor-floor" ? 1 : 0);
    expect(extractionSpans()).toHaveLength(0);
    const values = [];
    for await (const item of stream) values.push(item);
    expect(values.at(-1).name).toBe("Ada");
    const [span] = extractionSpans();
    expect(extractionSpans()).toHaveLength(1);
    expect(parentId).toBe(span.spanContext().spanId);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(JSON.parse(span.attributes[S.OUTPUT_VALUE] as string)).toEqual({ name: "Ada" });
    expect(trace.getActiveSpan()).toBeUndefined();
  });

  it("records lazy provider rejection instead of ending OK before execution", async () => {
    jest.spyOn(console, "error").mockImplementation(() => {});
    const error = new Error("private stream failure");
    const { client } = setup(async () => { throw error; });
    if (packageName === "instructor-floor") {
      await expect(client.chat.completions.create({ ...request(), stream: true })).rejects.toThrow();
    } else {
      const stream = await client.chat.completions.create({ ...request(), stream: true });
      await expect(stream.next()).rejects.toThrow();
    }
    expect(extractionSpans()).toHaveLength(1);
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(extractionSpans()[0].events.some(e => e.name === "exception")).toBe(true);
  });

  it("leaves passthrough provider calls unwrapped", async () => {
    const value = completion();
    const { client } = setup(async () => value);
    const { response_model, ...params } = request();
    expect(await client.chat.completions.create(params)).toBe(value);
    expect(extractionSpans()).toHaveLength(0);
  });

  it("honors suppression without swallowing the result", async () => {
    const { client } = setup(async () => completion());
    const result = await context.with(suppressTracing(context.active()), () => client.chat.completions.create(request()));
    expect(result.name).toBe("Ada"); expect(extractionSpans()).toHaveLength(0);
  });

  it("restores the factory and makes existing clients inert on disable", async () => {
    const { module, factory, client } = setup(async () => completion());
    inst.disable();
    expect(module.default).toBe(factory);
    expect(isPatched()).toBe(false);
    expect((await client.chat.completions.create(request())).name).toBe("Ada");
    expect(extractionSpans()).toHaveLength(0);
  });

  it("keeps validation retry provider attempts under one extraction", async () => {
    jest.spyOn(console, "warn").mockImplementation(() => {});
    let attempts = 0;
    const parents: Array<string | undefined> = [];
    const { client } = setup(async () => {
      parents.push(trace.getActiveSpan()?.spanContext().spanId);
      return completion(attempts++ === 0 ? 123 : "Ada");
    });
    expect((await client.chat.completions.create({ ...request(), max_retries: 1 })).name).toBe("Ada");
    expect(attempts).toBe(2); expect(extractionSpans()).toHaveLength(1);
    expect(parents).toEqual([extractionSpans()[0].spanContext().spanId, extractionSpans()[0].spanContext().spanId]);
  });

  it("redacts input, output and exception details on the OTLP wire", async () => {
    jest.spyOn(console, "error").mockImplementation(() => {});
    const create = jest.fn().mockResolvedValueOnce(completion("private output")).mockRejectedValueOnce(new Error("private error"));
    const { client } = setup(create, { hideInputs: true, hideOutputs: true });
    await client.chat.completions.create(request());
    await expect(client.chat.completions.create(request())).rejects.toThrow("private error");
    const payload = wire();
    for (const secret of ["private input", "private output", "private error"]) expect(payload).not.toContain(secret);
    expect(extractionSpans()[1].status.code).toBe(SpanStatusCode.ERROR);
  });
});

describe("iterator and patch contracts", () => {
  function setup(create: (...args: any[]) => any) {
    const module = { default: () => ({ chat: { completions: { create } } }) };
    instrument(module);
    return { module, client: module.default() };
  }
  it("closes a never-started iterator without executing it", async () => {
    const execute = jest.fn();
    const { client } = setup(async () => (async function* () { execute(); yield { name: "Ada" }; })());
    const stream = await client.chat.completions.create(request());
    expect(await stream.return("cancelled")).toEqual({ done: true, value: "cancelled" });
    expect(execute).not.toHaveBeenCalled();
    expect(extractionSpans()).toHaveLength(1);
    expect(extractionSpans()[0].attributes["instructor.stream.cancelled"]).toBe(true);
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.UNSET);
  });
  it("preserves return values and closes on break under extraction context", async () => {
    let cleanupParent: string | undefined;
    const { client } = setup(async () => (async function* () {
      try { yield { name: "A" }; yield { name: "Ada" }; }
      finally { cleanupParent = trace.getActiveSpan()?.spanContext().spanId; }
    })());
    const stream = await client.chat.completions.create(request());
    for await (const value of stream) { expect(value.name).toBe("A"); break; }
    const [span] = extractionSpans();
    expect(cleanupParent).toBe(span.spanContext().spanId);
    expect(JSON.parse(span.attributes[S.OUTPUT_VALUE] as string)).toEqual({ name: "A" });
    await stream.return(); expect(extractionSpans()).toHaveLength(1);
  });
  it("propagates iterator throw by identity and records the failure", async () => {
    const { client } = setup(async () => (async function* () { yield { name: "A" }; })());
    const stream = await client.chat.completions.create(request());
    await stream.next(); const error = new Error("sentinel");
    await expect(stream.throw(error)).rejects.toBe(error);
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });
  it("retains only the last cumulative output and latest supplied usage", async () => {
    const { client } = setup(async () => (async function* () {
      yield { name: "A", _meta: { usage: { total_tokens: 2 } } };
      yield { name: "Ada", _meta: { usage } };
    })());
    for await (const _ of await client.chat.completions.create(request())) { /* consume */ }
    const [span] = extractionSpans();
    expect(JSON.parse(span.attributes[S.OUTPUT_VALUE] as string)).toEqual({ name: "Ada" });
    expect(span.attributes["instructor.usage.total_tokens"]).toBe(6);
  });
  it("patches multiple module copies without wrapping the same factory twice", async () => {
    const { module, client } = setup(async () => ({ name: "Ada" }));
    const factory = module.default;
    inst.manuallyInstrument(module); expect(module.default).toBe(factory);
    const other = { default: () => ({ chat: { completions: { create: async (_params: unknown) => ({ name: "Grace" }) } } }) };
    inst.manuallyInstrument(other);
    await client.chat.completions.create(request());
    await other.default().chat.completions.create(request());
    expect(extractionSpans()).toHaveLength(2);
  });
  it("records failure after a yielded value without changing the thrown object", async () => {
    const error = new Error("iteration failed");
    const { client } = setup(async () => (async function* () { yield { name: "A" }; throw error; })());
    const stream = await client.chat.completions.create(request());
    await stream.next(); expect(extractionSpans()).toHaveLength(0);
    await expect(stream.next()).rejects.toBe(error);
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    await stream.next(); expect(extractionSpans()).toHaveLength(1);
  });
  it("records cleanup errors rather than reporting cancellation as success", async () => {
    const error = new Error("cleanup failed");
    const { client } = setup(async () => (async function* () {
      try { yield { name: "A" }; } finally { throw error; }
    })());
    const stream = await client.chat.completions.create(request()); await stream.next();
    await expect(stream.return()).rejects.toBe(error);
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });
  it("does not finish if a generator handles throw and yields another value", async () => {
    const { client } = setup(async () => (async function* () {
      try { yield { name: "A" }; } catch { yield { name: "Ada" }; }
    })());
    const stream = await client.chat.completions.create(request()); await stream.next();
    expect((await stream.throw(new Error("recoverable"))).value.name).toBe("Ada");
    expect(extractionSpans()).toHaveLength(0); await stream.next();
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });
  it("finishes an empty stream and ignores malformed or missing usage", async () => {
    const { client } = setup(async () => (async function* () {})());
    const stream = await client.chat.completions.create(request());
    expect((await stream.next()).done).toBe(true);
    expect(extractionSpans()[0].attributes[S.OUTPUT_VALUE]).toBeUndefined();
    expect(extractionSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });
  it("preserves result identity, request options and cyclic output", async () => {
    const result: Record<string, unknown> = { name: "Ada", _meta: { usage: { total_tokens: NaN } } };
    result.self = result;
    const create = jest.fn(async () => result); const { client } = setup(create);
    const params = request(); const options = { headers: { authorization: "synthetic-secret" } };
    expect(await client.chat.completions.create(params, options)).toBe(result);
    expect(create).toHaveBeenCalledWith(params, options);
    expect(wire()).not.toContain("synthetic-secret");
    expect(extractionSpans()[0].attributes["instructor.usage.total_tokens"]).toBeUndefined();
  });
  it("finishes an in-flight stream after disable without tracing new calls", async () => {
    const { client } = setup(async () => (async function* () { yield { name: "Ada" }; })());
    const stream = await client.chat.completions.create(request()); inst.disable();
    for await (const _ of stream) { /* finish the active operation */ }
    for await (const _ of await client.chat.completions.create(request())) { /* untraced */ }
    expect(extractionSpans()).toHaveLength(1);
  });
  it("isolates overlapping asynchronous requests", async () => {
    const parents: string[] = [];
    const { client } = setup(async () => {
      await Promise.resolve(); parents.push(trace.getActiveSpan()!.spanContext().spanId);
      return { name: "Ada" };
    });
    await Promise.all([client.chat.completions.create(request()), client.chat.completions.create(request())]);
    expect(new Set(parents).size).toBe(2);
    expect(parents.sort()).toEqual(extractionSpans().map(s => s.spanContext().spanId).sort());
  });
});
