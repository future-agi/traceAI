import { describe, expect, it } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import {
  redactPiiInString,
  redactPiiInValue,
} from "../trace/trace-config/piiRedaction";

// Reference only for bounded compatibility inputs, never for resource-size text.
const LEGACY_EMAIL = /\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b/g;
const EMAIL = "<EMAIL_ADDRESS>";
const REGEX_FAILURE = "Email matching must not re-enter the backtracking regex";

/**
 * Observe the public implementation, not a duplicate scanner. Intercept the old
 * regex before exec so the RED itself cannot spend quadratic CPU. Other PII
 * regexes remain real. Reads, at-sign search volume and retained slices expose
 * the private scanner's work without exporting instrumentation or using timers.
 */
function measuredScan(
  text: string,
  scan: (value: string) => string = redactPiiInString,
) {
  const nativeExec = RegExp.prototype.exec;
  const nativeCode = String.prototype.charCodeAt;
  const nativeIndex = String.prototype.indexOf;
  const nativeSlice = String.prototype.slice;
  let reads = 0;
  let searchUnits = 0;
  let searches = 0;
  let sliceUnits = 0;
  let nextSearchFloor = 0;
  // Plain scoped wrappers avoid keeping a mock-call record per scanned unit.
  // These tests are synchronous, not concurrent; restore even on the RED throw.
  try {
    RegExp.prototype.exec = function (this: RegExp, value: string) {
      if (this.source.includes("@")) throw new Error(REGEX_FAILURE);
      return nativeExec.call(this, value);
    };
    String.prototype.charCodeAt = function (this: string, index: number) {
      if (this === text) reads++;
      return nativeCode.call(this, index);
    };
    String.prototype.indexOf = function (this: string, search: string, position?: number) {
      const found = nativeIndex.call(this, search, position);
      if (this === text && search === "@") {
        const from = position ?? 0;
        if (from < nextSearchFloor || from > text.length) {
          throw new Error("At-sign search revisited an already searched prefix");
        }
        searches++;
        searchUnits += (found < 0 ? text.length : found + 1) - from;
        nextSearchFloor = found < 0 ? text.length : found + 1;
      }
      return found;
    };
    String.prototype.slice = function (this: string, start?: number, end?: number) {
      const result = nativeSlice.call(this, start, end);
      if (this === text) sliceUnits += result.length;
      return result;
    };
    const output = scan(text);
    return { output, reads, searchUnits, searches, sliceUnits };
  } finally {
    String.prototype.slice = nativeSlice;
    String.prototype.indexOf = nativeIndex;
    String.prototype.charCodeAt = nativeCode;
    RegExp.prototype.exec = nativeExec;
  }
}

const LITERAL_CASES: Array<[string, string]> = [
  ["Contact john@example.com for info", "Contact <EMAIL_ADDRESS> for info"],
  ["From alice@test.org to bob@test.org", "From <EMAIL_ADDRESS> to <EMAIL_ADDRESS>"],
  [".alice@example.com", ".<EMAIL_ADDRESS>"],
  ["+-_%alice@example.com", "+-<EMAIL_ADDRESS>"],
  ["A._%+-9@B--..TeSt.", "<EMAIL_ADDRESS>."],
  ["a@.co", "a@.co"],
  ["a@..co", "<EMAIL_ADDRESS>"],
  ["a@b.c", "a@b.c"],
  ["a@b.co1", "a@b.co1"],
  ["a@b.co_", "a@b.co_"],
  ["a@b.co-7", "<EMAIL_ADDRESS>-7"],
  ["a@b.co.foo_", "<EMAIL_ADDRESS>.foo_"],
  ["a@b.co.9xy", "<EMAIL_ADDRESS>.9xy"],
  ["a@b.cde9.fg", "<EMAIL_ADDRESS>"],
  ["a@b.co.co", "<EMAIL_ADDRESS>"],
  ["a@b.co@c.de", "<EMAIL_ADDRESS>@c.de"],
  ["a@b.co.x@y.zz", "<EMAIL_ADDRESS><EMAIL_ADDRESS>"],
  ["a@b.co.foo_@c.de", "<EMAIL_ADDRESS><EMAIL_ADDRESS>"],
  ["a@b.co+b@d.ef", "<EMAIL_ADDRESS><EMAIL_ADDRESS>"],
  ["a@b.co!b@d.ef", "<EMAIL_ADDRESS>!<EMAIL_ADDRESS>"],
  ["éa@b.coé", "é<EMAIL_ADDRESS>é"],
  ["a@b.éé", "a@b.éé"],
  ["a.@@", "a.@@"],
  ["@@@ ordinary .prose", "@@@ ordinary .prose"],
  ["prefix user@bad_thing; good@site.test suffix", "prefix user@bad_thing; <EMAIL_ADDRESS> suffix"],
];

// Every large fixture has an at-sign. Literal layout expectations forbid both
// skip-all-PII and whole-input replacement. There is no time-based assertion.
const RESOURCE_SHAPES: Array<[string, (n: number) => [string, string]]> = [
  ["repeated local boundaries, no domain", (n) => {
    const value = "a.".repeat(n) + "@";
    return [value, value];
  }],
  ["domain dots without a two-letter TLD", (n) => {
    const value = "a@" + "b.".repeat(n) + "x";
    return [value, value];
  }],
  ["long TLD rejected at a word suffix", (n) => {
    const value = "a@b." + "c".repeat(n) + "_";
    return [value, value];
  }],
  ["dense failed at-signs", (n) => {
    const value = "a.@".repeat(n);
    return [value, value];
  }],
  ["unbounded valid local part", (n) => ["a.".repeat(n) + "z@example.test", EMAIL]],
  ["late valid domain suffix", (n) => ["a@" + "b.".repeat(n) + "test", EMAIL]],
  ["many accepted addresses", (n) => ["a@b.co!".repeat(n), "<EMAIL_ADDRESS>!".repeat(n)]],
  ["failed prefix, real PII and retained suffix", (n) => {
    const prefix = "a.".repeat(n) + "@ | ";
    const suffix = " | ordinary " + "z.".repeat(n) + "@";
    return [prefix + "user@example.test" + suffix, prefix + EMAIL + suffix];
  }],
];

describe("email matching has bounded candidate work", () => {
  it("the structural guard is sensitive to the populated legacy failure", () => {
    expect(() => measuredScan("a.a.a.@", (value) => value.replace(LEGACY_EMAIL, EMAIL)))
      .toThrow(REGEX_FAILURE);
    expect("a.a.a.@".replace(LEGACY_EMAIL, EMAIL)).toBe("a.a.a.@");
  });

  it.each(RESOURCE_SHAPES)("%s", (_label, build) => {
    for (const size of [16, 64, 1024, 32768]) {
      const [input, expected] = build(size);
      const observed = measuredScan(input);
      expect(observed.output).toBe(expected);
      expect(observed.reads).toBeGreaterThan(0);
      expect(observed.searches).toBeGreaterThan(0);
      expect(observed.reads).toBeLessThanOrEqual(8 * input.length + 32);
      expect(observed.searchUnits).toBeLessThanOrEqual(input.length);
      expect(observed.sliceUnits).toBeLessThanOrEqual(input.length);
    }
  });

  it.each(LITERAL_CASES)("preserves legacy literal result for %s", (input, expected) => {
    expect(redactPiiInString(input)).toBe(expected);
    expect(redactPiiInString(input)).toBe(expected);
  });

  it("keeps original UTF-16 neighbors, including unmatched surrogate units", () => {
    const left = String.fromCharCode(0xd800);
    const right = String.fromCharCode(0xdc00);
    expect(redactPiiInString(left + "a@b.co" + right)).toBe(left + EMAIL + right);
  });

  it.each([63, 64, 65])("does not invent a local-part length cap at %i", (length) => {
    const local = "a".repeat(length);
    expect(redactPiiInString(local + "@example.test")).toBe(EMAIL);
    const invalid = local + "@example.t";
    expect(redactPiiInString(invalid)).toBe(invalid);
  });

  it.each([254, 255, 256])("does not invent a whole-address length cap at %i", (length) => {
    const value = "a".repeat(length - "@b.co".length) + "@b.co";
    expect(value.length).toBe(length);
    expect(redactPiiInString(value)).toBe(EMAIL);
    expect(redactPiiInString(value + "_")).toBe(value + "_");
  });

  it("retains empty, ordinary and no-at-sign dotted text byte-for-byte", () => {
    for (const value of ["", "ordinary text", "a.".repeat(32768)]) {
      expect(redactPiiInString(value)).toBe(value);
    }
  });

  it("matches the original grammar on a bounded short-input cross product", () => {
    const locals = ["", "a", "a.", "._a", "%-", "_", "éa"];
    const domains = ["b.co", "..co", ".co", "b.c", "b.co_", "b.co1", "b.co.foo_", "b.c0.xy", ""];
    for (const prefix of ["", "x!", "é", " "]) {
      for (const local of locals) {
        for (const domain of domains) {
          for (const suffix of ["", "_", "é", "@c.de", ".x@y.zz"]) {
            const value = prefix + local + "@" + domain + suffix;
            // These bounded strings contain no SSN/card/key/IP/phone candidate.
            const expected = value.replace(LEGACY_EMAIL, EMAIL);
            expect(redactPiiInString(value)).toBe(expected);
          }
        }
      }
    }
  });

  it("retains email-first ordering and all other PII replacements", () => {
    const input = "123-45-6789@x.test | 123-45-6789 | 4111 1111 1111 1111 | 192.168.1.1 | 555-123-4567 | " + ["sk", "live", "A".repeat(24)].join("_"); // synthetic test value (fi-security RLIT-TRACEAI-R15-3888B65C-GH013-STRIPE-FIXTURES-r1-FISEC)
    expect(measuredScan(input).output).toBe(
      "<EMAIL_ADDRESS> | <SSN> | <CREDIT_CARD> | <IP_ADDRESS> | <PHONE_NUMBER> | <API_KEY>",
    );
  });

  it("preserves shallow array and non-string value behavior without mutation", () => {
    const object = { email: "a@b.co" };
    const values = Object.freeze(["a.@", "a@b.co", "ordinary", 42, true, object]);
    expect(redactPiiInValue(values)).toEqual(["a.@", EMAIL, "ordinary", 42, true, object]);
    expect(values).toEqual(["a.@", "a@b.co", "ordinary", 42, true, object]);
    expect(redactPiiInValue(object)).toBe(object);
    expect(redactPiiInValue(42)).toBe(42);
    expect(redactPiiInValue(null)).toBeNull();
    expect(redactPiiInValue(undefined)).toBeUndefined();
    expect(redactPiiInValue("a@b.co")).toBe(EMAIL);
  });
});

describe("email resource controls reach actual SDK exports", () => {
  it.each([false, true])("PII enabled=%s, with cap boundaries and ordinary controls", (piiRedaction) => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const tracer = new FITracer({
      tracer: provider.getTracer("email-resource-controls"),
      traceConfig: { piiRedaction, hideErrorDetails: false, base64ImageMaxLength: 256 },
    });
    const malformed = "a.".repeat(2048) + "@";
    const input = malformed + " | contact good@example.test | ordinary";
    const redacted = malformed + " | contact <EMAIL_ADDRESS> | ordinary";
    const header = "data:image/png;base64,";
    const exactImage = header + "A".repeat(256 - header.length);
    const fixtures: Array<[string, string]> = [
      ["ordinary a.a.@ text", "ordinary a.a.@ text"],
      [malformed, malformed],
      [input, piiRedaction ? redacted : input],
      // PENDING I1xF2-PRECEDENCE T36-T37: TS I1 media omission dominates positive caps.
      // LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1.
      [exactImage, "<omitted: 234 base64 chars>"],
      [exactImage + "A", "__REDACTED__"],
    ];
    for (const [raw, expected] of fixtures) {
      exporter.reset();
      const attributes = Object.freeze({ "input.value": raw, labels: [raw, "keep"] });
      const eventAttributes = Object.freeze({ value: raw, attempt: 2 });
      const error = new Error(raw);
      error.stack = "Error: " + raw + "\n    at synthetic";
      const stack = error.stack;
      const span = tracer.startSpan("case", { attributes });
      span.setAttribute("later", raw);
      span.setAttributes({ bulk: raw });
      span.addEvent("request", eventAttributes, [1, 2]);
      span.recordException(error, [2, 3]);
      span.setStatus({ code: SpanStatusCode.ERROR, message: raw });
      span.end();
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      const result = spans[0];
      expect(result.attributes["input.value"]).toBe(expected);
      expect(result.attributes.labels).toEqual([expected, "keep"]);
      expect(result.attributes.later).toBe(expected);
      expect(result.attributes.bulk).toBe(expected);
      expect(result.events.find((event) => event.name === "request")?.attributes)
        .toEqual({ value: expected, attempt: 2 });
      expect(result.events.find((event) => event.name === "request")?.time).toEqual([1, 2]);
      const exceptions = result.events.filter((event) => event.name === "exception");
      expect(exceptions).toHaveLength(1);
      expect(exceptions[0].attributes?.["exception.message"]).toBe(expected);
      expect(exceptions[0].attributes?.["exception.stacktrace"])
        .toBe(raw === exactImage ? "[image-redacted]" : "Error: " + expected + "\n    at synthetic");
      expect(exceptions[0].attributes?.["exception.type"]).toBe("Error");
      expect(exceptions[0].time).toEqual([2, 3]);
      expect(result.attributes["error.type"]).toBe("Error");
      expect(result.status).toEqual({ code: SpanStatusCode.ERROR, message: expected });
      if (raw === exactImage || raw === exactImage + "A") {
        expect(JSON.stringify({ attributes: result.attributes, events: result.events, status: result.status }))
          .not.toContain(exactImage.slice(header.length));
      }
      expect(attributes).toEqual({ "input.value": raw, labels: [raw, "keep"] });
      expect(eventAttributes).toEqual({ value: raw, attempt: 2 });
      expect(error.message).toBe(raw);
      expect(error.stack).toBe(stack);
    }
  });
});
