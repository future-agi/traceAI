import { afterAll, afterEach, beforeEach, describe, expect, it } from "@jest/globals";
import { SpanStatusCode, TimeInput } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { traceConfigMetadata } from "../trace/trace-config/constants";
import { TraceConfigOptions } from "../trace/trace-config/types";

const EMAIL = "error.person@example.com";
const MESSAGE = `Callback failed for ${EMAIL}`;
const MASKED = "Callback failed for <EMAIL_ADDRESS>";
const TIME: [number, number] = [1700000000, 123000000];

// A real SDK exporter catches regressions at the FISpan boundary, without mocks.
describe("event attribute masking", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const savedEnv: Record<string, string | undefined> = {};
  const keys = Object.values(traceConfigMetadata).map(m => m.envKey);
  const tracer = (traceConfig?: TraceConfigOptions) => new FITracer({ tracer: provider.getTracer("events"), traceConfig });
  const finished = () => exporter.getFinishedSpans()[0];

  beforeEach(() => {
    exporter.reset();
    for (const key of keys) {
      savedEnv[key] = process.env[key];
      delete process.env[key];
    }
  });
  afterEach(() => {
    for (const key of keys) {
      if (savedEnv[key] === undefined) delete process.env[key];
      else process.env[key] = savedEnv[key];
    }
  });
  afterAll(async () => { await provider.shutdown(); });

  it.each(["option", "environment"])("masks recorded exceptions with PII enabled via %s", source => {
    if (source === "environment") process.env.FI_PII_REDACTION = "true";
    const span = tracer(source === "option" ? { piiRedaction: true } : undefined).startSpan("exception");
    const error = Object.freeze(new TypeError(MESSAGE));
    const stack = error.stack;
    span.recordException(error, TIME);
    span.recordException(MESSAGE, TIME);
    span.recordException(Object.freeze({ name: "Error", message: MESSAGE, stack: `Error: ${MESSAGE}\n    at callback (/app.js:1:1)` }), TIME);
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();

    const events = finished().events;
    expect(events).toHaveLength(3);
    for (const event of events) {
      expect(event.name).toBe("exception");
      expect(event.time).toEqual(TIME);
      expect(event.attributes?.["exception.message"]).toBe(MASKED);
    }
    expect(events[0].attributes?.["exception.type"]).toBe("TypeError");
    expect(events[0].attributes?.["exception.stacktrace"]).toContain(MASKED);
    expect(events[2].attributes?.["exception.stacktrace"]).toContain(MASKED);
    expect(JSON.stringify(events)).not.toContain(EMAIL);
    expect(error.message).toBe(MESSAGE);
    expect(error.stack).toBe(stack);
  });

  it.each(["exception", "provider.failure"])("masks all retained attributes of addEvent(%s) without mutating them", name => {
    const attributes = Object.freeze({
      "exception.type": "TypeError",
      "exception.message": MESSAGE,
      "exception.stacktrace": `TypeError: ${MESSAGE}\n    at callback (/app.js:1:1)`,
      "provider.error.message": MESSAGE,
      "exception.escaped": true,
      "retry.attempt": 2,
      recipients: Object.freeze([EMAIL, "safe"]) as unknown as string[],
    });
    const span = tracer({ piiRedaction: true }).startSpan("event");
    span.addEvent(name, attributes, TIME);
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();
    const event = finished().events[0];
    expect(event.time).toEqual(TIME);
    expect(event.attributes).toEqual({
      ...attributes,
      "exception.message": MASKED,
      "exception.stacktrace": `TypeError: ${MASKED}\n    at callback (/app.js:1:1)`,
      "provider.error.message": MASKED,
      recipients: ["<EMAIL_ADDRESS>", "safe"],
    });
    expect(finished().attributes["error.type"]).toBe(name === "exception" ? "TypeError" : "_OTHER");
    expect(attributes["exception.message"]).toBe(MESSAGE);
    expect(attributes.recipients).toEqual([EMAIL, "safe"]);
  });

  it("drops hidden error details before redacting other event attributes", () => {
    const span = tracer({ piiRedaction: true, hideErrorDetails: true }).startSpan("hidden");
    span.recordException(new Error(MESSAGE));
    for (const name of ["exception", "provider.failure"]) {
      span.addEvent(name, {
        "exception.type": "Error", "exception.message": MESSAGE, "exception.stacktrace": MESSAGE,
        "error.message": MESSAGE, "provider.error.message": MESSAGE, "tool.error_message": MESSAGE,
        "exception.escaped": true, recipient: EMAIL,
      });
    }
    span.end();
    expect(finished().events[0].attributes).toEqual({ "exception.type": "Error" });
    for (const event of finished().events.slice(1)) {
      expect(event.attributes).toEqual({ "exception.type": "Error", "exception.escaped": true, recipient: "<EMAIL_ADDRESS>" });
    }
  });

  it("applies shared input/output masking to event attributes too", () => {
    const span = tracer({ hideInputs: true, hideOutputs: true }).startSpan("content");
    span.addEvent("payload", { "input.value": MESSAGE, "output.value": MESSAGE, "exception.message": MESSAGE, count: 1 });
    span.end();
    expect(finished().events[0].attributes).toEqual({ "input.value": "__REDACTED__", "output.value": "__REDACTED__", count: 1 });
  });

  it("leaves exception details intact when privacy is off", () => {
    const span = tracer({ piiRedaction: false, hideErrorDetails: false }).startSpan("unmasked");
    const error = new TypeError(MESSAGE);
    span.recordException(error, TIME);
    span.addEvent("exception", { "exception.type": "TypeError", "exception.message": MESSAGE }, TIME);
    span.end();
    expect(finished().events[0].attributes).toEqual({ "exception.type": "TypeError", "exception.message": MESSAGE, "exception.stacktrace": error.stack });
    expect(finished().events[1].attributes?.["exception.message"]).toBe(MESSAGE);
  });

  it.each([false, true])("preserves attribute-free time overloads (PII %s)", piiRedaction => {
    const span = tracer({ piiRedaction }).startSpan("time");
    const times: TimeInput[] = [TIME, 1700000000123, new Date(1700000000123)];
    for (const time of times) {
      span.addEvent("exception", time);
      span.addEvent("event", undefined, time);
    }
    span.addEvent("untimed");
    span.end();
    expect(finished().events).toHaveLength(7);
    for (const event of finished().events.slice(0, 6)) {
      expect(event.time).toEqual(TIME);
      expect(event.attributes).toEqual({});
    }
  });
});
