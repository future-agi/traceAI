import { discoverSecrets, indexPrivacyHints, isCredentialName, mediaReplacement, scrubAttribute, scrubValue } from "../privacy-runtime";

test("S6 core walking uses data properties without invoking user getters or conversions", () => {
  const execute = jest.fn(() => { throw new Error("user code executed"); });
  const value = Object.defineProperty({ safe: "ordinary words", unsupported: { toString: execute, toJSON: execute } }, "api_key", { enumerable: true, get: execute });
  const clean = scrubValue(value);
  expect(clean.safe).toBe("ordinary words"); expect(clean.api_key).toBeUndefined(); expect(execute).not.toHaveBeenCalled();
  const cycle: any = { safe: "ordinary words" }; cycle.self = cycle;
  expect(scrubValue(cycle)).toEqual({ safe: "ordinary words", self: "<omitted>" });
});
test.each(["depth", "nodes", "text"])("S6 core %s budget is fail-closed, not silently partial", kind => {
  let value: any = kind === "nodes" ? Array(16385).fill(0) : kind === "text" ? "x".repeat(4194305) : "ordinary";
  if (kind === "depth") for (let i = 0; i < 34; i++) value = [value];
  expect(() => scrubValue(value)).toThrow(/Privacy (structure|text) budget/);
});
test("S1 keeps JSON primitive scalar recognition through SDK serialization", () => {
  expect(scrubAttribute(JSON.stringify("iVBORw0KGgoAAAANSUhEUg=="))).toBe(JSON.stringify("<omitted: 24 base64 chars>"));
  expect(scrubAttribute(JSON.stringify('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">payload</svg>'))).toBe(JSON.stringify("<omitted: data URI>"));
  expect(scrubAttribute("12345678", ["12345678"])).toBe("<redacted>");
});
test("S6 full path exclusions never exempt independent credential ancestry", () => {
  // A flattened name has no provider provenance (RL 2299t). Only the explicit
  // provider-root walker may exempt token names; independent ancestry stays private.
  expect(isCredentialName("choices.0.logprobs.content.0.token")).toBe(true);
  expect(isCredentialName("secret.choices.0.logprobs.content.0.token")).toBe(true);
  expect(isCredentialName("gen_ai.usage.input_tokens_details.cache_read")).toBe(false);
  expect(isCredentialName("provider_client_secret_tokens")).toBe(true);
  expect(discoverSecrets([{ secret: { input_tokens: ["credential-123456"] } }])).toContain("credential-123456");
});
test("S6 duplicate query hints and metadata boundaries use the same media predicate", () => {
  const hints = indexPrivacyHints([["parts[0].mimeType[0]", "audio/pcm"], ["parts.0.mimeType", ["text/plain"]]]);
  expect(mediaReplacement("AQIDBA==", "parts.0.content.chunk", hints)).toBe("<omitted: 8 base64 chars>");
  expect(mediaReplacement("AQIDBA==", "parts.1.content.chunk", hints)).toBeUndefined();
  expect(mediaReplacement("spoken words", "parts.0.transcript.content", hints)).toBeUndefined();
  expect(scrubAttribute("https://example.invalid/?content=AQIDBA==&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor")).toBe("https://example.invalid/?content=<omitted: 8 base64 chars>&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor");
});
