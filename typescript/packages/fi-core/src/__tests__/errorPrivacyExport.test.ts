import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "@jest/globals";
import { createServer } from "node:http";
import { AddressInfo } from "node:net";
import { SpanStatusCode } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { traceConfigMetadata } from "../trace/trace-config/constants";
import { TraceConfigOptions } from "../trace/trace-config/types";

const EMAIL = "review.person@example.com";
const SECRET = "sk_test_abcdefghijklmnopqrstuvwxyz";
const MESSAGE = `Failure for ${EMAIL} using ${SECRET}`;
const MASKED_MESSAGE = "Failure for <EMAIL_ADDRESS> using <API_KEY>";
const TYPE = `Error for ${EMAIL} using ${SECRET}`;
const MASKED_TYPE = "Error for <EMAIL_ADDRESS> using <API_KEY>";

type WireAttributes = { key: string; value: { stringValue?: string } }[];
interface WireSpan {
  status: { code: number; message?: string };
  attributes: WireAttributes;
  events: { attributes: WireAttributes }[];
}
interface WireRequest {
  resourceSpans: { scopeSpans: { spans: WireSpan[] }[] }[];
}
const attribute = (attributes: WireAttributes, key: string) =>
  attributes.find(item => item.key === key)?.value.stringValue;

// Inspect the actual OTLP/HTTP payload: masking only the exception event is not
// sufficient when the same error is also exported in status or error.type.
describe("error privacy across the entire OTLP export", () => {
  const requests: WireRequest[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      requests.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  let provider: BasicTracerProvider;
  const savedEnv: Record<string, string | undefined> = {};
  const keys = Object.values(traceConfigMetadata).map(metadata => metadata.envKey);
  const tracer = (traceConfig?: TraceConfigOptions) =>
    new FITracer({ tracer: provider.getTracer("error-privacy"), traceConfig });
  const exported = async (redacted = true) => {
    await provider.forceFlush();
    const spans = requests.flatMap(request => request.resourceSpans.flatMap(resource =>
      resource.scopeSpans.flatMap(scope => scope.spans)));
    expect(spans).toHaveLength(1);
    if (redacted) {
      // Scan the full request, not just the event attributes or status under test.
      const payload = JSON.stringify(requests);
      expect(payload).not.toContain(EMAIL);
      expect(payload).not.toContain(SECRET);
    }
    return spans[0];
  };

  beforeAll(async () => {
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/traces`,
    }))] });
  });
  beforeEach(() => {
    requests.length = 0;
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
  afterAll(async () => {
    await provider.shutdown();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });

  describe.each(["option", "environment"])("PII configured via %s", source => {
    let fiTracer: FITracer;
    beforeEach(() => {
      if (source === "environment") process.env.FI_PII_REDACTION = "true";
      fiTracer = tracer(source === "option" ? { piiRedaction: true } : undefined);
    });

    it.each(["message", "stack", "status-only"])("redacts the retained %s status description", async form => {
      const span = fiTracer.startSpan("status");
      const error = Object.freeze(new TypeError(MESSAGE));
      const originalStack = error.stack;
      if (form !== "status-only") span.recordException(error);
      const status = Object.freeze({ code: SpanStatusCode.ERROR, message: form === "stack" ? error.stack : error.message });
      span.setStatus(status);
      span.end();
      const wire = await exported();
      expect(wire.status).toEqual({ code: SpanStatusCode.ERROR, message: MASKED_MESSAGE });
      expect(error.message).toBe(MESSAGE);
      expect(error.stack).toBe(originalStack);
      expect(status.message).toBe(form === "stack" ? originalStack : MESSAGE);
    });

    it.each(["addEvent", "recordException", "provider-code"])("redacts error.type derived from %s", async method => {
      const span = fiTracer.startSpan("derived-type");
      const attributes = Object.freeze({ "exception.type": TYPE, "exception.message": MESSAGE });
      class SensitiveError extends Error {}
      // JavaScript permits dynamic constructor names (e.g. generated error classes).
      Object.defineProperty(SensitiveError, "name", { value: TYPE });
      const error = Object.freeze(method === "provider-code"
        ? Object.assign(new Error(MESSAGE), { code: SECRET })
        : new SensitiveError(MESSAGE));
      const originalStack = error.stack;
      if (method === "addEvent") span.addEvent("exception", attributes);
      else span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR });
      span.end();
      const wire = await exported();
      expect(attribute(wire.attributes, "error.type")).toBe(method === "provider-code" ? "<API_KEY>" : MASKED_TYPE);
      expect(attribute(wire.events[0].attributes, "exception.message")).toBe(MASKED_MESSAGE);
      expect(attributes["exception.type"]).toBe(TYPE);
      expect(error.message).toBe(MESSAGE);
      expect(error.stack).toBe(originalStack);
      expect(SensitiveError.name).toBe(TYPE);
    });

    it("hides error details before redacting retained classification", async () => {
      if (source === "environment") process.env.FI_HIDE_ERROR_DETAILS = "true";
      const span = tracer(source === "option" ? { piiRedaction: true, hideErrorDetails: true } : undefined).startSpan("hidden");
      const status = Object.freeze({ code: SpanStatusCode.ERROR, message: MESSAGE });
      span.addEvent("exception", { "exception.type": TYPE, "exception.message": MESSAGE, "exception.stacktrace": MESSAGE });
      span.setStatus(status);
      span.end();
      const wire = await exported();
      expect(wire.status.message).toBeUndefined();
      expect(attribute(wire.attributes, "error.type")).toBe(MASKED_TYPE);
      expect(wire.events[0].attributes).toEqual([{ key: "exception.type", value: { stringValue: MASKED_TYPE } }]);
      expect(status.message).toBe(MESSAGE);
    });

    it.each(["creation", "setAttribute", "setAttributes"])("preserves masked explicit error.type from %s", async placement => {
      const explicit = `Explicit ${EMAIL} using ${SECRET}`;
      const span = fiTracer.startSpan("explicit", placement === "creation" ? { attributes: { "error.type": explicit } } : undefined);
      if (placement === "setAttribute") span.setAttribute("error.type", explicit);
      if (placement === "setAttributes") span.setAttributes({ "error.type": explicit });
      span.recordException(new TypeError(MESSAGE));
      span.setStatus({ code: SpanStatusCode.ERROR });
      span.end();
      expect(attribute((await exported()).attributes, "error.type")).toBe("Explicit <EMAIL_ADDRESS> using <API_KEY>");
    });

    it.each(["TypeError", "ECONNREFUSED", "_OTHER"])("preserves ordinary %s classification", async classification => {
      const span = fiTracer.startSpan("ordinary");
      if (classification === "TypeError") span.recordException(new TypeError(MESSAGE));
      if (classification === "ECONNREFUSED") span.recordException(Object.assign(new Error(MESSAGE), { code: classification }));
      span.setStatus({ code: SpanStatusCode.ERROR, message: MESSAGE });
      span.end();
      expect(attribute((await exported()).attributes, "error.type")).toBe(classification);
    });
  });

  it.each(["default", "explicit false"])("leaves details intact with PII disabled via %s", async source => {
    if (source === "explicit false") process.env.FI_PII_REDACTION = "true";
    const span = tracer(source === "explicit false" ? { piiRedaction: false } : undefined).startSpan("unmasked");
    span.addEvent("exception", { "exception.type": TYPE, "exception.message": MESSAGE });
    span.setStatus({ code: SpanStatusCode.ERROR, message: MESSAGE });
    span.end();
    const wire = await exported(false);
    expect(wire.status.message).toBe(MESSAGE);
    expect(attribute(wire.attributes, "error.type")).toBe(TYPE);
    expect(attribute(wire.events[0].attributes, "exception.message")).toBe(MESSAGE);
  });
});
