import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// A registered value only adds redaction. Where a read writes more than placeholders (a JSON text it writes again), it cannot be placed
// in the original text; such a read is kept only where it shows no 4 characters together that the other read does not show.

/** The 4-character runs of the registered values that `output` shows. */
const registeredRunsShown = (output: string, secrets: string[]) => {
  const shown: string[] = [];
  for (const secret of secrets) for (let i = 0; i + 4 <= secret.length; i++) if (output.includes(secret.slice(i, i + 4))) shown.push(secret.slice(i, i + 4));
  return shown;
};

describe("two reads of which one cannot be placed, where the subsequence test alone keeps a read that shows a registered run", () => {
  test("a nested JSON string with a list and a quoted api_key name", () => {
    const input = "{\"k\": {\"k\": \"[\\\"MKEA96B272\\\"]'api_key' :27336668490\"}}", secrets = ["33666849", "72\"]'api_k", "i_key' :27"];
    expect(registeredRunsShown(scrubText(input, secrets), secrets)).toEqual([]);
  });
  test("a quoted password pair followed by a JSON fragment, deep in a value", () => {
    const input = "'password': 'MK1' [{\"x\":TAILVA2636\"", secrets = ["1' [{\"x\":TAI", "ord': 'MK1"];
    expect(registeredRunsShown(scrubText(input, secrets), secrets)).toEqual([]);
    expect(registeredRunsShown(scrubValue(input, secrets, "message", 29), secrets)).toEqual([]);
  });
});

describe("two reads of which one cannot be placed, where the subsequence test alone would keep the joined read", () => {
  // The joined read hides every character the reference-order read hides as a subsequence, but shows a run of a registered value
  // that the reference-order read does not show together: neither read is kept, and the text is the placeholder.
  test("nested JSON strings holding an escaped api_key object", () => {
    const input = "{\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": {\"k\": \"\\\"\\\\\\\"{\\\\\\\\\\\\\\\"api_key\\\\\\\\\\\\\\\": \\\\\\\\\\\\\\\"MK4D\\\\\\\\\\\\\\\\u0041A6FB1\\\\\\\\\\\\\\\", \\\\\\\\\\\\\\\"n\\\\\\\\\\\\\\\": \\\\\\\\\\\\\\\"s\\\\\\\\\\\\\\\"}\\\\\\\"\\\"\\\"TAILQ9EFF0A8\\\"'note'\"}}}}}}}}}}}}}}}}}}}}}}}}}}}}}", secrets = ["TAILQ9EFF0", "{\\\\\\\"api_k"];
    expect(registeredRunsShown(scrubText(input, secrets), secrets)).toEqual([]);
  });
});
