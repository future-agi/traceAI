import { Attributes, context, trace } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { VercelAIInstrumentation } from "../auto";
import { safelyGetFIAttributes } from "../utils";

const INPUT_TOKENS = SemanticConventions.LLM_TOKEN_COUNT_PROMPT;
const TOTAL_TOKENS = SemanticConventions.LLM_TOKEN_COUNT_TOTAL;
const OUTPUT_TOKENS = SemanticConventions.LLM_TOKEN_COUNT_COMPLETION;
const operations = ["ai.embed", "ai.embed.doEmbed", "ai.embedMany", "ai.embedMany.doEmbed"];

describe("AI SDK 5 embedding usage attribute shape", () => {
  test.each(operations)("normalizes input and total tokens for %s with or without a function ID", operation => {
    for (const suffix of ["", " customer-function"]) {
      const attributes = {
        "operation.name": `${operation}${suffix}`,
        "ai.usage.tokens": operation.includes("embedMany") ? 11 : 7,
      };
      const normalized = safelyGetFIAttributes(attributes);
      expect(normalized).toMatchObject({
        [INPUT_TOKENS]: attributes["ai.usage.tokens"],
        [TOTAL_TOKENS]: attributes["ai.usage.tokens"],
      });
      expect(normalized?.[OUTPUT_TOKENS]).toBeUndefined();
      expect(attributes).not.toHaveProperty(INPUT_TOKENS);
      expect(attributes).not.toHaveProperty(TOTAL_TOKENS);
    }
  });

  test.each([INPUT_TOKENS, TOTAL_TOKENS])("preserves an existing zero %s while filling the other counter", key => {
    const normalized = safelyGetFIAttributes({
      "operation.name": "ai.embed.doEmbed",
      "ai.usage.tokens": 7,
      [key]: 0,
    });
    expect(normalized).toMatchObject({ [INPUT_TOKENS]: 7, [TOTAL_TOKENS]: 7, [key]: 0 });
  });

  test("preserves both existing canonical counters", () => {
    expect(safelyGetFIAttributes({
      "operation.name": "ai.embedMany",
      "ai.usage.tokens": 11,
      [INPUT_TOKENS]: 13,
      [TOTAL_TOKENS]: 17,
    })).toMatchObject({ [INPUT_TOKENS]: 13, [TOTAL_TOKENS]: 17 });
  });

  test("keeps zero embedding usage", () => {
    expect(safelyGetFIAttributes({ "operation.name": "ai.embed", "ai.usage.tokens": 0 }))
      .toMatchObject({ [INPUT_TOKENS]: 0, [TOTAL_TOKENS]: 0 });
  });

  test.each([undefined, "7", [7], Number.NaN, Number.POSITIVE_INFINITY, -1])(
    "does not synthesize counters for invalid embedding usage %s", tokens => {
      const normalized = safelyGetFIAttributes({ "operation.name": "ai.embed", "ai.usage.tokens": tokens });
      expect(normalized?.[INPUT_TOKENS]).toBeUndefined();
      expect(normalized?.[TOTAL_TOKENS]).toBeUndefined();
    },
  );

  test.each(["ai.generateText", "ai.generateText.doGenerate", "ai.toolCall", "ai.embedOther", undefined])(
    "does not apply embedding usage to unrelated operation %s", operation => {
      const normalized = safelyGetFIAttributes({ "operation.name": operation, "ai.usage.tokens": 7 });
      expect(normalized?.[INPUT_TOKENS]).toBeUndefined();
      expect(normalized?.[TOTAL_TOKENS]).toBeUndefined();
    },
  );

  test("chat prompt/completion usage remains unchanged", () => {
    expect(safelyGetFIAttributes({
      "operation.name": "ai.generateText.doGenerate",
      "ai.usage.promptTokens": 3,
      "ai.usage.completionTokens": 4,
      "ai.usage.tokens": 99,
    })).toMatchObject({ [INPUT_TOKENS]: 3, [OUTPUT_TOKENS]: 4 });
  });
});

describe("real AI SDK 6.0.320 embedding usage in auto mode", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider();
  const manager = new AsyncLocalStorageContextManager();
  let original: any;
  let sdk: any;
  let createOpenAI: any;
  let instrumentor: VercelAIInstrumentation;

  beforeAll(() => {
    provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
    context.setGlobalContextManager(manager.enable());
    trace.setGlobalTracerProvider(provider);
    original = require("ai");
    ({ createOpenAI } = require("@ai-sdk/openai"));
    instrumentor = new VercelAIInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentor.setTracerProvider(provider);
    instrumentor.enable();
    sdk = instrumentor.manuallyInstrument(original, "6.0.320");
  });
  beforeEach(() => exporter.reset());
  afterAll(async () => {
    instrumentor?.disable();
    await provider.shutdown();
    context.disable();
    trace.disable();
    manager.disable();
  });

  test("keeps a canonical total that the span set itself when embedding usage adds the input counter", () => {
    const fake = instrumentor.manuallyInstrument({
      embed({ experimental_telemetry }: any) {
        const attributes: Attributes = { "operation.name": "ai.embed.doEmbed", [TOTAL_TOKENS]: 17, [OUTPUT_TOKENS]: 0 };
        const span = experimental_telemetry.tracer.startSpan("ai.embed.doEmbed", { attributes });
        span.setAttribute("ai.usage.tokens", 7);
        span.end();
      },
    }, "6.0.320");
    fake.embed({});
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes[INPUT_TOKENS]).toBe(7);
    expect(spans[0].attributes[TOTAL_TOKENS]).toBe(17);
  });

  test.each([
    ["embed", ["hello"], 7],
    ["embedMany", ["hello", "world"], 11],
  ] as const)("%s keeps usage on the operation and doEmbed spans without content aliases", async (method, values, tokens) => {
    const embeddings = values.map((_, index) => [0.123456 + index, 0.654321]);
    const fetch = jest.fn(async (url: string, init: RequestInit) => {
      expect(String(url)).toBe("https://embedding.test/v1/embeddings");
      const body = JSON.parse(init.body as string);
      expect(body.input).toEqual(values);
      return new Response(JSON.stringify({
        object: "list",
        model: "text-embedding-3-small",
        data: embeddings.map((embedding, index) => ({ object: "embedding", index, embedding })),
        usage: { prompt_tokens: tokens, total_tokens: tokens },
      }), { status: 200, headers: { "content-type": "application/json" } });
    });
    const openai = createOpenAI({ apiKey: "test-key", baseURL: "https://embedding.test/v1", fetch });
    const options = {
      model: openai.embedding("text-embedding-3-small"),
      ...(method === "embed" ? { value: values[0] } : { values: [...values] }),
      maxRetries: 0,
    };
    const plain = await context.with(suppressTracing(context.active()), () => original[method](options));
    const result = await sdk[method](options);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.usage).toEqual(plain.usage);
    expect(result.usage.tokens).toBe(tokens);
    const resultKey = method === "embed" ? "embedding" : "embeddings";
    expect(result[resultKey]).toEqual(plain[resultKey]);
    expect(result[resultKey]).toEqual(method === "embed" ? embeddings[0] : embeddings);
    expect(options).not.toHaveProperty("experimental_telemetry");

    const spans = exporter.getFinishedSpans();
    expect(spans.map(span => span.name).sort()).toEqual([`ai.${method}`, `ai.${method}.doEmbed`]);
    for (const span of spans) {
      expect(span.attributes).toMatchObject({ [INPUT_TOKENS]: tokens, [TOTAL_TOKENS]: tokens });
      expect(span.attributes[OUTPUT_TOKENS]).toBeUndefined();
      expect(Object.keys(span.attributes).some(key => key.startsWith("ai."))).toBe(false);
      expect(JSON.stringify(span.attributes)).not.toContain("0.123456");
    }
  });

  test.each([INPUT_TOKENS, TOTAL_TOKENS])("auto mode preserves canonical %s during native span updates", key => {
    const emit = instrumentor.manuallyInstrument({
      embed({ experimental_telemetry }: any) {
        const attributes: Attributes = { "operation.name": "ai.embed.doEmbed", [key]: 0 };
        const span = experimental_telemetry.tracer.startSpan("ai.embed.doEmbed", { attributes });
        span.setAttribute("ai.usage.tokens", 7);
        span.end();
        return "unchanged";
      },
    }, "6.0.320");
    expect(emit.embed({})).toBe("unchanged");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes)
      .toMatchObject({ [INPUT_TOKENS]: 7, [TOTAL_TOKENS]: 7, [key]: 0 });
  });
});
