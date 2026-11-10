import { indexPrivacyHints, mediaReplacement } from "../privacy-runtime";
import { carryGenkitAliases, isGenkitMappedAlias, rememberGenkitAliases } from "../genkit-content-policy";
import { privacyFilter } from "../export-privacy";

const counts = ["llm.token_count.prompt_details.audio", "llm.token_count.completion_details.audio", "gen_ai.usage.cache_read_tokens.audio"];

describe("exact typed audio usage metadata", () => {
  test.each(counts)("keeps valid %s counts but not invalid payloads or hinted media", key => {
    for (const value of [0, 1, 730000001]) expect(mediaReplacement(value, key)).toBeUndefined();
    for (const [value, expected] of [
      ["SYNTHETIC CONTENT !", "<omitted: media>"], ["730000001", "<omitted: 9 base64 chars>"],
      [-1, "<omitted: media>"], [0.5, "<omitted: media>"], [true, "<omitted: media>"], [[1, 2], "<omitted: media>"],
    ] as const) {
      expect(mediaReplacement(value, key)).toBe(expected);
    }
    const parent = key.slice(0, key.lastIndexOf("."));
    const hints = indexPrivacyHints([[parent + ".mime_type", "audio/wav"]]);
    expect(mediaReplacement(42, key, hints)).toBe("<omitted: media>");
    expect(mediaReplacement(42, key + ".0.content")).toBe("<omitted: media>");
    expect(mediaReplacement(42, key, undefined, true, false, true)).toBe("<omitted: media>");
    // Numeric metadata remains subject to exact secret replacement.
    expect(privacyFilter({}, ["730000001"]).attributes({ [key]: 730000001 })[key]).toBe("<redacted>");
  });
});

describe("value-bound native aliases", () => {
  it("does not trust an unregistered map, mutated value or weaker mapper policy", () => {
    const attributes = { "output.value": "masked response" };
    expect(isGenkitMappedAlias(attributes, "output.value", attributes["output.value"], { hideOutputText: true })).toBe(false);
    rememberGenkitAliases(attributes, { hideOutputText: true }, ["output.value"]);
    expect(isGenkitMappedAlias(attributes, "output.value", attributes["output.value"], { hideOutputText: true })).toBe(true);
    expect(isGenkitMappedAlias(attributes, "output.value", "changed response", { hideOutputText: true })).toBe(false);
    expect(isGenkitMappedAlias(attributes, "output.value", attributes["output.value"], { hideOutputs: true })).toBe(false);
    const copy = { "output.value": "credential-scrubbed response" };
    carryGenkitAliases(attributes, copy);
    expect(isGenkitMappedAlias(copy, "output.value", copy["output.value"], { hideOutputText: true })).toBe(true);
  });
});
