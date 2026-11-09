import { describe, expect, it } from "@jest/globals";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { REDACTED_VALUE as MARK } from "../trace/trace-config/constants";

const MAX = 256;
const PREFIX = "data:image/png;base64,";
const SLASH = String.fromCharCode(92);
const BIG = PREFIX + "QUJD".repeat(1024) + "DECODECANARY";
// Split UTF-16 units, not code points: lone and paired surrogates must both survive.
const hexText = (text: string) => text.split("").map((c) => SLASH + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");
const slashText = (text: string) => text.replace(/\//g, SLASH + "/");
const delayedOrdinary = (layers: number) => SLASH + "u005c".repeat(layers) + "u0078ordinary " + (SLASH + "/").repeat(4096);

const SPELLINGS: Array<[string, (text: string) => string]> = [
  ["simple slash", slashText],
  ["hex lower", hexText],
  ["hex upper digits", (text) => hexText(text).replace(/[a-f]/g, (c) => c.toUpperCase())],
  ["alternating raw and escaped units", (text) => text.split("").map((c, i) => i % 2 ? c : hexText(c)).join("")],
];

describe("escaped image decode controls", () => {
  it.each(SPELLINGS)("decoded cap units and exact original boundaries: %s", (_label, encode) => {
    const url = PREFIX + "/A+_-=B".repeat(700);
    const encoded = encode(url);
    const before = "prefix " + SLASH + "t before ";
    const after = " caption " + SLASH + "u0061";
    const raw = before + encoded + after;
    expect(capBase64Images(raw, url.length + 1)).toBe("[image-redacted]");
    expect(capBase64Images(raw, url.length)).toBe("[image-redacted]");
    expect(capBase64Images(raw, url.length - 1)).toBe("[image-redacted]");
    const document = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"url":"' + encoded + '","s":"' + SLASH + 'uD800" }';
    expect(capBase64Images(document, url.length)).toBe(document);
    expect(capBase64Images(document, url.length - 1)).toBe(document.replace(encoded, MARK));
  });

  it.each([4095, 4096, 4097, 8191, 8192, 8193])("numeric conversion blocks do not change offsets, decoded length=%i", (length) => {
    const url = PREFIX + "/".repeat(length - PREFIX.length);
    const raw = hexText(url);
    expect(capBase64Images(raw, length)).toBe("[image-redacted]");
    expect(capBase64Images(raw, length - 1)).toBe("[image-redacted]");
    // Decode a quote immediately after a full/partial block without ending the token.
    const document = '{"url":"' + raw + SLASH + '\" caption","note":"keep"}';
    expect(capBase64Images(document, length - 1)).toBe(document.replace(raw, MARK));
  });

  it.each(['"', SLASH, "/", "b", "f", "n", "r", "t"])("simple escape %s before and after an image preserves raw neighbors", (code) => {
    const neighbor = SLASH + code;
    const raw = "keep " + neighbor + " | " + hexText(BIG) + " | " + neighbor + " keep";
    expect(capBase64Images(raw, MAX)).toBe("[image-redacted]");
    expect(capBase64Images(raw, BIG.length)).toBe("[image-redacted]");
  });

  it.each(["u00g0", "uG000", "u0G00", "u000G", "u00", "u", "q", ""])("invalid escape %s is retained outside selected image ranges", (tail) => {
    const malformed = SLASH + tail;
    const encoded = hexText(BIG);
    const raw = "before " + malformed + " | " + encoded + " | " + malformed;
    expect(capBase64Images(raw, MAX)).toBe("[image-redacted]");
    const ordinary = "ordinary " + "x".repeat(8192) + malformed;
    expect(capBase64Images(ordinary, MAX)).toBe(ordinary);
  });

  it.each(["u00g0", "u00", "q", ""])("damaged declared token stays fail-closed with dense valid escapes: %s", (tail) => {
    const stem = '{"note":"retain","data":"';
    const content = hexText("QUJD".repeat(2048)) + SLASH + tail;
    const raw = stem + content + (tail ? '","media_type":"image/png"}' : '');
    // Unterminated trailing backslash uses an earlier declaration.
    const declared = tail ? raw : '{"media_type":"image/png",' + raw.slice(1);
    expect(capBase64Images(declared, MAX)).toBe(declared.replace(content, MARK));
  });

  it("escaped UTF-16 headers and metadata retain lone and paired surrogate units", () => {
    const units = String.fromCharCode(0xd800, 0x61, 0xd83d, 0xde00, 0xdc00, 0);
    const url = 'data:image/png;name="' + units + '";base64,' + "A".repeat(8192);
    const encoded = hexText(url);
    const raw = '{ "s":"' + hexText(units) + '","url":"' + encoded + '" }';
    expect(capBase64Images(raw, url.length)).toBe(raw);
    expect(capBase64Images(raw, url.length - 1)).toBe(raw.replace(encoded, MARK));
  });

  it("internal CR/LF count, trailing CR/LF and captions stay outside the edit", () => {
    const url = PREFIX + ("/A\r\n".repeat(2048)) + "/";
    const encoded = hexText(url);
    const suffix = SLASH + "r" + SLASH + "n caption";
    const raw = "before " + encoded + suffix;
    expect(capBase64Images(raw, url.length)).toBe("[image-redacted]");
    expect(capBase64Images(raw, url.length - 1)).toBe("[image-redacted]");
  });

  it("escaped declarations protect named payloads but not metadata or array children", () => {
    const data = hexText("QUJD".repeat(2048));
    const raw = '{"' + hexText("source") + '":{"' + hexText("data") + '":"' + data + '"},"type":"' + hexText("image") + '","name":"' + data + '","other":[{"data":"' + data + '"}]}';
    const expected = raw.replace('":"' + data + '"}', '":"' + MARK + '"}');
    expect(capBase64Images(raw, MAX)).toBe(expected);
    const metadata = '{"type":"' + hexText("image") + '","name":"' + hexText("QUJD") + '","data":"' + hexText("QUJD") + '"}';
    expect(capBase64Images(metadata, 4)).toBe(metadata);
    expect(capBase64Images(metadata, 0)).toBe(metadata.replace('"data":"' + hexText("QUJD") + '"', '"data":"[image-redacted]"'));
  });

  it("many edits through nested flat escape indexes preserve lossless outer tokens", () => {
    const urls = Array.from({ length: 16 }, (_, i) => i % 2 ? hexText(BIG) : slashText(BIG));
    let raw = '{ "n":1e400,"same":1,"same":2,"urls":["' + urls.join('","') + '"],"s":"' + SLASH + 'uD800" }';
    let expected = '{ "n":1e400,"same":1,"same":2,"urls":' + JSON.stringify(urls.map(() => MARK)) + ',"s":"' + SLASH + 'uD800" }';
    for (let i = 0; i < 3; i++) { raw = JSON.stringify(raw); expected = JSON.stringify(expected); }
    expect(capBase64Images(raw, MAX)).toBe(expected);
  });

  it("unterminated undeclared URL token preserves its damaged outer shape", () => {
    const encoded = hexText(BIG);
    const raw = '{"note":"keep","url":"' + encoded;
    expect(capBase64Images(raw, MAX)).toBe("[image-redacted]");
  });

  it("sensitivity: dense-escape ordinary exhaustion still replaces the entire scope", () => {
    const ordinary = delayedOrdinary(512);
    expect(ordinary).not.toContain("data:image/");
    expect(capBase64Images(ordinary, MAX)).toBe("[image-redacted]");
    const exact = PREFIX + "A".repeat(MAX - PREFIX.length);
    const raw = JSON.stringify({ opaque: ordinary, ordinary: delayedOrdinary(2), exact });
    expect(capBase64Images(raw, MAX)).toBe(JSON.stringify({ opaque: "[image-redacted]", ordinary: delayedOrdinary(2), exact }));
  });

  it("fallback unions prior mapped edits and covers pending escaped descendants", () => {
    const inner = JSON.stringify({ first: delayedOrdinary(512), pending: BIG, last: slashText(BIG) });
    const raw = JSON.stringify({ opaque: inner, note: "keep", shallow: BIG });
    expect(capBase64Images(raw, MAX)).toBe(JSON.stringify({ opaque: JSON.stringify({ first: "[image-redacted]", pending: MARK, last: "[image-redacted]" }), note: "keep", shallow: MARK }));
  });

  it("affordable ordinary escapes and escape-free tokens remain byte-identical", () => {
    for (const value of [delayedOrdinary(2), "ordinary " + "x".repeat(65536), "ordinary " + SLASH + "t" + "x".repeat(65536)]) {
      expect(capBase64Images(value, MAX)).toBe(value);
    }
    const value = JSON.stringify({ a: "", b: "short", c: "x".repeat(65536), d: "/".repeat(4096) });
    expect(capBase64Images(value, MAX)).toBe(value);
  });
});
