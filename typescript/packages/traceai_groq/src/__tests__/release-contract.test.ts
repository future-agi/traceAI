import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import { createReadStream, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SpanKind, SpanStatusCode, context } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { GroqInstrumentation } from "../instrumentation";

// Real SDK and HTTP multipart/JSON requests; synthetic credentials and loopback only.
const sdk = require(process.env.GROQ_TEST_SDK || "groq-sdk");
const proto = require(require.resolve("@opentelemetry/otlp-transformer/build/src/generated/root.js", {
  paths: [require.resolve("@opentelemetry/exporter-trace-otlp-proto")],
})).opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const params = { model: "requested-model", messages: [{ role: "user", content: "private-prompt" }], max_tokens: 20, temperature: 0.5 };
const completion = {
  id: "response-test", object: "chat.completion", created: 1, model: "actual-model",
  choices: [{ index: 0, message: { role: "assistant", content: "answer" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
};
let provider: NodeTracerProvider;
let exporter: InMemorySpanExporter;
let instrumentation: GroqInstrumentation;
let server: Server;
let baseURL: string;
let wire: Buffer[];
let requests: Array<{ path: string; body: string; header?: string | string[] }>;
let audioMode: "json" | "text" | "error";
const client = () => new sdk.Groq({ apiKey: "synthetic-not-a-key", maxRetries: 0, baseURL });
function instrument(traceConfig?: TraceConfigOptions) {
  instrumentation = new GroqInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
}
function cleanup() {
  instrumentation.disable();
  // Also clean up old implementations in the pre-fix regression run.
  (instrumentation as unknown as { unpatch(module: object): void }).unpatch(sdk);
}
async function exportedWire() {
  await provider.forceFlush();
  return JSON.stringify(wire.map(bytes => proto.toObject(proto.decode(bytes))));
}
beforeAll(async () => {
  server = createServer((req, res) => {
    const data: Buffer[] = [];
    req.on("data", bytes => data.push(bytes));
    req.on("end", () => {
      if (req.url === "/v1/traces") { wire.push(Buffer.concat(data)); res.end(); return; }
      requests.push({ path: req.url!, body: Buffer.concat(data).toString(), header: req.headers["x-test"] });
      res.setHeader("x-request-id", "request-audio");
      if (req.url?.includes("/audio/") && audioMode === "error") {
        res.writeHead(400, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message: "private-audio-error", type: "invalid_request_error" } }));
      } else if (req.url?.endsWith("/audio/speech")) {
        res.writeHead(200, { "content-type": "audio/wav" }); res.end("private-speech-bytes");
      } else if (req.url?.includes("/audio/") && audioMode === "text") {
        res.writeHead(200, { "content-type": "text/plain" }); res.end("private-transcript");
      } else {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify(req.url?.includes("/audio/") ? { text: "private-transcript", language: "english", duration: 2 } : completion));
      }
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  baseURL = origin;
  exporter = new InMemorySpanExporter();
  provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${origin}/v1/traces` })),
  ] });
  provider.register();
});
beforeEach(() => { wire = []; requests = []; audioMode = "json"; exporter.reset(); instrument(); });
afterEach(async () => { await provider.forceFlush(); cleanup(); });
afterAll(async () => { await provider.shutdown(); await new Promise<void>(resolve => server.close(() => resolve())); });

const inputPolicies: Array<[string, TraceConfigOptions | undefined, string | undefined]> = [
  ["hideInputs", { hideInputs: true }, undefined],
  ["hideInputText", { hideInputText: true }, undefined],
  ["FI_HIDE_INPUTS", undefined, "FI_HIDE_INPUTS"],
  ["FI_HIDE_INPUT_TEXT", undefined, "FI_HIDE_INPUT_TEXT"],
];
describe.each(inputPolicies)("document privacy with %s", (_name, config, env) => {
  it.each(["text", "json"])("hides %s document sources on the OTLP wire, not in the provider request", async type => {
    cleanup();
    if (env) process.env[env] = "true";
    try {
      instrument(config);
      const documents = [{ id: "document-1", source: type === "text"
        ? { type, text: "secret-document-content" } : { type, data: { nested: "secret-document-content" } } }];
      await client().chat.completions.create({ ...params, documents });
      expect(JSON.parse(requests[0].body)).toEqual({ ...params, documents });
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(JSON.stringify(spans[0].attributes)).not.toContain("secret-document-content");
      expect(await exportedWire()).not.toContain("secret-document-content");
      const a = spans[0].attributes;
      expect(a[S.LLM_MODEL_NAME]).toBe(params.model);
      expect(a[S.LLM_TOKEN_COUNT_TOTAL]).toBe(13);
      expect(JSON.parse(a[S.LLM_INVOCATION_PARAMETERS] as string)).toMatchObject({ model: params.model, max_tokens: 20, temperature: 0.5 });
    } finally { if (env) delete process.env[env]; }
  });
});
it("keeps document content in privacy-controlled input attributes only", async () => {
  await client().chat.completions.create({ ...params, documents: [{ source: { type: "text", text: "secret-document-content" } }] });
  const a = exporter.getFinishedSpans()[0].attributes;
  expect(a[S.INPUT_VALUE]).toContain("secret-document-content");
  expect(a[S.RAW_INPUT]).toContain("secret-document-content");
  expect(a[S.LLM_INVOCATION_PARAMETERS]).not.toContain("secret-document-content");
});

it("stops manual instrumentation on disable and restores exactly one span on re-enable", async () => {
  instrumentation.manuallyInstrument({ Groq: sdk.Groq });
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  instrumentation.disable(); instrumentation.disable();
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  instrumentation.enable(); instrumentation.enable();
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});
it("does not patch a manually registered module while disabled", async () => {
  cleanup();
  instrumentation = new GroqInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  instrumentation.enable();
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});
it("disabling a second instrumentor does not remove the first instrumentor's patch", async () => {
  const second = new GroqInstrumentation({ instrumentationConfig: { enabled: false } });
  second.setTracerProvider(provider); second.enable(); second.manuallyInstrument({ Groq: sdk.Groq });
  second.disable();
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  instrumentation.disable();
  await client().chat.completions.create(params);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});
it("leaves another library's outer wrapper intact across disable and re-enable", async () => {
  const prototype = sdk.Groq.Chat.Completions.prototype;
  const inner = prototype.create;
  const outer = jest.fn(function (this: unknown, ...args: unknown[]) { return inner.apply(this, args); });
  prototype.create = outer;
  try {
    instrumentation.disable();
    expect(prototype.create).toBe(outer);
    await client().chat.completions.create(params);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    expect(prototype.create).toBe(outer);
    await client().chat.completions.create(params);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(outer).toHaveBeenCalledTimes(2);
  } finally { prototype.create = inner; }
});

// The declared SDK floor has no audio APIs. Do not make floor support depend on them.
const audioDescribe = sdk.Groq.Audio ? describe : describe.skip;
audioDescribe.each(["transcriptions", "translations"])("native audio %s", endpoint => {
  const audioParams = async () => ({
    model: "whisper-large-v3", file: await sdk.toFile(Buffer.from("private-audio-bytes"), "private-filename.wav", { type: "audio/wav" }),
    prompt: "private-audio-prompt", temperature: 0, response_format: "verbose_json",
  });
  it("records parent, operation, model and result without serializing or consuming the file", async () => {
    const body = await audioParams();
    const fileJSON = jest.fn(() => { throw new Error("instrumentation must not serialize the file"); });
    Object.defineProperty(body.file, "toJSON", { value: fileJSON });
    await provider.getTracer("audio-test").startActiveSpan("parent", async parent => {
      const promise = client().audio[endpoint].create(body, { headers: { "x-test": "forwarded" } });
      expect(promise).toBeInstanceOf(Promise);
      expect(typeof promise.asResponse).toBe("function");
      const { data, response } = await promise.withResponse();
      expect(data.text).toBe("private-transcript");
      expect(await promise).toBe(data);
      expect(await promise.asResponse()).toBe(response);
      parent.end();
    });
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(2);
    const span = spans.find(s => s.name !== "parent")!;
    expect(span.parentSpanContext?.spanId).toBe(spans.find(s => s.name === "parent")!.spanContext().spanId);
    expect(span.kind).toBe(SpanKind.INTERNAL);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes[S.LLM_PROVIDER]).toBe("groq");
    expect(span.attributes[S.LLM_MODEL_NAME]).toBe(body.model);
    expect(span.attributes[S.GEN_AI_OPERATION_NAME]).toBe(endpoint === "transcriptions" ? "transcription" : "translation");
    expect(span.attributes[S.OUTPUT_VALUE]).toContain("private-transcript");
    expect(span.attributes[S.INPUT_VALUE]).toContain("private-audio-prompt");
    expect(span.attributes[S.LLM_INVOCATION_PARAMETERS]).not.toContain("private-audio-prompt");
    expect(fileJSON).not.toHaveBeenCalled();
    expect(await body.file.text()).toBe("private-audio-bytes");
    expect(requests).toHaveLength(1);
    expect(requests[0].path).toBe(`/openai/v1/audio/${endpoint}`);
    expect(requests[0].body).toContain("private-audio-bytes");
    expect(requests[0].header).toBe("forwarded");
    const exported = await exportedWire();
    for (const secret of ["private-audio-bytes", "private-filename.wav", "synthetic-not-a-key"]) expect(exported).not.toContain(secret);
  });
  it("leaves a Node file stream solely to the SDK's multipart uploader", async () => {
    const directory = mkdtempSync(join(tmpdir(), "groq-audio-"));
    const path = join(directory, "private-file.wav");
    writeFileSync(path, "private-streamed-audio");
    const file = createReadStream(path);
    try {
      const result = await client().audio[endpoint].create({ model: "whisper-large-v3", file });
      expect(result.text).toBe("private-transcript");
      expect(requests).toHaveLength(1);
      expect(requests[0].body.split("private-streamed-audio")).toHaveLength(2);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      const exported = await exportedWire();
      expect(exported).not.toContain("private-streamed-audio");
      expect(exported).not.toContain(path);
    } finally { file.destroy(); rmSync(directory, { recursive: true, force: true }); }
  });
  it("preserves an already-aborted upload request and ends its error span", async () => {
    const controller = new AbortController(); controller.abort();
    await expect(client().audio[endpoint].create(await audioParams(), { signal: controller.signal })).rejects.toBeInstanceOf(sdk.APIUserAbortError);
    expect(requests).toHaveLength(0);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });
  it("preserves raw response ownership without parsing the body", async () => {
    const raw = await client().audio[endpoint].create(await audioParams()).asResponse();
    expect(raw.bodyUsed).toBe(false);
    expect((await raw.json()).text).toBe("private-transcript");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE]).toBeUndefined();
  });
  it("handles text response formats without changing the returned value", async () => {
    audioMode = "text";
    expect(await client().audio[endpoint].create({ ...await audioParams(), response_format: "text" })).toBe("private-transcript");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE]).toBe("private-transcript");
  });
  it.each([false, true])("keeps provider errors intact and applies error privacy=%s", async hideErrorDetails => {
    cleanup(); instrument({ hideErrorDetails }); audioMode = "error";
    const promise = client().audio[endpoint].create(await audioParams());
    let caught: unknown;
    try { await promise; } catch (error) { caught = error; }
    expect(caught).toBeInstanceOf(sdk.BadRequestError);
    expect((caught as Error).message).toContain("private-audio-error");
    await expect(promise).rejects.toBe(caught);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    const exported = await exportedWire();
    if (hideErrorDetails) expect(exported).not.toContain("private-audio-error");
    else expect(exported).toContain("private-audio-error");
  });
  it.each(["options-text", "options-all", "environment-text", "environment-all"])("applies input/output hiding via %s on the wire", async source => {
    cleanup();
    if (source === "environment-text") { process.env.FI_HIDE_INPUT_TEXT = "true"; process.env.FI_HIDE_OUTPUT_TEXT = "true"; }
    if (source === "environment-all") { process.env.FI_HIDE_INPUTS = "true"; process.env.FI_HIDE_OUTPUTS = "true"; }
    try {
      instrument(source === "options-text" ? { hideInputText: true, hideOutputText: true } : source === "options-all" ? { hideInputs: true, hideOutputs: true } : undefined);
      await client().audio[endpoint].create(await audioParams());
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      const exported = await exportedWire();
      for (const secret of ["private-audio-prompt", "private-transcript", "private-audio-bytes"]) expect(exported).not.toContain(secret);
      expect(exporter.getFinishedSpans()[0].attributes[S.LLM_MODEL_NAME]).toBe("whisper-large-v3");
    } finally {
      delete process.env.FI_HIDE_INPUT_TEXT; delete process.env.FI_HIDE_OUTPUT_TEXT;
      delete process.env.FI_HIDE_INPUTS; delete process.env.FI_HIDE_OUTPUTS;
    }
  });
  it("honors suppression and disable/re-enable", async () => {
    const body = await audioParams();
    await context.with(suppressTracing(context.active()), () => client().audio[endpoint].create(body));
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.disable();
    await client().audio[endpoint].create(body);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    await client().audio[endpoint].create(body);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
});

const speechDescribe = sdk.Groq.Audio?.Speech ? describe : describe.skip;
speechDescribe("native audio speech", () => {
  const body = { model: "playai-tts", voice: "Fritz-PlayAI", input: "private-speech-input", response_format: "wav" };
  it.each(["await", "withResponse", "asResponse"])("traces %s without reading generated audio", async helper => {
    const promise = client().audio.speech.create(body);
    const response = helper === "withResponse" ? (await promise.withResponse()).data
      : helper === "asResponse" ? await promise.asResponse() : await promise;
    expect(response.bodyUsed).toBe(false);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const span = exporter.getFinishedSpans()[0];
    expect(span.attributes[S.LLM_MODEL_NAME]).toBe("playai-tts");
    expect(span.attributes[S.GEN_AI_OPERATION_NAME]).toBe("speech");
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes[S.INPUT_VALUE]).toContain("private-speech-input");
    expect(span.attributes[S.LLM_INVOCATION_PARAMETERS]).not.toContain("private-speech-input");
    expect(span.attributes[S.OUTPUT_VALUE]).toBeUndefined();
    expect(await exportedWire()).not.toContain("private-speech-bytes");
    expect(await response.text()).toBe("private-speech-bytes");
    expect(JSON.parse(requests[0].body)).toEqual(body);
  });
  it("hides speech input with the same input policy as other endpoints", async () => {
    cleanup(); instrument({ hideInputs: true });
    const response = await client().audio.speech.create(body);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(await exportedWire()).not.toContain("private-speech-input");
    expect(await response.text()).toBe("private-speech-bytes");
  });
  it("preserves speech errors and redacts their details", async () => {
    cleanup(); instrument({ hideErrorDetails: true }); audioMode = "error";
    await expect(client().audio.speech.create(body)).rejects.toBeInstanceOf(sdk.BadRequestError);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(await exportedWire()).not.toContain("private-audio-error");
  });
});
