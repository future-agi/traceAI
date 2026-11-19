import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

const SCHEME_URLS = [
  "wss://example.test",
  "ws://example.test",
  "redis://example.test",
  "postgres://example.test",
  "s3://example.test",
  "gs://example.test",
  "mongodb+srv://example.test",
  "file:///tmp/report.txt",
  "WSS://example.test",
  "a1+v.-x://example.test",
] as const;
const MP3 = "//uQxAAAAAAA";
// Mutable tuples: jest test.each rejects readonly (as const) rows under ts-jest type checking.
const MEDIA: Array<[name: string, payload: string, marker: string]> = [
  ["MP3 frame sync", MP3, "<omitted: 12 base64 chars>"],
  ["JPEG", "/9j/4AAQSkZJRgABAQ==", "<omitted: 20 base64 chars>"],
  ["PNG", "iVBORw0KGgoAAAANSUhEUg==", "<omitted: 24 base64 chars>"],
  ["WAV", "UklGRiQAAABXQVZFZm10IA==", "<omitted: 24 base64 chars>"],
];

describe("scheme URLs are not bare base64 media", () => {
  test.each(SCHEME_URLS)("preserves %s in free text and framed strings", url => {
    for (const value of [url, `Connect to ${url} now.`, `(${url})`]) {
      expect(scrubText(value)).toBe(value);
      expect(scrubAttribute(value)).toBe(value);
      expect(scrubAttribute(JSON.stringify(value))).toBe(JSON.stringify(value));
    }
  });

  test.each(SCHEME_URLS)("preserves %s in prompt, output and attribute text", url => {
    const value = {
      "input.value": `Connect to ${url} now.`,
      "output.value": `The endpoint is ${url}.`,
      "attribute.text": `Endpoint (${url}) is ready.`,
    };
    expect(scrubValue(value)).toEqual(value);
    expect(scrubAttribute(JSON.stringify(value))).toBe(JSON.stringify(value));
  });

  test("handles multiple scheme URLs in one prose gap", () => {
    const value = SCHEME_URLS.join(" ; ");
    expect(scrubText(value)).toBe(value);
    expect(scrubAttribute(value)).toBe(value);
  });

  test("does not change HTTP(S) URL handling", () => {
    const value = "Use https://example.test and http://example.test now.";
    expect(scrubText(value)).toBe(value);
    expect(scrubAttribute(value)).toBe(value);
  });
});

describe("scheme recognition never exempts independent media or credentials", () => {
  test.each(MEDIA)("still omits %s at the start and after prose or a URL", (_name, payload, marker) => {
    for (const prefix of ["", "payload ", "audio: ", "audio:\t", "wss://example.test "]) {
      expect(scrubText(prefix + payload)).toBe(prefix + marker);
      expect(scrubAttribute(prefix + payload)).toBe(prefix + marker);
      expect(scrubAttribute(JSON.stringify(prefix + payload))).toBe(JSON.stringify(prefix + marker));
    }
  });

  test.each(["1ws", "+ws", "-ws", ".ws", "123", ""])(
    "does not accept a non-letter-leading or empty scheme %s", prefix => {
      const value = `${prefix}:${MP3}`;
      const expected = `${prefix}:<omitted: 12 base64 chars>`;
      expect(scrubText(value)).toBe(expected);
      expect(scrubAttribute(value)).toBe(expected);
    },
  );

  test.each(MEDIA.slice(1))("still omits %s directly after an audio label", (_name, payload, marker) => {
    expect(scrubText(`audio:${payload}`)).toBe(`audio:${marker}`);
    expect(scrubAttribute(`audio:${payload}`)).toBe(`audio:${marker}`);
  });

  test.each(MEDIA)("still omits %s in HTTP(S) query values", (_name, payload, marker) => {
    for (const scheme of ["http", "https"]) {
      const base = `${scheme}://example.test/?blob=`;
      const value = base + encodeURIComponent(payload) + "&model=keep#anchor";
      const expected = base + marker + "&model=keep#anchor";
      expect(scrubText(value)).toBe(expected);
      expect(scrubAttribute(value)).toBe(expected);
    }
  });

  test.each([
    ["audio/mpeg", MP3, "<omitted: 12 base64 chars>"],
    ["image/png", "iVBORw0KGgoAAAANSUhEUg==", "<omitted: 24 base64 chars>"],
  ])("still omits data: %s payloads in prose and URL queries/fragments", (mime, payload, marker) => {
    const uri = `data:${mime};base64,${payload}`;
    expect(scrubText(`payload ${uri} end`)).toBe(`payload ${marker} end`);
    expect(scrubAttribute(uri)).toBe(marker);
    for (const base of [
      "https://example.test/#",
      "https://example.test/?blob=",
      "wss://example.test/#",
      "wss://example.test/?blob=",
    ]) {
      expect(scrubText(base + uri)).toBe(base + marker);
      expect(scrubAttribute(base + uri)).toBe(base + marker);
    }
  });

  test("still omits non-base64 data URIs", () => {
    const value = "payload data:image/svg+xml,%3Csvg%3Epayload%3C/svg%3E end";
    expect(scrubText(value)).toBe("payload <omitted: data URI> end");
    expect(scrubAttribute(value)).toBe("payload <omitted: data URI> end");
  });

  test.each(["?", "#"])("does not exempt an independent MP3 run after URL delimiter %s", delimiter => {
    const base = `wss://example.test/${delimiter}`;
    expect(scrubText(base + MP3)).toBe(base + "<omitted: 12 base64 chars>");
    expect(scrubAttribute(base + MP3)).toBe(base + "<omitted: 12 base64 chars>");
  });

  test("retains the duplicate-query MIME control from privacy.test.ts", () => {
    const value = "https://example.invalid/?content=AQIDBA==&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor";
    const expected = "https://example.invalid/?content=<omitted: 8 base64 chars>&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor";
    expect(scrubText(value)).toBe(expected);
    expect(scrubAttribute(value)).toBe(expected);
  });

  test("keeps known-secret, credential-key and media-context masking", () => {
    const secret = "registered-authority-fixture";
    const value = `wss://example.test/${secret}`;
    expect(scrubText(value, [secret])).toBe("wss://example.test/<redacted>");
    expect(scrubAttribute(value, [secret])).toBe("wss://example.test/<redacted>");
    expect(scrubText("password=wss://example.test")).toBe('password="<redacted>"');
    expect(scrubAttribute("password=wss://example.test")).toBe('password="<redacted>"');
    expect(scrubValue({ api_key: "wss://example.test" })).toEqual({ api_key: "<redacted>" });
    expect(scrubValue({ mime_type: "audio/pcm", data: "wss://example.test" }))
      .toEqual({ mime_type: "audio/pcm", data: "<omitted: media>" });
  });
});
