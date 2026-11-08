import { describe, expect, it } from "@jest/globals";
import { capBase64Images } from "../trace/trace-config/imageCap";

// Source-first regression controls. No runtime result is claimed by this file.
// Oracles are literal layouts, not a second recognizer or cap-generated output.
const CAP = 64;
const BS = String.fromCharCode(92);
const PAYLOAD = "QUJD".repeat(40);
const hexText = (text: string): string => text.split("").map((c) => BS + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");

const WRAPS: Array<[string, (value: string) => string]> = [
  ["literal", (value) => value],
  ["JSON value", (value) => JSON.stringify({ value, note: "keep" })],
  ["JSON key", (value) => JSON.stringify({ [value]: "keep", note: "retain" })],
  ["nested document", (value) => JSON.stringify({ opaque: JSON.stringify({ value }), other: "retain" })],
];

describe("round-six prefix discovery", () => {
  it("B-001: a rejected hidden prefix cannot conceal a later MIME pair at the one-unit boundary", () => {
    const hidden = BS + "u0064ata:image/";
    const before = hidden + "x ".repeat(120);
    const later = hidden + 'png;name="a' + BS + '" b";base64,' + PAYLOAD;
    const raw = before + later;
    // The literal second MIME header has 35 UTF-16 units, plus 160 payload units.
    expect(capBase64Images(raw, 194)).toBe("[image-redacted]");
    expect(capBase64Images(raw, 195)).toBe("[image-redacted]");
    expect(capBase64Images(raw, 196)).toBe("[image-redacted]");
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(raw), 194)).toBe(wrap("[image-redacted]"));
      expect(capBase64Images(wrap(raw), 195)).toBe(wrap("[image-redacted]"));
    }
  });

  it("A-001: preserves the lead-slice overlap repair with the literal lens fixture", () => {
    const header = "data:image/png stray;base64,";
    const raw = header + header + PAYLOAD;
    expect(capBase64Images(raw, CAP)).toBe("__REDACTED__");
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(raw), CAP)).toBe(wrap("__REDACTED__"));
    }
  });

  it("the precise scanner also revisits a prefix whose first four letters were payload", () => {
    const first = "data:image/png;base64,";
    // The later comma is beyond the supplementary window, so its precise scan
    // must not depend on the supplementary overlap repair to find this image.
    const later = 'data:image/png;name="' + "x".repeat(300) + '";base64,' + PAYLOAD;
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(first + later), CAP)).toBe(wrap("__REDACTED__"));
    }
  });
});

describe("round-six strict document fence", () => {
  it("A-002: retains the exact nested key/value lens fixture", () => {
    const inner = '{"data:image/png;name=":";base64,' + PAYLOAD + '"}';
    const raw = JSON.stringify({ opaque: inner, other: "retain" });
    expect(capBase64Images(raw, CAP)).toBe(raw);
    expect(capBase64Images(raw, 0)).toBe(raw);
  });

  it.each([0, 1, 3])("keeps split tokens apart through document and scalar transports, depth=%i", (depth) => {
    for (const inner of [
      '{"data:image/png;name=":";base64,' + PAYLOAD + '"}',
      '["data:image/png;name=", ";base64,' + PAYLOAD + '"]',
    ]) {
      let raw = inner;
      for (let i = 0; i < depth; i++) raw = JSON.stringify(raw);
      expect(capBase64Images(raw, 0)).toBe(raw);
      const transported = hexText(raw);
      expect(capBase64Images(transported, 0)).toBe(transported);
      const escapedContent = JSON.stringify(raw).slice(1, -1);
      expect(capBase64Images(escapedContent, 0)).toBe(escapedContent);
    }
  });

  it("complete lexical parsing retains duplicate keys, large numeric tokens and surrogate spellings", () => {
    const raw = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"s":"' + BS + 'uD800","data:image/png;name=":";base64,' + PAYLOAD + '" }';
    expect(capBase64Images(raw, 0)).toBe(raw);
    const nested = JSON.stringify({ opaque: raw, other: "retain" });
    expect(capBase64Images(nested, 0)).toBe(nested);
  });

  it("does not certify unfinished, mismatched, trailing-comma or multiple-root documents", () => {
    const stem = '{"data:image/png;name=":";base64,' + PAYLOAD + '"';
    for (const suffix of ["", "]", ",}", "} null"]) {
      expect(capBase64Images(stem + suffix, CAP)).toBe('{"__REDACTED__"' + suffix);
    }
  });

  it("a full header and payload in one token still redact, including a transported token and key", () => {
    const url = "data:image/png stray;base64," + PAYLOAD;
    const raw = JSON.stringify({ value: url, note: "keep" });
    expect(capBase64Images(raw, CAP)).toBe('{"value":"__REDACTED__","note":"keep"}');
    const transported = JSON.stringify({ value: hexText(url), note: "keep" });
    expect(capBase64Images(transported, CAP)).toBe('{"value":"[image-redacted]","note":"keep"}');
    const key = JSON.stringify({ [hexText(url)]: "keep", note: "retain" });
    expect(capBase64Images(key, CAP)).toBe('{"[image-redacted]":"keep","note":"retain"}');
    const nested = JSON.stringify({ opaque: raw, other: "retain" });
    expect(capBase64Images(nested, CAP)).toBe(JSON.stringify({ opaque: '{"value":"__REDACTED__","note":"keep"}', other: "retain" }));
  });
});

describe("round-six Q4 ambiguity precedence", () => {
  it("A-003: a populated ambiguous decoy after stray MIME whitespace is redacted", () => {
    const raw = 'data:image/svg+xml stray;name="a' + BS + '";base64,' + PAYLOAD + '",%3Csvg%3E';
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(raw), CAP)).toBe(wrap('__REDACTED__",%3Csvg%3E'));
    }
  });

  it("B-002: a populated ambiguous decoy after a semicolon-space is redacted", () => {
    const raw = 'data:image/svg+xml; name="a' + BS + '";base64,' + PAYLOAD + '",%3Csvg%3E';
    for (const [, wrap] of WRAPS) {
      expect(capBase64Images(wrap(raw), CAP)).toBe(wrap('__REDACTED__",%3Csvg%3E'));
    }
  });

  it("an ambiguous Unicode quote cannot veto a populated run", () => {
    const raw = 'data:image/svg+xml stray;name="a' + BS + 'u0022;base64,' + PAYLOAD + '",%3Csvg%3E';
    expect(capBase64Images(raw, CAP)).toBe('__REDACTED__",%3Csvg%3E');
  });

  it("preserves an unambiguous literal populated MIME decoy and ordinary text", () => {
    const literal = 'data:image/svg+xml;name="a' + BS + '";base64,' + PAYLOAD + '",%3Csvg%3E';
    const noPair = 'data:image/svg+xml stray;name=";base64,' + PAYLOAD + '",%3Csvg%3E';
    for (const value of [literal, noPair]) {
      for (const [, wrap] of WRAPS) expect(capBase64Images(wrap(value), 0)).toBe(wrap("[image-redacted]"));
    }
    for (const value of ["ordinary base64 words QUJD"]) {
      for (const [, wrap] of WRAPS) expect(capBase64Images(wrap(value), 0)).toBe(wrap(value));
    }
  });
});
