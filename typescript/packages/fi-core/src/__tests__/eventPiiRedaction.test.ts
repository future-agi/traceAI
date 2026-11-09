import { describe, it, expect, beforeEach } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { TraceConfigOptions } from "../trace/trace-config/types";

/**
 * piiRedaction covers the whole exported span: span attributes, event attributes, the recorded
 * exception (message and stack trace) and the ERROR status description. With piiRedaction off,
 * events, exceptions and the status are exported exactly as before.
 */

const EMAIL = "jane.doe@example.com";

/** Everything the exporter is handed for the span, as text */
function exported(span: ReadableSpan): string {
  return JSON.stringify({
    name: span.name,
    attributes: span.attributes,
    events: span.events,
    status: span.status,
    links: span.links,
  });
}

describe("PII redaction of events, exceptions and the status", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });

  beforeEach(() => {
    exporter.reset();
  });

  function tracerWith(traceConfig: TraceConfigOptions) {
    return new FITracer({ tracer: provider.getTracer("test"), traceConfig });
  }

  function onlySpan(): ReadableSpan {
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  /** A failing call whose error text quotes the user's email */
  function failingCall(traceConfig: TraceConfigOptions): ReadableSpan {
    const span = tracerWith(traceConfig).startSpan("llm", { attributes: { "input.value": `mail ${EMAIL}` } });
    span.addEvent("retry", { "retry.reason": `rate limited for ${EMAIL}`, "retry.attempt": 2 });
    const error = new Error(`400 unknown user ${EMAIL}`);
    span.recordException(error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
    span.end();
    return onlySpan();
  }

  describe("piiRedaction on, hideErrorDetails off", () => {
    const config = { piiRedaction: true, hideErrorDetails: false };

    it("the email is in no exported attribute, event or status", () => {
      const span = failingCall(config);
      expect(exported(span)).not.toContain(EMAIL);
      expect(exported(span)).toContain("<EMAIL_ADDRESS>");
    });

    it("the recorded exception keeps its type, with the message and stack trace redacted", () => {
      const exceptions = failingCall(config).events.filter((e) => e.name === "exception");
      // One event: the redacted one replaces the original, it is not added next to it
      expect(exceptions).toHaveLength(1);
      const [event] = exceptions;
      expect(event?.attributes?.["exception.type"]).toBe("Error");
      expect(event?.attributes?.["exception.message"]).toBe("400 unknown user <EMAIL_ADDRESS>");
      expect(String(event?.attributes?.["exception.stacktrace"])).toContain("<EMAIL_ADDRESS>");
      expect(String(event?.attributes?.["exception.stacktrace"])).not.toContain(EMAIL);
    });

    it("the ERROR status description is redacted", () => {
      const span = failingCall(config);
      expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "400 unknown user <EMAIL_ADDRESS>" });
      expect(span.attributes["error.type"]).toBe("Error");
    });

    it("event attributes are redacted, non-string values are kept", () => {
      const event = failingCall(config).events.find((e) => e.name === "retry");
      expect(event?.attributes).toEqual({ "retry.reason": "rate limited for <EMAIL_ADDRESS>", "retry.attempt": 2 });
    });

    it("a thrown string is redacted", () => {
      const span = tracerWith(config).startSpan("llm");
      span.recordException(`no account for ${EMAIL}`);
      span.end();
      expect(exported(onlySpan())).not.toContain(EMAIL);
    });

    it("addEvent with a time and no attributes still records the event", () => {
      const span = tracerWith(config).startSpan("llm");
      span.addEvent("tick", [1, 0]);
      span.end();
      const [event] = onlySpan().events;
      expect(event.name).toBe("tick");
      expect(event.time).toEqual([1, 0]);
    });
  });

  it("piiRedaction on, hideErrorDetails on: error text is dropped, other event attributes redacted", () => {
    const span = failingCall({ piiRedaction: true, hideErrorDetails: true });
    expect(exported(span)).not.toContain(EMAIL);
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
  });

  it("piiRedaction off: events, the exception and the status are exported as before", () => {
    const span = failingCall({ hideErrorDetails: false });
    const exception = span.events.find((e) => e.name === "exception");
    expect(exception?.attributes?.["exception.message"]).toBe(`400 unknown user ${EMAIL}`);
    expect(span.events.find((e) => e.name === "retry")?.attributes).toEqual({
      "retry.reason": `rate limited for ${EMAIL}`,
      "retry.attempt": 2,
    });
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: `400 unknown user ${EMAIL}` });
    expect(span.attributes["input.value"]).toBe(`mail ${EMAIL}`);
  });
});
