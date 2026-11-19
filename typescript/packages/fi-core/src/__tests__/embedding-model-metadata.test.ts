import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { DROPPED_CONTENT_COUNT, isProjectionMetadata, projectAttributes, protectedFamily } from "../attribute-projection";
import { privacyFilter } from "../export-privacy";
import { FITracer } from "../trace/trace-config/FITracer";

const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });

// Projection-only carry from r2. Retention priority does not exempt a value from privacy.
// Hiding embedding vectors also hides the model name, so the vector-hiding exemption cases are dropped.
test("projection reserves exact legacy model identity before embedding payload groups", () => {
  const attrs: Attributes = {
    "fi.span.kind": "EMBEDDING", "gen_ai.operation.name": "embeddings",
    "gen_ai.provider.name": "mistralai", "gen_ai.request.model": "requested-embedding-alias",
    "embedding.model_name": "requested-embedding-alias", "gen_ai.response.model": "resolved-model",
    "gen_ai.response.id": "embedding-id", "gen_ai.usage.input_tokens": 140,
    "gen_ai.usage.total_tokens": 140,
  };
  for (let index = 0; index < 140; index++) {
    attrs[`embedding.embeddings.${index}.embedding.text`] = `synthetic-input-${index}`;
    attrs[`embedding.embeddings.${index}.embedding.vector`] = [912345.6, index];
  }
  const projected = projectAttributes(attrs, 128);
  expect(protectedFamily("embedding.model_name")).toBe(protectedFamily("gen_ai.request.model"));
  expect(isProjectionMetadata("embedding.model_name")).toBe(false);
  expect(projected.attributes["embedding.model_name"]).toBe("requested-embedding-alias");
  expect(projected.attributes["gen_ai.usage.total_tokens"]).toBe(140);
  expect(projected.attributes["embedding.embeddings.0.embedding.text"]).toBe("synthetic-input-0");
  expect(projected.attributes["embedding.embeddings.0.embedding.vector"]).toEqual([912345.6, 0]);
  expect(projected.attributes[DROPPED_CONTENT_COUNT]).toBeGreaterThan(0);
  expect(Object.keys(projected.attributes).length).toBeLessThanOrEqual(128);
  expect(projectAttributes(Object.fromEntries(Object.entries(attrs).reverse()), 128)).toEqual(projected);
});

// Only public cases are covered here; this does not grant a vector-hiding exemption.
// Native projection keeps the alias, but a detached FI export has lost siblings.
// Unlike gen_ai.request.model, embedding.model_name is not arbitrary-base64 exempt.
// These identifiers (and synthetic-input-0) are all in the base64/base64url alphabet.
const modelRows = [
  { model: "requested-embedding-alias", omittedModel: "<omitted: 25 base64 chars>" },
  { model: "mistral-embed", omittedModel: "<omitted: 13 base64 chars>" },
  { model: "text-embedding-3-small", omittedModel: "<omitted: 22 base64 chars>" },
  { model: "codestral-embed-2505", omittedModel: "<omitted: 20 base64 chars>" },
];
const batchRows = [
  { size: 1, dropped: 0, attributeCount: 11 },
  { size: 64, dropped: 10, attributeCount: 128 },
  { size: 140, dropped: 86, attributeCount: 128 },
];
test.each(modelRows.flatMap(row => batchRows.map(batch => ({ ...row, ...batch, privateContent: false }))))
("native model retention and FI projection-loss privacy (model=$model, private=$privateContent, batch=$size)", async ({ model, omittedModel, privateContent, size, dropped, attributeCount }) => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const options = { hideInputs: privateContent, hideOutputs: privateContent, hideEmbeddingVectors: privateContent };
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("embedding-regression"), traceConfig: options });
    const start: Attributes = {
      "fi.span.kind": "EMBEDDING", "gen_ai.operation.name": "embeddings",
      "gen_ai.provider.name": "mistralai", "gen_ai.request.model": model,
      "embedding.model_name": model,
    };
    for (let index = 0; index < Math.min(size, 64); index++) {
      start[`embedding.embeddings.${index}.embedding.text`] = `synthetic-input-${index}`;
    }
    const span = tracer.startSpan("embedding", { attributes: start });
    const response: Attributes = {
      "gen_ai.response.model": "resolved-model", "gen_ai.response.id": "embedding-id",
      "gen_ai.usage.input_tokens": size, "gen_ai.usage.total_tokens": size,
    };
    for (let index = 0; index < size; index++) {
      response[`embedding.embeddings.${index}.embedding.vector`] = [912345.6, index];
    }
    span.setAttributes(response);
    span.end();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    const first = privacyFilter(options).span(native);
    const second = privacyFilter(options).span(first);
    // At batches 64/140 there are 137/213 candidates: nine protected fields,
    // 118 content keys and one marker occupy 128 slots. FI drops 10/86 keys.
    expect(native.droppedAttributesCount).toBe(dropped);
    expect(first.droppedAttributesCount).toBe(dropped);
    expect(second.droppedAttributesCount).toBe(dropped);
    for (const [surface, attrs] of [["native", native.attributes], ["first", first.attributes], ["second", second.attributes]] as const) {
      const projectedExport = surface !== "native" && dropped > 0;
      expect(attrs["embedding.model_name"]).toBe(projectedExport ? omittedModel : model);
      expect(attrs["gen_ai.request.model"]).toBe(model);
      expect(attrs["gen_ai.response.model"]).toBe("resolved-model");
      expect(attrs["gen_ai.response.id"]).toBe("embedding-id");
      expect(attrs["gen_ai.usage.input_tokens"]).toBe(size);
      expect(attrs["gen_ai.usage.total_tokens"]).toBe(size);
      expect(Object.keys(attrs).length).toBe(attributeCount);
      expect(attrs[DROPPED_CONTENT_COUNT]).toBe(dropped || undefined);
      expect(attrs["traceai.capture.host_data_unknown"]).toBeUndefined();
      expect(attrs["embedding.embeddings.0.embedding.text"]).toBe(projectedExport ? "<omitted: 17 base64 chars>" : "synthetic-input-0");
      expect(attrs["embedding.embeddings.0.embedding.vector"]).toEqual([912345.6, 0]);
      if (projectedExport) {
        const texts = Object.entries(attrs).filter(([key]) => /^embedding\.embeddings\.\d+\.embedding\.text$/.test(key));
        expect(texts.length).toBeGreaterThan(0);
        for (const [key, value] of texts) {
          const index = Number(key.split(".")[2]);
          expect(value).toBe(index < 10 ? "<omitted: 17 base64 chars>" : "<omitted: 18 base64 chars>");
        }
      }
    }
    expect(second.attributes).toEqual(first.attributes);
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});
