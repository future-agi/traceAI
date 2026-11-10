import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { privacyFilter, REDACTED_VALUE } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "../instrumentation";

// Use the real SDK parser with a fake transport; no network requests are made.
const sdk = require(process.env.OPENAI_TEST_MODULE || "openai");
const savedEnv = { ...process.env };
beforeEach(() => {
  for (const key of Object.keys(process.env)) {
    if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  }
});
afterEach(() => { process.env = { ...savedEnv }; });

const usageRows = [
  { label: "ordinary usage", usage: { prompt_tokens: 7, total_tokens: 7 } },
  { label: "distinct provider totals", usage: { prompt_tokens: 7, total_tokens: 11 } },
  { label: "zero usage", usage: { prompt_tokens: 0, total_tokens: 0 } },
  { label: "omitted usage", usage: undefined },
];

describe.each([false, true])("T7-TS-OPENAI-002 embedding usage (hideOutputs=%s)", (hideOutputs) => {
  test.each(usageRows)("retains $label without changing the response", async ({ usage }) => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    const traceConfig = { hideInputs: false, hideOutputs, hideEmbeddingVectors: false };
    const instrumentation = new OpenAIInstrumentation({ traceConfig });
    try {
      instrumentation.setTracerProvider(provider);
      instrumentation.manuallyInstrument(sdk);
      const body = {
        model: "text-embedding-3-small",
        input: "hello",
        encoding_format: "float" as const,
      };
      const response = {
        object: "list",
        model: body.model,
        data: [{ object: "embedding", index: 0, embedding: [0.1, 0.2] }],
        ...(usage === undefined ? {} : { usage }),
      };
      const fetch = jest.fn(async (_url: unknown, _init?: RequestInit) => new Response(JSON.stringify(response), {
        headers: { "content-type": "application/json" },
      }));
      const client = new sdk.OpenAI({ apiKey: "FAKE_OPENAI_KEY", maxRetries: 0, fetch });
      const result = await client.embeddings.create(body);
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual(body);
      expect(result).toEqual(response);
      expect(result.usage).toEqual(usage);
      await provider.forceFlush();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      const native = exporter.getFinishedSpans()[0];
      expect(native.name).toBe("OpenAI Embeddings");
      expect(native.status.code).toBe(SpanStatusCode.OK);
      if (hideOutputs) {
        expect(native.attributes["raw.output"]).toBe(REDACTED_VALUE);
      } else {
        expect(JSON.parse(native.attributes["raw.output"] as string)).toEqual(response);
      }

      // Canonical numeric usage must survive both native and FI export paths,
      // even when raw.output is hidden. Never invent output tokens or totals.
      const projected = privacyFilter(traceConfig).span(native);
      for (const span of [native, projected]) {
        expect(span.attributes["gen_ai.usage.input_tokens"]).toBe(usage?.prompt_tokens);
        expect(span.attributes["gen_ai.usage.total_tokens"]).toBe(usage?.total_tokens);
        expect(span.attributes["gen_ai.usage.output_tokens"]).toBeUndefined();
        if (hideOutputs) {
          expect(span.attributes["raw.output"]).toBe(REDACTED_VALUE);
        }
      }
    } finally {
      instrumentation.disable();
      await provider.shutdown();
    }
  });
});
