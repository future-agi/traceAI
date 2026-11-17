import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { privacyFilter } from "../export-privacy";
import { indexPrivacyHints, mediaReplacement, scrubAttribute, scrubValue } from "../privacy-runtime";
import { REDACTED_VALUE } from "../trace/trace-config/constants";

const flags = ["FI_HIDE_ERROR_DETAILS", "FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_OUTPUT_TEXT"] as const;
const aliases = ["mastra.processor_run.input", "input.value"] as const;
const providerError = "SYNTHETIC_PROVIDER_ERROR";
const png = "iVBORw0KGgoAAAANSUhEUg==";
const savedEnvironment = { ...process.env };

beforeEach(() => {
  for (const key of Object.keys(process.env)) {
    if (key.startsWith("FI_HIDE_") || key === "FI_PII_REDACTION") delete process.env[key];
  }
});
afterEach(() => { process.env = { ...savedEnvironment }; });

function input(error: unknown = providerError) {
  return { error, messages: [{ role: "user", content: "SYNTHETIC_PROMPT", error: "ordinary nested error words" }], retry: 2 };
}
function span(payload = input(), spanType = "processor_run"): ReadableSpan {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  const values = Object.fromEntries(aliases.map(key => [key, JSON.stringify(payload)]));
  return {
    _spanContext: ctx, spanContext: () => ctx,
    // Do not depend on an ambient provider's SDK value limit.
    _attributeValueLengthLimit: Infinity,
    name: "processor error", kind: 0, startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true,
    attributes: { "mastra.span.type": spanType, ...values },
    status: { code: 2, message: providerError },
    events: [{ name: "processor event", time: [1, 1], attributes: { ...values } }], links: [],
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    resource: resourceFromAttributes({}), instrumentationScope: { name: "g1-marker-test" },
  } as unknown as ReadableSpan;
}

// #567's real provider-error contract, driven through the shared S1 boundary.
// S2a writes REDACTED_VALUE, then recursively mistakes that marker for base64
// MP3 magic and exports "<omitted: 12 base64 chars>" inside each JSON input.
describe.each(flags)("%s marker precedence", flag => {
  test.each([providerError, png])("G1-HIDDEN-PROCESSOR-ERROR (%s)", error => {
    process.env[flag] = "true";
    const payload = input(error), native = span(payload), before = JSON.stringify(native);
    const filter = privacyFilter(), clean = filter.span(native);
    expect(clean.name).toBe("processor error"); // A fail-closed empty span is not a pass.
    expect(clean.status).toEqual({ code: 2 });
    expect(clean.events).toHaveLength(1);
    const expected = { ...payload, error: REDACTED_VALUE };
    for (const surface of [clean.attributes, clean.events[0].attributes!]) {
      for (const key of aliases) {
        const serialized = surface[key];
        expect(typeof serialized).toBe("string");
        expect(JSON.parse(serialized as string)).toEqual(expected);
        expect(serialized).not.toMatch(/<omitted|base64|chars|bytes|length|encoding/i);
        expect(serialized).not.toContain(error);
      }
    }
    expect(JSON.stringify(native)).toBe(before);
    const again = filter.span(clean);
    for (const key of aliases) expect(again.attributes[key]).toBe(clean.attributes[key]);
    expect(again.events[0].attributes).toEqual(clean.events[0].attributes);
  });

  test("G1-EXPLICIT-FALSE retains safe provider error without relaxing media rules", () => {
    process.env[flag] = "true";
    process.env.FI_HIDE_ERROR_DETAILS = "false";
    const native = span(), clean = privacyFilter().span(native);
    for (const key of aliases) expect(JSON.parse(clean.attributes[key] as string)).toEqual(input());
    expect(clean.status.message).toBe(providerError);
    // PENDING I1xF2-PRECEDENCE T71-T74: TS I1 media omission dominates positive caps.
    // Default zero uses the
    // F2 marker; the explicit positive-cap twin retains the original I1 marker.
    const mediaInput = span(input(png)), before = JSON.stringify(mediaInput);
    for (const [filter, expected] of [
      [privacyFilter(), "[image-redacted]"],
      [privacyFilter({ base64ImageMaxLength: png.length }), "<omitted: 24 base64 chars>"],
    ] as const) {
      const media = filter.span(mediaInput);
      expect(media.name).toBe("processor error");
      expect(media.status.message).toBe(providerError);
      expect(media.events).toHaveLength(1);
      for (const surface of [media.attributes, media.events[0].attributes!]) {
        for (const key of aliases) {
          expect(JSON.parse(surface[key] as string).error).toBe(expected);
          expect(JSON.parse(surface[key] as string)).toEqual({ ...input(png), error: expected });
        }
      }
      expect(JSON.stringify({ attributes: media.attributes, events: media.events, status: media.status })).not.toContain(png);
    }
    expect(JSON.stringify(mediaInput)).toBe(before);
  });
});

test("G1-ERROR-SCOPE does not hide ordinary inputs or nonstring error members", () => {
  process.env.FI_HIDE_ERROR_DETAILS = "true";
  for (const payload of [input(null), input({ message: "ordinary error words" }), input(42)]) {
    const clean = privacyFilter().span(span(payload));
    for (const key of aliases) expect(JSON.parse(clean.attributes[key] as string)).toEqual(payload);
  }
  const clean = privacyFilter().span(span(input(), "agent_run"));
  for (const key of aliases) expect(JSON.parse(clean.attributes[key] as string)).toEqual(input());
});

test("G1-NESTED-SENTINEL survives recursive JSON, arrays and lost media context", () => {
  expect(REDACTED_VALUE).toBe("__REDACTED__");
  const payload = { error: REDACTED_VALUE, nested: [{ error: REDACTED_VALUE }], safe: "ordinary words" };
  expect(scrubValue(payload)).toEqual(payload);
  expect(JSON.parse(scrubAttribute(JSON.stringify(payload)))).toEqual(payload);
  expect(JSON.parse(scrubAttribute(JSON.stringify(JSON.stringify(payload))))).toBe(JSON.stringify(payload));
  const hints = indexPrivacyHints([["part.mime_type", "audio/pcm"]]);
  expect(mediaReplacement(REDACTED_VALUE, "part.data", hints, true, false, false, true)).toBe(REDACTED_VALUE);
  expect(scrubValue({ audio: [REDACTED_VALUE] })).toEqual({ audio: [REDACTED_VALUE] });
});

test("G1-NO-PREFIX-EXEMPTION keeps actual media and credentials masked", () => {
  const longer = `${REDACTED_VALUE}AAAA`;
  expect(mediaReplacement(longer)).toBe("<omitted: 16 base64 chars>");
  expect(mediaReplacement(png)).toBe("<omitted: 24 base64 chars>");
  expect(scrubValue({ error: png, password: "synthetic-g1-private", safe: "ordinary words" })).toEqual({
    error: "<omitted: 24 base64 chars>", password: "<redacted>", safe: "ordinary words",
  });
  // Preserving a marker must not create a credential-name exception.
  expect(scrubValue({ password: REDACTED_VALUE })).toEqual({ password: "<redacted>" });
  expect(scrubAttribute(`before ${REDACTED_VALUE} synthetic-g1-private after`, ["synthetic-g1-private"])).not.toContain("synthetic-g1-private");
});
