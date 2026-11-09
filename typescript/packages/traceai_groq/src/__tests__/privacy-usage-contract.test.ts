import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { GroqInstrumentation } from "../instrumentation";

// Exercise real SDK JSON/SSE parsing and decode actual exported OTLP protobuf.
// Both the provider and collector are loopback servers; no provider key is used.
const sdk = require(process.env.GROQ_TEST_SDK || "groq-sdk");
const proto = require(require.resolve("@opentelemetry/otlp-transformer/build/src/generated/root.js", {
  paths: [require.resolve("@opentelemetry/exporter-trace-otlp-proto")],
})).opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const params = { model: "synthetic-model", messages: [{ role: "user", content: "private-input" }] };
const tools = [{ type: "function", function: {
  name: "lookup", description: "private-tool-description",
  parameters: { type: "object", properties: { project: { type: "string", enum: ["private-schema-value"] } } },
} }];
const completion = {
  id: "test-response", object: "chat.completion", created: 1, model: "synthetic-model",
  choices: [{ index: 0, message: { role: "assistant", content: "private-answer" }, finish_reason: "stop" }],
};
const chunk = (extra: Record<string, unknown>) => ({
  id: "test-response", object: "chat.completion.chunk", created: 1, model: "synthetic-model", choices: [], ...extra,
});
let server: Server;
let baseURL: string;
let provider: NodeTracerProvider;
let exporter: InMemorySpanExporter;
let instrumentation: GroqInstrumentation;
let wire: Buffer[];
let requests: unknown[];
let usage: Record<string, unknown>;
let usageChunks: Record<string, unknown>[] | undefined;
let streamChunks: Record<string, unknown>[] | undefined;
function responseChunks() {
  return streamChunks ?? [
    chunk({ choices: [{ index: 0, delta: { role: "assistant", content: "private-answer" }, finish_reason: "stop" }] }),
    ...(usageChunks ?? [usage]).map(chunk),
  ];
}

function instrument(traceConfig?: TraceConfigOptions) {
  instrumentation?.disable();
  instrumentation = new GroqInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
}
async function request(stream: boolean, withTools = false, extra: Record<string, unknown> = {}) {
  const body = { ...params, stream, ...(withTools ? { tools } : {}), ...extra };
  const snapshot = JSON.parse(JSON.stringify(body));
  const result = await new sdk.Groq({ apiKey: "synthetic-not-a-key", maxRetries: 0, baseURL }).chat.completions.create(body);
  const expected = stream ? responseChunks() : { ...completion, ...usage };
  if (stream) {
    const received = [];
    for await (const item of result) received.push(item);
    expect(received).toEqual(expected);
  } else expect(result).toEqual(expected);
  expect(body).toEqual(snapshot);
  expect(requests).toEqual([snapshot]);
  await provider.forceFlush();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(wire).toHaveLength(1);
  const decoded = proto.decode(wire[0]);
  const spans = decoded.resourceSpans[0].scopeSpans[0].spans;
  expect(spans).toHaveLength(1);
  return {
    span: exporter.getFinishedSpans()[0],
    wireSpan: spans[0],
    attributes: exporter.getFinishedSpans()[0].attributes,
    wireAttributes: Object.fromEntries(spans[0].attributes.map((a: { key: string; value: unknown }) => [a.key, a.value])),
    exported: JSON.stringify(proto.toObject(decoded)),
  };
}
beforeAll(async () => {
  server = createServer((req, res) => {
    const data: Buffer[] = [];
    req.on("data", bytes => data.push(bytes));
    req.on("end", () => {
      if (req.url === "/v1/traces") { wire.push(Buffer.concat(data)); res.end(); return; }
      const body = JSON.parse(Buffer.concat(data).toString());
      requests.push(body);
      if (body.stream) {
        res.setHeader("content-type", "text/event-stream");
        const chunks = responseChunks();
        res.end(chunks.map(c => `data: ${JSON.stringify(c)}\n\n`).join("") + "data: [DONE]\n\n");
      } else {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ ...completion, ...usage }));
      }
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  exporter = new InMemorySpanExporter();
  provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${baseURL}/v1/traces` })),
  ] });
  provider.register();
});
beforeEach(() => {
  wire = []; requests = []; usage = {}; usageChunks = undefined; streamChunks = undefined; exporter.reset(); instrument();
});
afterEach(async () => { await provider.forceFlush(); instrumentation.disable(); });
afterAll(async () => { await provider.shutdown(); await new Promise<void>(resolve => server.close(() => resolve())); });

const policies: Array<[string, TraceConfigOptions | undefined, string | undefined]> = [
  ["hideInputs", { hideInputs: true }, undefined],
  ["hideInputText", { hideInputText: true }, undefined],
  ["FI_HIDE_INPUTS", undefined, "FI_HIDE_INPUTS"],
  ["FI_HIDE_INPUT_TEXT", undefined, "FI_HIDE_INPUT_TEXT"],
];
describe.each(policies)("tool schema privacy with %s", (_name, config, env) => {
  it.each([false, true])("protects input schemas on the OTLP wire with stream=%s", async stream => {
    const previous = env ? process.env[env] : undefined;
    if (env) process.env[env] = "true";
    try {
      instrument(config);
      const result = await request(stream, true);
      for (const secret of ["private-input", "private-tool-description", "private-schema-value"]) {
        expect(result.exported).not.toContain(secret);
        expect(JSON.stringify(result.attributes)).not.toContain(secret);
      }
      expect(result.attributes[S.LLM_MODEL_NAME]).toBe(params.model);
    } finally {
      if (env) { if (previous === undefined) delete process.env[env]; else process.env[env] = previous; }
    }
  });
});
it("retains tool schemas by default", async () => {
  const result = await request(false, true);
  expect(JSON.parse(result.attributes[S.LLM_TOOLS] as string)).toEqual(tools);
  expect(JSON.parse(result.wireAttributes[S.LLM_TOOLS].stringValue)).toEqual(tools);
});

describe.each<TraceConfigOptions>([{}, { piiRedaction: true }, { hideInputs: true }])("Compound credentials with %j", config => {
  it.each([false, true])("never exports Wolfram authorization with stream=%s", async stream => {
    instrument(config);
    const credential = "synthetic-wolfram-credential-not-a-real-key";
    // Freeze each nested object: sanitizing telemetry must not mutate provider arguments.
    const wolfram = Object.freeze({ authorization: credential, query: "public-query" });
    const compound = Object.freeze({ tools: Object.freeze({
      enabled_tools: ["wolfram_alpha"], wolfram_settings: wolfram,
    }) });
    const result = await request(stream, false, { compound_custom: compound });
    expect(result.exported).not.toContain(credential);
    expect(JSON.stringify(result.attributes)).not.toContain(credential);
    if (!config.hideInputs) {
      for (const key of [S.INPUT_VALUE, S.RAW_INPUT]) {
        const input = JSON.parse(result.wireAttributes[key].stringValue);
        expect(input.compound_custom.tools.wolfram_settings.authorization).toBeUndefined();
        expect(input.compound_custom.tools.wolfram_settings.query).toBe("public-query");
        expect(input.compound_custom.tools.enabled_tools).toEqual(["wolfram_alpha"]);
      }
    }
  });
});

const errorPolicies: Array<[string, TraceConfigOptions, string?]> = [
  ["default", {}],
  ["hideErrorDetails", { hideErrorDetails: true }],
  ["hideInputs", { hideInputs: true }],
  ["hideOutputs", { hideOutputs: true }],
  ["FI_HIDE_ERROR_DETAILS", {}, "FI_HIDE_ERROR_DETAILS"],
];
describe.each(errorPolicies)("provider-stopped stream with %s", (name, config, env) => {
  it("records ERROR through FISpan while preserving chunks, partial output and usage", async () => {
    const previous = env ? process.env[env] : undefined;
    if (env) process.env[env] = "true";
    try {
      instrument(config);
      const error = "synthetic-provider-stopped-early";
      streamChunks = [
        chunk({ choices: [{ index: 0, delta: { role: "assistant", content: "partial-answer" }, finish_reason: null }] }),
        chunk({ x_groq: { error, usage: { prompt_tokens: 12, completion_tokens: 5, total_tokens: 17 } } }),
        // Empty later metadata must not erase a previously observed provider error.
        chunk({ x_groq: { error: null } }),
      ];
      const result = await request(true);
      expect(result.span.status.code).toBe(2);
      expect(result.wireSpan.status.code).toBe(2);
      expect(result.span.events).toHaveLength(1);
      expect(result.span.events[0].name).toBe("exception");
      expect(result.wireSpan.events).toHaveLength(1);
      expect(result.attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(17);
      expect(Number(result.wireAttributes[S.LLM_TOKEN_COUNT_TOTAL].intValue)).toBe(17);
      if (config.hideOutputs) expect(result.exported).not.toContain("partial-answer");
      else {
        expect(result.attributes[S.OUTPUT_VALUE]).toBe("partial-answer");
        expect(result.wireAttributes[S.RAW_OUTPUT].stringValue).toContain("partial-answer");
      }
      if (name === "default") {
        expect(result.span.status.message).toBe(error);
        expect(result.span.events[0].attributes?.["exception.message"]).toBe(error);
        expect(result.exported).toContain(error);
      } else {
        expect(result.span.status.message).toBeUndefined();
        expect(result.span.events[0].attributes?.["exception.message"]).toBeUndefined();
        expect(result.exported).not.toContain(error);
      }
    } finally {
      if (env) { if (previous === undefined) delete process.env[env]; else process.env[env] = previous; }
    }
  });
});
it.each([undefined, null, ""])('does not fail a normal stream for x_groq.error=%s', async error => {
  usage = { x_groq: { error } };
  const result = await request(true);
  expect(result.span.status.code).toBe(1);
  expect(result.wireSpan.status.code).toBe(1);
  expect(result.span.events).toHaveLength(0);
});

const metrics = (zero: boolean) => ({
  [S.LLM_TOKEN_COUNT_PROMPT]: zero ? 0 : 12,
  [S.LLM_TOKEN_COUNT_COMPLETION]: zero ? 0 : 5,
  [S.LLM_TOKEN_COUNT_TOTAL]: zero ? 0 : 17,
  [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: zero ? 0 : 9,
  [S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]: zero ? 0 : 3,
  "groq.usage.dram_cached_tokens": zero ? 0 : 7,
  "groq.usage.sram_cached_tokens": zero ? 0 : 2,
});
function fixture(zero: boolean, source: string) {
  const standard = { prompt_tokens: zero ? 0 : 12, completion_tokens: zero ? 0 : 5, total_tokens: zero ? 0 : 17,
    prompt_tokens_details: { cached_tokens: zero ? 0 : 9 }, completion_tokens_details: { reasoning_tokens: zero ? 0 : 3 } };
  const hardware = { dram_cached_tokens: zero ? 0 : 7, sram_cached_tokens: zero ? 0 : 2 };
  return source === "standard" ? { usage: { ...standard, ...hardware } }
    : source === "x_groq" ? { x_groq: { usage: { ...standard, ...hardware } } }
      : { usage: standard, x_groq: { usage: hardware } };
}
function expectMetrics(result: Awaited<ReturnType<typeof request>>, zero: boolean) {
  for (const [key, value] of Object.entries(metrics(zero))) {
    expect(result.attributes[key]).toBe(value);
    expect(Number(result.wireAttributes[key]?.intValue)).toBe(value);
  }
}
describe.each([false, true])("structured usage with stream=%s", stream => {
  describe.each([false, true])("hideOutputs=%s", hideOutputs => {
    it.each(["standard", "x_groq", "split"])("exports %s usage and hardware breakdowns", async source => {
      instrument({ hideOutputs }); usage = fixture(false, source);
      const result = await request(stream);
      expectMetrics(result, false);
      if (hideOutputs) {
        expect(result.exported).not.toContain("private-answer");
        expect(result.attributes[S.RAW_OUTPUT]).toBe("__REDACTED__");
      } else expect(result.attributes[S.RAW_OUTPUT]).toContain("private-answer");
    });
    it.each(["standard", "x_groq", "split"])("preserves explicit zero %s usage", async source => {
      instrument({ hideOutputs }); usage = fixture(true, source);
      expectMetrics(await request(stream), true);
    });
  });
  it("prefers standard usage per field without dropping x_groq details", async () => {
    instrument({ hideOutputs: true });
    usage = { usage: { prompt_tokens: 0, prompt_tokens_details: { cached_tokens: 0 }, completion_tokens_details: { reasoning_tokens: 0 } },
      x_groq: { usage: { prompt_tokens: 99, completion_tokens: 5, total_tokens: 5,
        prompt_tokens_details: { cached_tokens: 99 }, completion_tokens_details: { reasoning_tokens: 99 },
        dram_cached_tokens: 0, sram_cached_tokens: 0 } } };
    const result = await request(stream);
    expect(result.attributes).toMatchObject({
      ...metrics(true), [S.LLM_TOKEN_COUNT_COMPLETION]: 5, [S.LLM_TOKEN_COUNT_TOTAL]: 5,
    });
  });
  it("does not invent metrics when optional usage is null or missing", async () => {
    instrument({ hideOutputs: true });
    usage = { usage: { prompt_tokens: 12, prompt_tokens_details: null, completion_tokens_details: null }, x_groq: { usage: null } };
    const result = await request(stream);
    expect(result.attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBe(12);
    for (const key of Object.keys(metrics(false)).filter(key => key !== S.LLM_TOKEN_COUNT_PROMPT)) {
      expect(result.attributes[key]).toBeUndefined();
      expect(result.wireAttributes[key]).toBeUndefined();
    }
  });
});
it("merges partial usage snapshots across chunks without adding counts or losing cache details", async () => {
  instrument({ hideOutputs: true });
  usageChunks = [
    { x_groq: { usage: { prompt_tokens: 12, prompt_tokens_details: { cached_tokens: 9 }, dram_cached_tokens: 7 } } },
    { usage: { completion_tokens: 5, completion_tokens_details: { reasoning_tokens: 3 }, prompt_tokens_details: {} } },
    { usage: { total_tokens: 17 }, x_groq: { usage: { sram_cached_tokens: 2 } } },
    { usage: { prompt_tokens: 12, total_tokens: 17 } },
  ];
  expectMetrics(await request(true), false);
});
