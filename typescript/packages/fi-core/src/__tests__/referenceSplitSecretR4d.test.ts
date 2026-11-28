import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A secret that holds a pair separator and what follows it is replaced before the pair rule reads the text, as the reference replaces it:
// the pair rule does not read a value that the secret would split. Each expected text is the reference output for the same input.
const none = indexPrivacyHints([]);
const run = (value: any, secrets: string[], framed: boolean) => scrubValue(value, secrets, "message", 0, false, undefined, none, framed);
const nest = (depth: number, leaf: any) => { let value = leaf; for (let i = 0; i < depth; i++) value = { k: value }; return value; };
const leaf = (value: any): any => { while (value !== null && typeof value === "object" && Object.keys(value).length === 1 && "k" in value) value = value.k; return value; };

// The reference replaces the secret before the pair rule reads the text, so the name is cut and the value after the line break goes with the secret.
describe("a secret that holds a line break after a pair separator", () => {
  test("redacts the value after a colon and a line break, inside unbalanced brackets (prose)", () => {
    const out = leaf(run(nest(0, "\t{\"pwd\": \"hunt\"SetCookie:\nTAILVD5864C]"), ["kie:\nTAILVD5864C"], false));
    expect(out).not.toContain("TAILVD5864C");
    // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name is the placeholder as a whole.
    expect(out).toBe("<redacted>");
  });
  test("redacts the value after a colon and a line break, inside unbalanced brackets (framed)", () => {
    const out = leaf(run(nest(0, "\t{\"pwd\": \"hunt\"SetCookie:\nTAILVD5864C]"), ["kie:\nTAILVD5864C"], true));
    expect(out).not.toContain("TAILVD5864C");
    // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name is the placeholder as a whole.
    expect(out).toBe("<redacted>");
  });
  test("redacts the value after a colon and a line break, inside unbalanced brackets, at depth 29", () => {
    const out = leaf(run(nest(29, "\t{\"pwd\": \"hunt\"SetCookie:\nTAILVD5864C]"), ["kie:\nTAILVD5864C"], true));
    expect(out).not.toContain("TAILVD5864C");
    // d2dab4e8 (version 6, round 3): a text that begins as a JSON object, is not one JSON value and holds a credential name is the placeholder as a whole.
    expect(out).toBe("<redacted>");
  });
  test("redacts the value after a quoted name, an equals sign and a line break", () => {
    const out = leaf(run(nest(0, "{\\\"note\\\": null}\"Cookie\"=\nTAILVAC8868"), ["ie\"=\nTAIL"], true));
    expect(out).not.toContain("TAILVAC8868");
    expect(out).toBe("{\\\"note\\\": null}\"Cook<redacted>VAC8868");
  });
  test("redacts the value after a quoted name, an equals sign and a line break, at depth 30", () => {
    const out = leaf(run(nest(30, "{\\\"note\\\": null}\"Cookie\"=\nTAILVAC8868"), ["ie\"=\nTAIL"], true));
    expect(out).not.toContain("TAILVAC8868");
    expect(out).toBe("{\\\"note\\\": null}\"Cook<redacted>VAC8868");
  });
  test("redacts the value after a line break beside a second secret that ends in a string", () => {
    const out = leaf(run(nest(0, "{\\\"note\\\": null}\"Cookie\"=\nTAILVAC8868"), ["ie\"=\nTAIL", "e\\\": null}"], true));
    expect(out).not.toContain("TAILVAC8868");
    expect(out).toBe("{\\\"not<redacted>\"Cook<redacted>VAC8868");
  });
  test("redacts the value after a line break beside a second secret that ends in a string, at depth 30", () => {
    const out = leaf(run(nest(30, "{\\\"note\\\": null}\"Cookie\"=\nTAILVAC8868"), ["ie\"=\nTAIL", "e\\\": null}"], true));
    expect(out).not.toContain("TAILVAC8868");
    expect(out).toBe("{\\\"not<redacted>\"Cook<redacted>VAC8868");
  });
  test("redacts the value after an escaped quote, an equals sign and a line break in braces", () => {
    const out = leaf(run(nest(0, "{ok%22\\\"SetCookie\\\"=\nTAILV0121CD}"), ["\"=\nTAILV0"], true));
    expect(out).not.toContain("TAILV0121CD");
    expect(out).toBe("{ok%22\\\"SetCookie\\<redacted>121CD}");
  });
  test("redacts the value after an escaped quote, an equals sign and a line break in braces, at depth 30", () => {
    const out = leaf(run(nest(30, "{ok%22\\\"SetCookie\\\"=\nTAILV0121CD}"), ["\"=\nTAILV0"], true));
    expect(out).not.toContain("TAILV0121CD");
    expect(out).toBe("{ok%22\\\"SetCookie\\<redacted>121CD}");
  });
  test("redacts the value after a line break, with secrets that cut a pair before it", () => {
    const out = leaf(run(nest(0, "'api_key'\":\"TAILV28C038\"{ok%22\\\"SetCookie\\\"=\nTAILV0121CD}"), ["y'\":\"TAILV28C0", "tCookie\\", "\"=\nTAILV0"], true));
    expect(out).not.toContain("TAILV0121CD");
    expect(out).toBe("'api_ke<redacted>38\"{ok%22\\\"Se<redacted><redacted>121CD}");
  });
  test("redacts the value after a line break, with secrets that cut a pair before it, at depth 30", () => {
    const out = leaf(run(nest(30, "'api_key'\":\"TAILV28C038\"{ok%22\\\"SetCookie\\\"=\nTAILV0121CD}"), ["y'\":\"TAILV28C0", "tCookie\\", "\"=\nTAILV0"], true));
    expect(out).not.toContain("TAILV0121CD");
    expect(out).toBe("'api_ke<redacted>38\"{ok%22\\\"Se<redacted><redacted>121CD}");
  });
  test("redacts the value after an encoded equals sign, a space and a line break", () => {
    const out = leaf(run(nest(0, "{\"a\"Authorization%3D \n\"TAILV2950DC}"), ["3D \n\"TAILV2"], true));
    expect(out).not.toContain("TAILV2950DC");
    expect(out).toBe("{\"a\"Authorization%<redacted>950DC}");
  });
  test("redacts the value after an encoded equals sign and a line break among other pairs", () => {
    const out = leaf(run(nest(0, "x\\n\"PASS\" : \r\nTAILV601FCD{\"password\"\" : MK40D7AF4B\"Authorization%3D \n\"TAILV2950DC}{\"sessionid\":\"\"}"), ["\"\" : MK40", "ASS\" : \r\nTAILV6", "3D \n\"TAILV2"], true));
    expect(out).not.toContain("TAILV2950DC");
    // The registered values take out each name's separator and value head; the values they leave are hidden with them.
    expect(out).toBe("x\\n\"P<redacted>");
  });
  test("redacts the value after a spaced equals sign and a line break", () => {
    const out = leaf(run(nest(0, "\t{Cookie = \n TAILV1446EF]"), ["kie = \n TAILV144"], true));
    expect(out).not.toContain("TAILV1446EF");
    expect(out).toBe("\t{Coo<redacted>6EF]");
  });
  test("redacts the value after a spaced equals sign and a line break, at depth 31", () => {
    const out = leaf(run(nest(31, "\t{Cookie = \n TAILV1446EF]"), ["kie = \n TAILV144"], true));
    expect(out).not.toContain("TAILV1446EF");
    expect(out).toBe("\t{Coo<redacted>6EF]");
  });
  test("redacts the value after a spaced equals sign and a line break, before a pair with an encoded separator", () => {
    const out = leaf(run(nest(0, "\t{Cookie = \n TAILV1446EF\\\"passwd%3D 'MKE5946F5F]"), ["kie = \n TAILV144", " \n TAILV1446EF"], true));
    expect(out).not.toContain("TAILV1446EF");
    expect(out).toBe("\t{Coo<redacted>6EF\\\"passwd%3D <redacted>");
  });
  test("redacts the value after an equals sign and a carriage return and line feed", () => {
    const out = leaf(run(nest(0, "{;Cookie=\r\n\"MKE38DEC91}"), ["okie=\r\n\"MKE38DE"], true));
    expect(out).not.toContain("MKE38DEC91");
    expect(out).toBe("{;Co<redacted>C91}");
  });
});

// The reference replaces the string with its quotes by the placeholder, so the pair rule reads the name around it as one name.
describe("a secret that is one whole JSON string between a name and the next name", () => {
  test("redacts the value of the pair after the string", () => {
    const out = leaf(run(nest(0, "key\"\":\"TAILVE40358\\\"{\"pwd\": true, \"access_token\": \"TAILVC3413C\"}"), ["\": true, \"", "access_toke"], true));
    expect(out).not.toContain("TAILVC3413C");
    expect(out).toBe("key\"\":\"TAILVE40358\\\"{\"pwd<redacted><redacted>n\": \"<redacted>\"");
  });
  test("redacts the value of the pair after the string, at depth 29", () => {
    const out = leaf(run(nest(29, "key\"\":\"TAILVE40358\\\"{\"pwd\": true, \"access_token\": \"TAILVC3413C\"}"), ["\": true, \"", "access_toke"], true));
    expect(out).not.toContain("TAILVC3413C");
    expect(out).toBe("key\"\":\"TAILVE40358\\\"{\"pwd<redacted><redacted>n\": \"<redacted>\"");
  });
  test("redacts the value of the pair after the string, before another pair, at depth 29", () => {
    const out = leaf(run(nest(29, "key\"\":\"TAILVE40358\\\"{\"pwd\": true, \"access_token\": \"TAILVC3413C\", \"SetCookie\": 1.5}"), ["\": true, \"", "access_toke", "d\": true,"], true));
    expect(out).not.toContain("TAILVC3413C");
    expect(out).toBe("key\"\":\"TAILVE40358\\\"{\"pwd<redacted><redacted>n\": \"<redacted>\"");
  });
});
