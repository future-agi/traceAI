import { describe, expect, it } from "@jest/globals";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { redactPiiInString, redactPiiInValue } from "../trace/trace-config/piiRedaction";

// Synthetic fixtures only. Isolated values must work without unrelated PII
// accidentally opening the admission guard. Authored before the source overlay.
const PHONES = ["5551234567", "15551234567", "+15551234567", "(555)1234567", "1(555)1234567", "+1(555)1234567"];
const ORDINARY = ["", "ordinary text", "0", "42", "123456", "123456789", "5551234567x", "5551234567_", "version 12.34.56", "count 1234"];
const CLASSES: Array<[string, string]> = [
  ["test@example.test", "<EMAIL_ADDRESS>"],
  ["123-45-6789", "<SSN>"],
  ["4111 1111 1111 1111", "<CREDIT_CARD>"],
  ["192.168.1.1", "<IP_ADDRESS>"],
  [["sk", "test", "ABCDEFGHIJKLMNOPQRSTUVWXYZ".slice(0, 24)].join("_"), "<API_KEY>"], // synthetic test value
  ["pk-live-ABCDEFGHIJKLMNOPQRSTUVWX", "<API_KEY>"],
  ["555-123-4567", "<PHONE_NUMBER>"],
];

describe("PII admission is a conservative superset", () => {
  it.each(PHONES)("isolated supported phone %s", (phone) => {
    expect(redactPiiInString(phone)).toBe("<PHONE_NUMBER>");
    expect(redactPiiInString("call " + phone + " now")).toBe("call <PHONE_NUMBER> now");
    expect(redactPiiInString(phone)).toBe("<PHONE_NUMBER>");
  });

  it.each(ORDINARY)("retains nonmatching numeric/ordinary text %s", (text) => {
    expect(redactPiiInString(text)).toBe(text);
  });

  it.each(CLASSES)("admits each retained class in isolation: %s", (text, expected) => {
    expect(redactPiiInString(text)).toBe(expected);
  });

  it("keeps existing pattern precedence, not just phone admission", () => {
    const raw = "123-45-6789@example.test | 123-45-6789 | 4111 1111 1111 1111 | 192.168.1.1 | " + ["sk", "test", "ABCDEFGHIJKLMNOPQRSTUVWXYZ".slice(0, 24)].join("_") + " | 5551234567"; // synthetic test value
    expect(redactPiiInString(raw)).toBe("<EMAIL_ADDRESS> | <SSN> | <CREDIT_CARD> | <IP_ADDRESS> | <API_KEY> | <PHONE_NUMBER>");
  });

  it("maps arrays independently without mutating caller values", () => {
    const object = { phone: "5551234567" };
    const input = Object.freeze(["5551234567", "15551234567", "(555)1234567", "123456789", "ordinary", 42, true, object, null]);
    expect(redactPiiInValue(input)).toEqual(["<PHONE_NUMBER>", "<PHONE_NUMBER>", "<PHONE_NUMBER>", "123456789", "ordinary", 42, true, object, null]);
    expect(input).toEqual(["5551234567", "15551234567", "(555)1234567", "123456789", "ordinary", 42, true, object, null]);
    expect(redactPiiInValue(object)).toBe(object);
    expect(redactPiiInValue(5551234567)).toBe(5551234567);
    expect(redactPiiInValue(undefined)).toBeUndefined();
    expect(redactPiiInValue(null)).toBeNull();
  });

  it.each([16, 1024, 8192])("retains growing numeric nonmatches around a supported phone, repeats=%i", (count) => {
    const prefix = "123456789 | ".repeat(count);
    expect(redactPiiInString(prefix + "5551234567 | ordinary")).toBe(prefix + "<PHONE_NUMBER> | ordinary");
    expect(redactPiiInString(prefix + "5551234567x")).toBe(prefix + "5551234567x");
  });
});

describe("phone admission reaches actual completed SDK exports", () => {
  it.each([false, true])("PII enabled=%s preserves explicit opt-in behavior", (piiRedaction) => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new FITracer({
      tracer: provider.getTracer("phone-admission"),
      traceConfig: {
        piiRedaction, base64ImageMaxLength: 256, hideErrorDetails: false,
        hideInputs: false, hideOutputs: false, hideInputText: false, hideOutputText: false,
        hideInputImages: false, hideInputMessages: false, hideOutputMessages: false,
        hideEmbeddingVectors: false,
      },
    });
    for (const phone of PHONES) {
      exporter.reset();
      const attributes = Object.freeze({ created: phone, ordinary: "123456789", attempts: 42 });
      const event = Object.freeze({ phone, ordinary: "keep" });
      const span = tracer.startSpan("case", { attributes });
      span.setAttribute("single", phone);
      span.setAttributes({ batch: phone, phones: [phone, "123456789"] });
      span.addEvent("request", event, [1, 2]);
      span.end();
      const results = exporter.getFinishedSpans();
      expect(results).toHaveLength(1);
      const expected = piiRedaction ? "<PHONE_NUMBER>" : phone;
      expect(results[0].attributes.created).toBe(expected);
      expect(results[0].attributes.single).toBe(expected);
      expect(results[0].attributes.batch).toBe(expected);
      expect(results[0].attributes.phones).toEqual([expected, "123456789"]);
      expect(results[0].attributes.ordinary).toBe("123456789");
      expect(results[0].attributes.attempts).toBe(42);
      expect(results[0].events[0].attributes).toEqual({ phone: expected, ordinary: "keep" });
      expect(results[0].events[0].time).toEqual([1, 2]);
      expect(attributes).toEqual({ created: phone, ordinary: "123456789", attempts: 42 });
      expect(event).toEqual({ phone, ordinary: "keep" });
    }
  });
});
