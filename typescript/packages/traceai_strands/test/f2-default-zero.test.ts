import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import { diag } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { NativePrivacy } from "../src/nativePrivacy.js";

// Source-first node:test controls. Runtime NOT_RUN.
// A TS-capable runner is not bound in this packet; no package configuration was read.
const FLAGS = [
  "FI_BASE64_IMAGE_MAX_LENGTH", "FI_PII_REDACTION", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
  "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT",
  "FI_HIDE_INPUT_IMAGES", "FI_HIDE_EMBEDDING_VECTORS", "FI_HIDE_ERROR_DETAILS",
];
const saved = new Map(FLAGS.map(key => [key, process.env[key]]));
beforeEach(() => { for (const key of FLAGS) delete process.env[key]; });
afterEach(() => {
  for (const key of FLAGS) {
    const value = saved.get(key);
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
});
const ordinaryKey = () => false;

for (const setting of [undefined, "0"]) {
  test(`F2 native URI read preserves zero: ${setting ?? "default"}`, () => {
    if (setting !== undefined) process.env.FI_BASE64_IMAGE_MAX_LENGTH = setting;
    const privacy = new NativePrivacy(true);
    assert.equal(privacy.config.base64ImageMaxLength, 0);
    assert.deepEqual(privacy.attributes({ image: "data:image/png;base64,A", keep: "ordinary" }, ordinaryKey),
      { image: "[image-redacted]", keep: "ordinary" });
  });
  test(`F2 native ImageBlock bytes read preserves zero: ${setting ?? "default"}`, () => {
    if (setting !== undefined) process.env.FI_BASE64_IMAGE_MAX_LENGTH = setting;
    const privacy = new NativePrivacy(true);
    const raw = '{"image":{"format":"png","source":{"bytes":"A"}},"keep":"ordinary"}';
    assert.deepEqual(privacy.attributes({ payload: raw }, ordinaryKey),
      { payload: '{"image":{"format":"png","source":{"bytes":"[image-redacted]"}},"keep":"ordinary"}' });
    const empty = '{"image":{"format":"png","source":{"bytes":""}}}';
    assert.deepEqual(privacy.attributes({ payload: empty }, ordinaryKey), { payload: empty });
    assert.equal(raw, '{"image":{"format":"png","source":{"bytes":"A"}},"keep":"ordinary"}');
  });
}

for (const value of [
  "DaTa: \t IMAGE/png;base64,A",
  "%64ata%3Aimage%2Fpng;name=" + "q".repeat(1024) + ";base64%2CA",
  "data:image/png;base64,QUJD QUJD\tQUJD\fQUJD",
  "data&colon;image&sol;png&semi;base64&comma;A",
  "data:image/svg+xml,%3Csvg%3E",
  "data:image/png;name=base64,QUJD",
  "data:image/png;base64,",
  "data: arbitrary IMAGE/png;base64,A",
]) {
  test(`F2 native value uses the core grammar-free scanner: ${value.slice(0, 32)}`, () => {
    const privacy = new NativePrivacy(true);
    assert.deepEqual(privacy.attributes({ payload: value }, ordinaryKey), { payload: "[image-redacted]" });
    assert.deepEqual(privacy.attributes({ payload: JSON.stringify({ image: value, keep: "ordinary" }) }, ordinaryKey),
      { payload: '{"image":"[image-redacted]","keep":"ordinary"}' });
  });
}

test("F2 native event and metadata-retained routes cannot bypass the cap", () => {
  const privacy = new NativePrivacy(true);
  const source = {
    attributes: {},
    events: [{ name: "request", time: [1, 2], attributes: { image: "data:image/png;base64,A", keep: "ordinary" } }],
  } as unknown as ReadableSpan;
  assert.deepEqual(privacy.events(source, true),
    [{ name: "request", time: [1, 2], attributes: { image: "[image-redacted]", keep: "ordinary" } }]);
  assert.deepEqual(privacy.attributes({ image: "data:image/png;base64,A" }, () => true), { image: "[image-redacted]" });
  assert.equal(source.events[0].attributes?.image, "data:image/png;base64,A");
});

test("F2 marker is not re-parsed as malformed native JSON", () => {
  const privacy = new NativePrivacy(true);
  assert.deepEqual(privacy.attributes({ image: "data:image/png;base64,A" }, ordinaryKey), { image: "[image-redacted]" });
  assert.deepEqual(privacy.attributes({ image: "[image-redacted]" }, ordinaryKey), { image: "[image-redacted]" });
});

test("F2 core masking does not short-circuit native error redaction", () => {
  const privacy = new NativePrivacy(false);
  const value = '{"url":"data:image/png;base64,A","tool":{"status":"error","content":"private failure"},"keep":"ordinary"}';
  assert.deepEqual(privacy.attributes({ payload: value }, ordinaryKey),
    { payload: '{"url":"[image-redacted]","tool":{"status":"error","content":"__REDACTED__"},"keep":"ordinary"}' });
});

for (const setting of ["-1", "NaN", "0.5", "abc", "1junk", "Infinity", "", "   ", "1e3", "0x10", "9007199254740992"]) {
  test(`F2 native initializer invalid cap ${setting} fails closed without throwing`, (t) => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = setting;
    const warn = t.mock.method(diag, "warn", () => undefined);
    const privacy = new NativePrivacy(true);
    assert.equal(privacy.config.base64ImageMaxLength, 0);
    assert.deepEqual(privacy.attributes({ image: "data:image/png;base64,A" }, ordinaryKey), { image: "[image-redacted]" });
    assert.deepEqual(privacy.attributes({ payload: '{"image":{"source":{"bytes":"A"}}}' }, ordinaryKey),
      { payload: '{"image":{"source":{"bytes":"[image-redacted]"}}}' });
    assert.deepEqual(warn.mock.calls.map(call => call.arguments), [["Invalid base64 image cap; using 0."]]);
  });
}

for (const setting of ["0", " 0 ", "000"]) {
  test(`F2 native parsed zero ${setting} is valid and warning-free`, (t) => {
    process.env.FI_BASE64_IMAGE_MAX_LENGTH = setting;
    const warn = t.mock.method(diag, "warn", () => undefined);
    const privacy = new NativePrivacy(true);
    assert.equal(privacy.config.base64ImageMaxLength, 0);
    assert.deepEqual(privacy.attributes({ image: "data:image/png;base64,A" }, ordinaryKey), { image: "[image-redacted]" });
    assert.deepEqual(warn.mock.calls, []);
  });
}

test("F2 native Q1 fragments and the split-token pair stay separate", () => {
  const privacy = new NativePrivacy(true);
  // Canonical JSON only: the native path re-serializes parsed payloads.
  for (const payload of [
    '{"data:":"image/png;base64,A"}',
    '{"data:image/png;name=":";base64,QUJD"}',
    '["data:image/png;name=",";base64,QUJD"]',
  ]) assert.deepEqual(privacy.attributes({ payload }, ordinaryKey), { payload });
});

test("F2 native r2 trigger needs a comma after data: and image/", () => {
  const privacy = new NativePrivacy(true);
  for (const image of ["data:image/", "data:image/png;name=", "data:image/png;base64"]) {
    assert.deepEqual(privacy.attributes({ image }, ordinaryKey), { image });
  }
  assert.deepEqual(privacy.attributes({ image: "data:image/png;base64," }, ordinaryKey), { image: "[image-redacted]" });
});

test("F2 native positive caps retain their previous markers and boundary", () => {
  process.env.FI_BASE64_IMAGE_MAX_LENGTH = "64";
  const privacy = new NativePrivacy(true);
  assert.deepEqual(privacy.attributes({ image: "data:image/png;base64,A" }, ordinaryKey), { image: "data:image/png;base64,A" });
  assert.deepEqual(privacy.attributes({ image: "data:image/png;base64," + "A".repeat(80) }, ordinaryKey), { image: "__REDACTED__" });
  const small = '{"image":{"format":"png","source":{"bytes":"A"}}}';
  assert.deepEqual(privacy.attributes({ payload: small }, ordinaryKey), { payload: small });
});

test("F2 native non-image strings and remote images remain byte-identical", () => {
  const privacy = new NativePrivacy(true);
  const values = {
    ordinary: "ordinary words", audio: "data:audio/wav;base64,QUJD",
    pdf: "data:application/pdf;base64,QUJD", remote: "https://example.invalid/a.png",
  };
  assert.deepEqual(privacy.attributes(values, ordinaryKey), values);
});

// TS-F2-R6 A-WHITESPACE-PARITY. Runtime NOT_RUN. nativePrivacy.ts has no own cap-0
// trigger: at zero, strings use the core seam (:73-76), so these pin inheritance.
const SPLIT_NATIVE: Array<[string, string]> = [
  ["TAB inside data:", "DATA\t:IMAGE/png;base64,A"],
  ["LF inside data:", "da\nta:image/png;base64,A"],
  ["CR LF inside image/", "data:ima\r\nge/png;base64,A"],
  ["SPACE inside data: and image/", "da ta:ima ge/png;base64,A"],
];
for (const [label, value] of SPLIT_NATIVE) {
  test(`F2 native value inherits the core ASCII whitespace parity: ${label}`, () => {
    const privacy = new NativePrivacy(true);
    assert.deepEqual(privacy.attributes({ payload: value }, ordinaryKey), { payload: "[image-redacted]" });
    assert.deepEqual(privacy.attributes({ payload: JSON.stringify({ image: value, keep: "ordinary" }) }, ordinaryKey),
      { payload: '{"image":"[image-redacted]","keep":"ordinary"}' });
  });
}

test("F2 native whitespace-split non-image and cross-token controls stay byte-identical", () => {
  const privacy = new NativePrivacy(true);
  for (const payload of ["DA\tTA:AUDIO/wav;base64,QUJD", JSON.stringify(["da\tta:image/", ",A"])]) {
    assert.deepEqual(privacy.attributes({ payload }, ordinaryKey), { payload });
  }
});
