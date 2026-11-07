import { describe, expect, it } from "@jest/globals";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { REDACTED_VALUE as MARK } from "../trace/trace-config/constants";

const MAX = 256;
const PREFIX = "data:image/png;base64,";
const PAYLOAD = "QUJD".repeat(512) + "RESOURCECANARY";
const BIG = PREFIX + PAYLOAD;
const SLASH = String.fromCharCode(92);
const at = (length: number) => PREFIX + "A".repeat(length - PREFIX.length);
// Each decode removes only five characters, unlike exponentially growing quoted
// wrappers. These are real escaped provider strings, not a timing-only fixture.
const delayed = (layers: number, tail: string) => SLASH + "u005c".repeat(layers) + tail;
const hiddenImage = (layers: number) => delayed(layers, "u0064ata:image/png;base64," + PAYLOAD);
const ordinary = (layers: number) => delayed(layers, "u0078ordinary text " + "z".repeat(2048));
const escaped = (text: string) => Array.from(text, (c) => SLASH + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")).join("");

describe("bounded image-cap work", () => {
  it("sensitivity: exhausted ordinary encoding is not returned uninspected", () => {
    const value = ordinary(256);
    expect(value).not.toContain("data:image/");
    expect(capBase64Images(value, MAX)).toBe("[image-redacted]");
    // The unlimited baseline returns value: this assertion detects a missing bound.
  });

  it("positive: affordable encoded ordinary text remains byte-identical", () => {
    for (const value of [ordinary(2), "ordinary " + "x".repeat(65536), SLASH + "q" + "safe ".repeat(1000)]) {
      expect(capBase64Images(value, MAX)).toBe(value);
    }
  });

  it.each([128, 256, 512])("exhaustion covers hidden payload and suffix, layers=%i", (layers) => {
    const value = hiddenImage(layers) + " ordinary suffix " + BIG;
    expect(capBase64Images(value, MAX)).toBe("[image-redacted]");
    const raw = '{"opaque":' + JSON.stringify(value) + ',"note":"keep","exact":' + JSON.stringify(at(MAX)) + '}';
    const expected = '{"opaque":"[image-redacted]","note":"keep","exact":' + JSON.stringify(at(MAX)) + '}';
    expect(capBase64Images(raw, MAX)).toBe(expected);
  });

  it("independent outer budgets retain long ordinary and exact-image siblings", () => {
    const raw = JSON.stringify({ before: ordinary(2), expensive: ordinary(256), after: ordinary(2), url: at(MAX), type: "image", id: "QUJD" });
    const expected = JSON.stringify({ before: ordinary(2), expensive: "[image-redacted]", after: ordinary(2), url: at(MAX), type: "image", id: "QUJD" });
    expect(capBase64Images(raw, MAX)).toBe(expected);
  });

  it("a fallback subsumes already mapped edits and every pending encoded child", () => {
    // The inner worklist visits last first, making an image edit before exhausting
    // on first. The fallback must union that edit, not expose its remaining suffix.
    const inner = JSON.stringify({ first: hiddenImage(256), pending: BIG, last: BIG });
    const raw = JSON.stringify({ opaque: inner, ordinary: "retain", shallow: BIG });
    expect(capBase64Images(raw, MAX)).toBe(JSON.stringify({ opaque: JSON.stringify({ first: "[image-redacted]", pending: MARK, last: MARK }), ordinary: "retain", shallow: MARK }));
  });

  it("unterminated outer token exhaustion includes the entire damaged suffix", () => {
    const content = JSON.stringify(hiddenImage(256) + " suffix " + BIG).slice(1, -1);
    const raw = '{"ordinary":"retain","opaque":"' + content;
    expect(capBase64Images(raw, MAX)).toBe("[image-redacted]");
  });

  it.each([1, 4, 9])("affordable nested encoded JSON preserves original tokens, depth=%i", (depth) => {
    let raw = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"s":"' + SLASH + 'uD800","data":"' + PAYLOAD + '","media_type":"image/png" }';
    let expected = raw.replace(PAYLOAD, MARK);
    for (let i = 0; i < depth; i++) { raw = JSON.stringify(raw); expected = JSON.stringify(expected); }
    expect(capBase64Images(raw, MAX)).toBe(expected);
  });

  it("large escaped payloads use decoded cap units with precise original offsets", () => {
    const payload = "QUJD".repeat(4096);
    const encoded = escaped(payload);
    const raw = '{"note":"' + SLASH + 'u0061' + SLASH + '/b","data":"' + encoded + '","media_type":"image/png"}';
    expect(capBase64Images(raw, payload.length)).toBe(raw);
    expect(capBase64Images(raw, payload.length + 1)).toBe(raw);
    expect(capBase64Images(raw, payload.length - 1)).toBe(raw.replace(encoded, MARK));
  });

  it("many edits through large escape indexes remain lossless", () => {
    const encoded = escaped(PREFIX + "QUJD".repeat(410) + "RESOURCECANARY" + "AAA");
    let raw = '["' + Array(48).fill(encoded).join('","') + '"]';
    let expected = JSON.stringify(Array(48).fill(MARK));
    for (let i = 0; i < 3; i++) { raw = JSON.stringify(raw); expected = JSON.stringify(expected); }
    expect(capBase64Images(raw, MAX)).toBe(expected);
  });

  it.each([6000, 65536])("deep malformed arrays still inspect populated leaves, depth=%i", (depth) => {
    const raw = "[".repeat(depth) + JSON.stringify(BIG) + ',{"data":' + JSON.stringify(PAYLOAD) + ',"type":"image","name":"keep"}';
    const expected = "[".repeat(depth) + JSON.stringify(MARK) + ',{"data":' + JSON.stringify(MARK) + ',"type":"image","name":"keep"}';
    expect(capBase64Images(raw, MAX)).toBe(expected);
    const empty = "[".repeat(depth);
    expect(capBase64Images(empty, MAX)).toBe(empty);
  });

  it("deep objects, mismatched closers and source declarations keep lexical semantics", () => {
    const raw = '{"source":{"data":' + JSON.stringify(PAYLOAD) + '},"deep":' + "{".repeat(6000) + '"url":' + JSON.stringify(BIG) + "]".repeat(6000) + ',"type":"image"}';
    expect(capBase64Images(raw, MAX)).toBe(raw.replace(PAYLOAD, MARK).replace(BIG, MARK));
  });

  it("compressed arrays do not invent image inheritance", () => {
    const raw = JSON.stringify({ type: "image", source: [[[{ data: PAYLOAD }]]], url: BIG });
    expect(capBase64Images(raw, MAX)).toBe(JSON.stringify({ type: "image", source: [[[{ data: PAYLOAD }]]], url: MARK }));
  });

  it("many short tokens still permit a late declaration and over-cap payload", () => {
    const raw = '{"ordinary":[' + '"a",'.repeat(20000) + '"a"],"data":' + JSON.stringify(PAYLOAD) + ',"type":"image"}';
    expect(capBase64Images(raw, MAX)).toBe(raw.replace(PAYLOAD, MARK));
  });

  it("long quoted headers and wrapped payloads have no truncation budget", () => {
    const wrapped = (PAYLOAD.match(/.{1,16}/g) ?? []).join("\r\n");
    const url = 'data:image/png;name="' + "a".repeat(65536) + '";base64,' + wrapped;
    const raw = "prefix " + url + " ordinary caption";
    expect(capBase64Images(raw, MAX)).toBe("prefix " + MARK + " ordinary caption");
    expect(capBase64Images(raw, url.length)).toBe(raw);
    expect(capBase64Images(raw, url.length + 1)).toBe(raw);
  });

  it("array attributes isolate exhausted elements without mutating callers", () => {
    const raw = [ordinary(256), at(MAX), ordinary(2), BIG];
    const before = [...raw];
    expect(capBase64Images(raw, MAX)).toEqual(["[image-redacted]", at(MAX), ordinary(2), MARK]);
    expect(raw).toEqual(before);
  });
});
