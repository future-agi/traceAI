import { describe, expect, it } from "@jest/globals";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { scrubAttribute, scrubValue } from "../privacy-runtime";
import { FITracer } from "../trace/trace-config/FITracer";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

const omitted = "<omitted: incomplete tool arguments>";

describe("shared incomplete content labels", () => {
  it.each(['{"city":"Par', '{"city":oops}', '{"city":', ''])("omits unparsed tool arguments: %s", (fragment) => {
    const messages = [{ role: "assistant", tool_calls: [{ id: "tool_demo", function: { name: "weather", arguments: fragment } }] }];
    const output = { tool_calls: [{ id: "tool_demo", name: "weather", input: fragment }] };
    const clean = JSON.parse(scrubAttribute(JSON.stringify(messages)));
    expect(clean[0].tool_calls[0]).toEqual({ id: "tool_demo", function: { name: "weather", arguments: omitted } });
    expect(JSON.parse(scrubAttribute(JSON.stringify(output))).tool_calls[0].input).toBe(omitted);
    expect(scrubValue(messages)[0].tool_calls[0].function.arguments).toBe(omitted);
    expect(messages[0].tool_calls[0].function.arguments).toBe(fragment);
  });

  it("keeps complete arguments, nested credential scrubbing, and omission idempotence", () => {
    const value = [{ tool_calls: [{ function: { arguments: '{"city":"Paris","api_key":"synthetic-only-secret"}' } }] }];
    const clean = JSON.parse(scrubAttribute(JSON.stringify(value)));
    expect(JSON.parse(clean[0].tool_calls[0].function.arguments)).toEqual({ city: "Paris", api_key: "<redacted>" });
    const alreadyClean = JSON.stringify([{ tool_calls: [{ function: { arguments: omitted } }] }]);
    expect(scrubAttribute(alreadyClean)).toBe(alreadyClean);
  });

  it("uses audio for media fragments and text for ordinary truncated prose", () => {
    expect(scrubAttribute('{"audio":"AQID')).toBe("<omitted: incomplete audio>");
  });

  it("retains normal hiding when output capture is disabled", () => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new FITracer({ tracer: provider.getTracer("offline"), traceConfig: { hideOutputs: true } });
    const span = tracer.startSpan("tool");
    span.setAttribute(SemanticConventions.OUTPUT_VALUE, JSON.stringify({ tool_calls: [{ id: "tool_demo", name: "weather", input: '{"city":"Par' }] }));
    span.setAttribute(SemanticConventions.LLM_OUTPUT_MESSAGES, JSON.stringify([{ tool_calls: [{ id: "tool_demo", function: { name: "weather", arguments: '{"city":"Par' } }] }]));
    span.end();
    const attributes = exporter.getFinishedSpans()[0].attributes;
    expect(attributes[SemanticConventions.OUTPUT_VALUE]).toBe(REDACTED_VALUE);
    expect(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES]).toBeUndefined();
    expect(JSON.stringify(attributes)).not.toContain("tool_demo");
    expect(JSON.stringify(attributes)).not.toContain("incomplete tool arguments");
  });
});
