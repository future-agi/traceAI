import { trace } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "@traceai/fi-core";
import { activeContext, applicationContextManager, contextBoundProvider, runIntegrationCall, runWithTracingSuppressed } from "../src/agent-context";
import { shouldSkip } from "../src/wire/fetch";

// An application with no OpenTelemetry context manager that wraps work in the agent's
// suppression helper (withTracingSuppressed). Fi-core's startActiveSpan keeps its span
// active in a private store, and the agent reads that same store, so suppression set
// before a startActiveSpan must still hold inside it, inside a nested startActiveSpan,
// and inside an SDK call the agent scopes.
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const integration = new FITracer({ tracer: contextBoundProvider(provider).getTracer("integration") });

beforeEach(() => exporter.reset());
afterAll(() => provider.shutdown());

test("no context manager is registered in this file", () => {
  expect(applicationContextManager()).toBe(false);
});

test("inside the suppression helper, spans from startActiveSpan, a nested startActiveSpan and an integration call record nothing, and wire capture skips the request", async () => {
  const seen: boolean[] = [];
  await runWithTracingSuppressed(async () => {
    await integration.startActiveSpan("outer", async outer => {
      seen.push(isTracingSuppressed(activeContext()), shouldSkip(activeContext()));
      await integration.startActiveSpan("nested", async nested => {
        seen.push(isTracingSuppressed(activeContext()), shouldSkip(activeContext()));
        // The model span of an integration call, started inside the nested span.
        await runIntegrationCall(async () => {
          const model = integration.startSpan("model");
          seen.push(isTracingSuppressed(activeContext()), shouldSkip(activeContext()));
          await Promise.resolve();
          seen.push(isTracingSuppressed(activeContext()), shouldSkip(activeContext()));
          model.end();
        });
        nested.end();
      });
      outer.end();
    });
  });
  expect(seen).toEqual([true, true, true, true, true, true, true, true]);
  // Nothing reaches Future AGI: no span at all, so no model span and no wire span.
  expect(exporter.getFinishedSpans().map(span => span.name)).toEqual([]);
  // After the helper returns, suppression is gone and spans record again.
  expect(isTracingSuppressed(activeContext())).toBe(false);
  await integration.startActiveSpan("after", async span => { span.end(); });
  expect(exporter.getFinishedSpans().map(span => span.name)).toEqual(["after"]);
  expect(trace.getSpan(activeContext())).toBeUndefined();
});
