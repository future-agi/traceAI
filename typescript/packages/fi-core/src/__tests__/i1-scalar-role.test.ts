import type { Attributes } from "@opentelemetry/api";
import { DROPPED_CONTENT_COUNT, projectAttributes } from "../attribute-projection";
import { privacyFilter } from "../export-privacy";
import { indexPrivacyHints, mediaReplacement, rememberProviderResponse, scrubValue } from "../privacy-runtime";
import { registerSecret } from "../secrets";

const detailKeys = [
  "gen_ai.usage.cache_read_tokens", "gen_ai.usage.input_tokens.audio",
  "gen_ai.usage.output_tokens.audio", "gen_ai.usage.output_tokens.reasoning",
];
const coreKeys = ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens"];
const contentKeys = ["input.value", "input.mime_type", "gen_ai.request.parameters", "gen_ai.input.messages", "raw.input",
  "output.value", "output.mime_type", "gen_ai.output.messages", "raw.output"];
function completion(): Attributes {
  return {
    "fi.span.kind": "LLM", "gen_ai.response.id": "scalar-role-response", "gen_ai.response.finish_reasons": ["stop"],
    "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": "fixture-model", "gen_ai.response.model": "fixture-model",
    "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108,
    "gen_ai.usage.cache_read_tokens": 0, "gen_ai.usage.input_tokens.audio": 0, "gen_ai.usage.output_tokens.audio": 0, "gen_ai.usage.output_tokens.reasoning": 0,
    "input.value": "hello", "input.mime_type": "text/plain", "gen_ai.request.parameters": "{}", "gen_ai.input.messages": "[]", "raw.input": "hello",
    "output.value": "answer", "output.mime_type": "text/plain", "gen_ai.output.messages": "[]", "raw.output": "answer",
  };
}

test("I1-G4 exact counters in their declared metric containers survive as scalars", () => {
  for (const key of ["gen_ai.audio.bytes", ...coreKeys, ...detailKeys]) {
    for (const value of [0, 9, "0", "9"]) {
      expect(mediaReplacement(value, key)).toBeUndefined();
      expect(scrubValue(value, [], key)).toBe(value);
      expect(projectAttributes({ [key]: value }, 16).attributes[key]).toBe(value);
    }
  }
  const nested = { gen_ai: { usage: { input_tokens: { audio: 9 } } } };
  expect(scrubValue(nested)).toEqual(nested);
  expect(scrubValue({ audio: { usage: { output_tokens: 7 }, voice: "alloy", sample_rate: 16000 } })).toEqual({ audio: { usage: { output_tokens: 7 }, voice: "alloy", sample_rate: 16000 } });
});

test("I1-G4 media scalars have no blanket number or boolean exemption", () => {
  for (const path of ["audio", "audio.samples.0", "audio.data.audio.source.0", "gen_ai.audio.bytes.extra", "other.gen_ai.audio.bytes"]) {
    for (const value of [7, true, false]) {
      expect(scrubValue(value, [], path)).toBe("<omitted: media>");
      expect(projectAttributes({ [path]: value }, 16).attributes[path]).toBe("<omitted: media>");
    }
  }
  expect(scrubValue(7, [], "audio.data.audio.source.0")).toBe("<omitted: media>");
  expect(scrubValue(7, [], "parts.0.bytes.0", 0, false, undefined, undefined, true, false, true)).toBe("<omitted: media>");
});

test("I1-G4 arrays never receive the scalar metric role", () => {
  for (const path of ["audio", "audio.data.samples", "gen_ai.audio.bytes", "gen_ai.usage.input_tokens.audio"]) {
    for (const value of [[1, 2, 3], [true, false]]) {
      expect(mediaReplacement(value, path)).toBe("<omitted: media>");
      expect(scrubValue(value, [], path)).toBe("<omitted: media>");
      expect(projectAttributes({ [path]: value }, 16).attributes[path]).toBe("<omitted: media>");
    }
  }
  // Existing S6 structure contract: string/object arrays are walked, not exempted.
  expect(scrubValue(["AQIDBA=="], [], "audio")).toEqual(["<omitted: 8 base64 chars>"]);
  expect(scrubValue([], [], "audio")).toEqual([]);
});

test("I1-G4 an allowlisted spelling inside media payload ancestry is still payload", () => {
  for (const key of ["gen_ai.audio.bytes", ...coreKeys, ...detailKeys]) {
    const path = `audio.data.wrapper.${key}`;
    for (const value of [7, true, "7"]) {
      expect(mediaReplacement(value, path)).toBe(typeof value === "string" ? "<omitted: 1 base64 chars>" : "<omitted: media>");
      expect(scrubValue({ audio: { data: { wrapper: { [key]: value } } } })).toEqual({ audio: { data: { wrapper: { [key]: typeof value === "string" ? "<omitted: 1 base64 chars>" : "<omitted: media>" } } } });
    }
  }
  // Ordinary usage metadata remains a boundary; it cannot launder a counter leaf.
  expect(scrubValue({ audio: { data: { usage: { content: "spoken words", total_tokens: 7 } } } })).toEqual({ audio: { data: { usage: { content: "spoken words", total_tokens: "<omitted: media>" } } } });
});

test("I1-G4 media MIME/type and inherited media beat an exact metric key", () => {
  for (const key of ["gen_ai.audio.bytes", "gen_ai.usage.input_tokens.audio", "gen_ai.usage.total_tokens"]) {
    for (const hint of [{ mime_type: "audio/pcm" }, { type: "audio" }]) {
      const hints = indexPrivacyHints(Object.entries(hint));
      expect(mediaReplacement(7, key, hints)).toBe("<omitted: media>");
      expect(projectAttributes({ ...hint, [key]: 7 }, 16).attributes[key]).toBe("<omitted: media>");
    }
    expect(mediaReplacement(7, key, undefined, true, true)).toBe("<omitted: media>");
    expect(mediaReplacement(7, key, undefined, true, false, true)).toBe("<omitted: media>");
  }
});

test("I1-G4 credential ancestry and exact known numeric/string secrets win", () => {
  const secret = "87004321";
  for (const key of ["gen_ai.audio.bytes", "gen_ai.usage.input_tokens.audio", "gen_ai.usage.total_tokens"]) {
    for (const value of [Number(secret), secret]) {
      expect(scrubValue(value, [secret], key)).toBe("<redacted>");
      expect(scrubValue(value, [secret], `audio.data.${key}`)).toBe("<redacted>");
      expect(scrubValue(value, [], `secret.${key}`)).toBe("<redacted>");
    }
  }
  expect(scrubValue('{"gen_ai.audio.bytes":87004321}', [secret])).toBe('{"gen_ai.audio.bytes":"<redacted>"}');
  registerSecret(secret); // Synthetic, distinct from the canonical usage fixture values.
  const attrs = { "gen_ai.audio.bytes": Number(secret), "gen_ai.usage.input_tokens.audio": secret, "gen_ai.usage.total_tokens": 108 };
  expect(privacyFilter().attributes(attrs)).toEqual({ "gen_ai.audio.bytes": "<redacted>", "gen_ai.usage.input_tokens.audio": "<redacted>", "gen_ai.usage.total_tokens": 108 });
});

// Open findings: these are required, unskipped controls, not claimed GREEN.
// The source-only packet records the owner/scope boundary for both repairs.
test("I1-G4 encoded metric cannot regain a role across a payload usage boundary", () => {
  const encoded = JSON.stringify({ "gen_ai.audio.bytes": 73 });
  expect(encoded).toContain(":73}");
  const clean = scrubValue({ audio: { data: { usage: encoded } } });
  expect(clean.audio.data.usage).not.toContain(":73}");
  expect(clean.audio.data.usage).toContain("<omitted:");
  expect(clean.audio.data.usage).toBe('{"gen_ai.audio.bytes":"<omitted: media>"}');
  for (const key of ["gen_ai.audio.bytes", "gen_ai.usage.total_tokens"]) {
    for (const source of [JSON.stringify({ [key]: 73 }), JSON.stringify(JSON.stringify({ [key]: 73 }))]) {
      const value = { audio: { data: { usage: source } } };
      const result = scrubValue(value);
      expect(JSON.stringify(result)).not.toContain(":73}");
      expect(scrubValue(result)).toEqual(result);
    }
  }
  expect(scrubValue({ audio: { usage: '{"gen_ai.usage.total_tokens":73}' } })).toEqual({ audio: { usage: '{"gen_ai.usage.total_tokens":73}' } });
  expect(scrubValue({ audio: { data: { usage: '{"content":"spoken words","gen_ai.usage.total_tokens":73}' } } })).toEqual({ audio: { data: { usage: '{"content":"spoken words","gen_ai.usage.total_tokens":"<omitted: media>"}' } } });
});

test("I1-G4 registered numeric secret beats media hints at the export boundary", () => {
  const secret = "87004329";
  registerSecret(secret);
  const clean = privacyFilter().attributes({ mime_type: "audio/pcm", "gen_ai.audio.bytes": Number(secret) });
  expect(clean["gen_ai.audio.bytes"]).toBe("<redacted>");
});

test("I1-G4 metric strings retain independent data URI and magic-byte checks", () => {
  expect(mediaReplacement("data:audio/pcm;base64,AQIDBA==", "gen_ai.audio.bytes")).toBe("<omitted: 8 base64 chars>");
  expect(mediaReplacement("iVBORw0KGgoAAAANSUhEUg==", "gen_ai.usage.input_tokens.audio")).toBe("<omitted: 24 base64 chars>");
  expect(mediaReplacement("9", "gen_ai.usage.input_tokens.audio.extra")).toBeUndefined();
  expect(mediaReplacement(9, "GEN_AI.audio.bytes")).toBe("<omitted: media>");
});

test("I1-A7 canonical pair outranks raw output with minimal known-zero deferral", () => {
  const attrs = completion(), before = JSON.stringify(attrs);
  const projected = projectAttributes(attrs, 16), kept = projected.attributes;
  expect(Object.keys(attrs)).toHaveLength(23);
  expect(Object.keys(kept)).toHaveLength(16);
  expect(kept).toMatchObject({ "fi.span.kind": "LLM", "gen_ai.response.id": "scalar-role-response", "output.value": "answer", "output.mime_type": "text/plain", "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108, [DROPPED_CONTENT_COUNT]: 7 });
  expect(kept["raw.output"]).toBeUndefined();
  expect(kept[detailKeys[3]]).toBeUndefined();
  for (const key of detailKeys.slice(0, 3)) expect(kept[key]).toBe(0);
  expect(projected.dropped).toBe(8);
  expect(projectAttributes(Object.fromEntries(Object.entries(attrs).reverse()), 16)).toEqual(projected);
  expect(projectAttributes(kept, 16)).toEqual({ attributes: kept, dropped: 0 });
  expect(projectAttributes(attrs, 128)).toEqual({ attributes: attrs, dropped: 0 });
  expect(JSON.stringify(attrs)).toBe(before);
});

test("I1-F1 nonzero, string-zero and unknown usage are never sacrificed for output", () => {
  for (const value of [1, "0"]) {
    const attrs = completion();
    for (const key of detailKeys) attrs[key] = value;
    const small = projectAttributes(attrs, 16).attributes;
    for (const key of detailKeys) expect(small[key]).toBe(value);
    for (const key of coreKeys) expect(small[key]).toBe(attrs[key]);
    expect(small["output.value"]).toBeUndefined();
    expect(small["output.mime_type"]).toBeUndefined();
    expect(projectAttributes(attrs, 17).attributes["output.value"]).toBe("answer");
  }
  const attrs = { ...completion(), "gen_ai.usage.custom_detail": 0 };
  expect(projectAttributes(attrs, 16).attributes).toMatchObject({ "gen_ai.usage.custom_detail": 0, "output.value": "answer" });
});

test("I1-A7 absent producer details consume no selector slots", () => {
  const attrs = completion();
  for (const key of detailKeys) attrs[key] = undefined;
  const kept = projectAttributes(attrs, 16);
  expect(kept.attributes).toMatchObject({ "output.value": "answer", "output.mime_type": "text/plain", "gen_ai.usage.input_tokens": 101, "gen_ai.usage.total_tokens": 108 });
  for (const key of detailKeys) expect(Object.prototype.hasOwnProperty.call(kept.attributes, key)).toBe(false);
  expect(projectAttributes({ "gen_ai.usage.custom_detail": undefined }, 16)).toEqual({ attributes: {}, dropped: 0 });
  for (const key of detailKeys) attrs[key] = "undefined";
  const literal = projectAttributes(attrs, 16).attributes;
  for (const key of detailKeys) expect(literal[key]).toBe("undefined");
  expect(literal["output.value"]).toBeUndefined();
});

test("I1-A7 bound provider admission must not manufacture undefined detail strings", () => {
  const attrs = completion();
  const output = '{"choices":[{"message":{"content":"answer"}}],"usage":{"prompt_tokens":101,"completion_tokens":7,"total_tokens":108}}';
  attrs["output.value"] = output; attrs["raw.output"] = output; attrs["output.mime_type"] = "application/json";
  for (const key of detailKeys) attrs[key] = undefined;
  const context = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  rememberProviderResponse(context, attrs, "openai.chat");
  const admitted = privacyFilter().admissionAttributes(attrs, context);
  for (const key of detailKeys) expect(Object.prototype.hasOwnProperty.call(admitted, key)).toBe(false);
  const kept = projectAttributes(admitted, 16).attributes;
  expect(kept["output.mime_type"]).toBe("application/json");
  expect(JSON.parse(String(kept["output.value"])).choices[0].message.content).toBe("answer");
  expect(kept["gen_ai.usage.input_tokens"]).toBe(101);
  expect(kept["gen_ai.usage.total_tokens"]).toBe(108);
});

test("I1-A7 drop markers count omitted content, not deferred zero metadata", () => {
  const attrs = { ...completion(), [DROPPED_CONTENT_COUNT]: 5 };
  for (const limit of [0, 1, 10, 12, 13, 14, 15, 16, 17, 22, 23, 24, 128]) {
    const result = projectAttributes(attrs, limit), kept = result.attributes;
    expect(Object.keys(kept).length).toBeLessThanOrEqual(limit);
    expect("output.value" in kept).toBe("output.mime_type" in kept);
    expect(result.dropped).toBe(23 - Object.keys(kept).filter(key => key !== DROPPED_CONTENT_COUNT).length);
    if (DROPPED_CONTENT_COUNT in kept) expect(kept[DROPPED_CONTENT_COUNT]).toBe(5 + contentKeys.filter(key => !(key in kept)).length);
  }
});
