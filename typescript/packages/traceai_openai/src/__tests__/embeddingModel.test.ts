import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { DROPPED_CONTENT_COUNT, privacyFilter } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "../instrumentation";

// Exercise the real SDK parser and instrumentor without opening a network socket.
// The SDK-contract matrix supplies OPENAI_TEST_MODULE for each supported version.
const sdk = require(process.env.OPENAI_TEST_MODULE || "openai");
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });

const modelRows = [
  { model: "text-embedding-3-small", omittedModel: "<omitted: 22 base64 chars>" },
  { model: "text-embedding-ada-002", omittedModel: "<omitted: 22 base64 chars>" },
];
const cases = modelRows.flatMap(row => [1, 64, 140].map(size => ({ ...row, size })));
const resolvedModel = "resolved-embedding-model";
const textKey = "embedding.embeddings.0.embedding.text";
const vectorKey = "embedding.embeddings.0.embedding.vector";

async function collectEmbedding({ model, size, hideEmbeddingVectors, responseModel }: {
  model: string;
  size: number;
  hideEmbeddingVectors: boolean;
  responseModel?: string;
}) {
  const exporter = new InMemorySpanExporter();
  const started: Attributes[] = [];
  const provider = new BasicTracerProvider({
    spanLimits: { attributeCountLimit: 128 },
    spanProcessors: [
      {
        onStart(span) { started.push({ ...span.attributes }); },
        onEnd() {},
        async forceFlush() {},
        async shutdown() {},
      },
      new SimpleSpanProcessor(exporter),
    ],
  });
  const options = { hideInputs: false, hideOutputs: false, hideEmbeddingVectors };
  const instrumentation = new OpenAIInstrumentation({ traceConfig: options });
  try {
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
    const input = Array.from({ length: size }, (_, index) => `synthetic-input-${index}`);
    const response = {
      object: "list",
      ...(responseModel === undefined ? {} : { model: responseModel }),
      data: input.map((_, index) => ({ object: "embedding", index, embedding: [912345.6, index] })),
      usage: { prompt_tokens: size, total_tokens: size },
    };
    const fetch = jest.fn(async (_url: unknown, _init?: RequestInit) => new Response(JSON.stringify(response), {
      headers: { "content-type": "application/json" },
    }));
    const client = new sdk.OpenAI({ apiKey: "synthetic-unused-key", maxRetries: 0, fetch });
    // Explicit float encoding prevents SDK-side base64 conversion in this fixture.
    const result = await client.embeddings.create({ model, input, encoding_format: "float" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({ model, input, encoding_format: "float" });
    // Telemetry policy must not rewrite the provider result or the SDK request.
    expect(result).toEqual(response);
    expect(input[0]).toBe("synthetic-input-0");
    await provider.forceFlush();
    expect(started).toHaveLength(1);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    const first = privacyFilter(options).span(native);
    const second = privacyFilter(options).span(first);
    expect(second.attributes).toEqual(first.attributes);
    expect(JSON.stringify(native.attributes)).toBe(before);
    return { start: started[0], native, first, second };
  } finally {
    instrumentation.disable();
    await provider.shutdown();
  }
}

// RED on r10: embeddings author only embedding.model_name, never either canonical
// model field. FI projection cannot manufacture gen_ai.request/response.model.
test.each(cases)("embedding canonical identity survives both FI exports (model=$model, batch=$size)", async ({ model, omittedModel, size }) => {
  const { start, native, first, second } = await collectEmbedding({ model, size, hideEmbeddingVectors: false, responseModel: resolvedModel });
  expect(start["gen_ai.request.model"]).toBe(model);
  expect(start["gen_ai.response.model"]).toBeUndefined();
  expect(start["embedding.model_name"]).toBe(model);
  expect(native.attributes["embedding.model_name"]).toBe(model);
  expect(native.attributes[textKey]).toBe("synthetic-input-0");
  for (const span of [native, first, second]) {
    expect(span.attributes["gen_ai.request.model"]).toBe(model);
    expect(span.attributes["gen_ai.response.model"]).toBe(resolvedModel);
    expect(span.attributes[vectorKey]).toEqual([912345.6, 0]);
    expect(span.attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
  }
  for (const span of [first, second]) {
    if (size === 1) {
      expect(span.attributes["embedding.model_name"]).toBe(model);
      expect(span.attributes[textKey]).toBe("synthetic-input-0");
      expect(span.attributes[DROPPED_CONTENT_COUNT]).toBeUndefined();
      expect(span.droppedAttributesCount).toBe(0);
    } else {
      // These are real model names, not renamed fixtures that evade media policy.
      // Retaining canonical metadata must not make the legacy alias a privacy exemption.
      expect(span.attributes["embedding.model_name"]).toBe(omittedModel);
      expect(span.attributes[textKey]).toBe("<omitted: 17 base64 chars>");
      expect(span.attributes[DROPPED_CONTENT_COUNT]).toBeGreaterThan(0);
      expect(span.droppedAttributesCount).toBeGreaterThan(0);
      expect(Object.keys(span.attributes).length).toBeLessThanOrEqual(128);
      const texts = Object.entries(span.attributes).filter(([key]) => /^embedding\.embeddings\.\d+\.embedding\.text$/.test(key));
      expect(texts.length).toBeGreaterThan(0);
      for (const [key, value] of texts) {
        const index = Number(key.split(".")[2]);
        expect(value).toBe(index < 10 ? "<omitted: 17 base64 chars>" : index < 100 ? "<omitted: 18 base64 chars>" : "<omitted: 19 base64 chars>");
      }
    }
  }
});

test.each(cases)("hideEmbeddingVectors still hides legacy identity and every vector (model=$model, batch=$size)", async ({ model, size }) => {
  const { start, native, first, second } = await collectEmbedding({ model, size, hideEmbeddingVectors: true, responseModel: resolvedModel });
  expect(start["gen_ai.request.model"]).toBe(model);
  expect(start["embedding.model_name"]).toBe("__REDACTED__");
  for (const span of [native, first, second]) {
    expect(span.attributes["gen_ai.request.model"]).toBe(model);
    expect(span.attributes["gen_ai.response.model"]).toBe(resolvedModel);
    expect(span.attributes["embedding.model_name"]).toBe("__REDACTED__");
    expect(span.attributes[vectorKey]).toBeUndefined();
    expect(Object.keys(span.attributes).filter(key => /^embedding\.embeddings\.\d+\.embedding\.vector$/.test(key))).toEqual([]);
    expect(span.attributes["raw.output"]).toBe("__REDACTED__");
    expect(JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status, links: span.links })).not.toContain("912345.6");
  }
});

test.each([1, 64, 140])("an absent response model is not invented (batch=%s)", async size => {
  const model = "text-embedding-3-small";
  const { start, native, first, second } = await collectEmbedding({ model, size, hideEmbeddingVectors: false });
  expect(start["gen_ai.request.model"]).toBe(model);
  expect(start["gen_ai.response.model"]).toBeUndefined();
  for (const span of [native, first, second]) {
    expect(span.attributes["gen_ai.request.model"]).toBe(model);
    expect(span.attributes["gen_ai.response.model"]).toBeUndefined();
  }
});
