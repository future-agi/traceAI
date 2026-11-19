import { ROOT_CONTEXT } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { DROPPED_CONTENT_COUNT, isProjectionMetadata, projectAttributes } from "../attribute-projection";
import { privacyFilter } from "../export-privacy";

const key = "fi.sampling.ratio";

test.each([1, 16, 128])("projection reserves an existing numeric ratio at a saturated %s-slot budget", limit => {
  const attributes = { "gen_ai.span.kind": "LLM", "gen_ai.operation.name": "chat", [key]: 0.25,
    ...Object.fromEntries(Array.from({ length: 160 }, (_, i) => [`gen_ai.output.messages.${i}.content`, "answer"])) };
  const projected = projectAttributes(attributes, limit);
  expect(projected.attributes[key]).toBe(0.25);
  expect(Object.keys(projected.attributes)).toHaveLength(limit === 1 ? 1 : limit);
  expect(projected.dropped).toBeGreaterThan(0);
  expect(projectAttributes(Object.fromEntries(Object.entries(attributes).reverse()), limit)).toEqual(projected);
  if (limit > 1) expect(projected.attributes[DROPPED_CONTENT_COUNT]).toBeGreaterThan(0);
});

test("zero capacity and invalid ratio values get no reservation", () => {
  expect(projectAttributes({ [key]: 0.25, "output.value": "answer" }, 0).attributes).toEqual({});
  for (const value of [0, 1, -1, NaN, Infinity, "0.25", "secret words"]) {
    expect(projectAttributes({ [key]: value, "output.value": "answer" }, 1).attributes[key]).toBeUndefined();
  }
});

test("projection's oversized-map fallback preserves the ratio without preserving content", () => {
  const attributes = { [key]: 0.25, ...Object.fromEntries(Array.from({ length: 4100 }, (_, i) => [`content.${i}`, "answer"])) };
  expect(projectAttributes(attributes, 1)).toEqual({ attributes: { [key]: 0.25 }, dropped: 4100 });
});

test.each([0.25, 0.123456789, "0.25", "private words", 1, -1])("host-loss export retains only valid numeric ratio %s", async value => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 1 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const span = provider.getTracer("test").startSpan("test", { attributes: { [key]: value, content: "private words" } }, ROOT_CONTEXT);
    span.end();
    const native = exporter.getFinishedSpans()[0];
    const clean = privacyFilter().span(native);
    expect(clean.attributes[key]).toBe(typeof value === "number" && value > 0 && value < 1 ? value : "<unknown: host-truncated>");
    expect(native.attributes[key]).toBe(value);
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    // A numeric registered secret must still be redacted, not restored as metadata.
    if (value === 0.123456789) expect(privacyFilter({}, [String(value)]).span(native).attributes[key]).not.toBe(value);
  } finally { await provider.shutdown(); }
});

test("ratio retention never creates a base64 or media exemption", () => {
  const privacy = privacyFilter();
  expect(privacy.attributes({ [key]: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" })[key]).not.toBe("iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB");
  expect(isProjectionMetadata(key)).toBe(false);
  const admitted = privacy.attributes({ [key]: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" });
  expect(admitted[key]).not.toContain("iVBORw0KGgo");
});
