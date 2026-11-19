import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

const key = "db.vector.query.filter";
const value = JSON.stringify({ category: { $eq: "synthetic-query-filter-value" } });

describe("vector query filter input policy", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  afterEach(() => exporter.reset());
  afterAll(() => provider.shutdown());

  for (const flag of ["hideInputs", "hideInputText"] as const) {
    for (const placement of ["creation", "setter", "link"] as const) {
      it(`${flag} redacts the complete filter at ${placement}`, () => {
        const tracer = new FITracer({ tracer: provider.getTracer("vector-filter"), traceConfig: { [flag]: true } });
        const span = tracer.startSpan("query", {
          attributes: { "db.system": "pinecone", ...(placement === "creation" ? { [key]: value } : {}) },
          links: placement === "link" ? [{
            context: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 },
            attributes: { [key]: value },
          }] : [],
        });
        if (placement === "setter") {
          span.setAttributes({ [key]: value });
          span.setAttribute(key, value);
        }
        span.end();
        const finished = exporter.getFinishedSpans()[0];
        const attributes = placement === "link" ? finished.links[0].attributes : finished.attributes;
        expect(attributes?.[key]).toBe(REDACTED_VALUE);
        expect(JSON.stringify({ attributes: finished.attributes, links: finished.links })).not.toContain("synthetic-query-filter-value");
        expect(finished.attributes["db.system"]).toBe("pinecone");
      });
    }
  }

  for (const config of [{}, { hideInputs: false, hideInputText: false }, { hideOutputs: true }, { hideOutputText: true }]) {
    it(`preserves filters for ${JSON.stringify(config)}`, () => {
      const tracer = new FITracer({ tracer: provider.getTracer("vector-filter"), traceConfig: config });
      tracer.startSpan("query", { attributes: { [key]: value } }).end();
      expect(exporter.getFinishedSpans()[0].attributes[key]).toBe(value);
    });
  }
});
