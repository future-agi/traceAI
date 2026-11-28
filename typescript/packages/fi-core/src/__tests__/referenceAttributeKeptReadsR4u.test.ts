import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A quoted JSON-string attribute (a JSON text that is one string) given registered values: every read of it runs on the original input,
// and what any read hides stays hidden in the output.

/** The 8-character runs of each registered value that `output` shows, as written or with its JSON escapes read. */
const registeredShown = (output: string, registered: readonly string[]) => {
  const views = [output, output.replace(/\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g, (_, h, c) => h ? String.fromCharCode(parseInt(h, 16)) : JSON.parse(`"\\${c}"`))];
  const shown: string[] = [];
  for (const value of registered) for (let i = 0; i + 8 <= value.length; i++) if (views.some(v => v.includes(value.slice(i, i + 8)))) shown.push(value.slice(i, i + 8));
  return shown;
};

describe("a registered value the raw bytes hold that runs from URL text into a credential the URL catalog hides", () => {
  // The catalog hides the credential's value first, so a registered value that starts in the URL before it no longer matches what is left;
  // it is hidden whole all the same: what scrubAttribute shows of it is nothing, as scrubValue and scrubText show nothing of it.
  const rows: [label: string, input: string, registered: string[], expected: string][] = [
    ["a URL fragment api%5Fkey",
      "\"https://e.invalid/x?safe=PLNTSsGPdqu9QyEjjuoMd9MXKk#tag=ok&api%5Fkey=PLNToS3QUdkCzqciObErRP&keep=PLNTBezRfLjeZQWnYbOJ7B4n\"",
      ["https://e.invalid/x?safe=PLNTSsGPdqu9QyEjjuoMd9MXKk#tag=ok&api%5Fkey=PLNToS", "&keep=PLNTBezRf"], "\"<redacted>\""],
    ["a URL query api_key",
      "\"https://e.invalid/x?safe=PLNTr7Hc2WqLm9XvKd4Tz8&api_key=PLNTb3Nf6YpQs1RgUe5Wj0&keep=PLNTa8Vd2Kx\"",
      ["https://e.invalid/x?safe=PLNTr7Hc2WqLm9XvKd4Tz8&api_key=PLNTb3", "&keep=PLNTa8V"], "\"<redacted>\""],
    ["a URL path before a query token",
      "\"https://e.invalid/v1/files/PLNTm4Jq8ZtRw2Lx/report?token=PLNTk9Ds3HvYc6Bn1Pq&keep=PLNTe5Gu7\"",
      ["https://e.invalid/v1/files/PLNTm4Jq8ZtRw2Lx/report?token=PLNTk9D"], "\"<redacted>\""],
    ["a URL in a JSON string field of the string",
      "\"{\\\"u\\\":\\\"https://e.invalid/x?safe=PLNTq2Wd7Hn4Kc9Lz&access_token=PLNTv6Rb1Ts8Mx3Py&keep=PLNTh5\\\"}\"",
      ["https://e.invalid/x?safe=PLNTq2Wd7Hn4Kc9Lz&access_token=PLNTv6"], "\"{\\\"u\\\":\\\"<redacted>\""],
  ];
  test.each(rows)("%s: scrubAttribute hides the registered value whole", (_, input, registered, expected) => {
    expect(scrubAttribute(input, registered)).toBe(expected);
    expect(registeredShown(scrubAttribute(input, registered), registered)).toEqual([]);
    expect(typeof JSON.parse(scrubAttribute(input, registered))).toBe("string");
  });
  test.each(rows)("%s: scrubValue and scrubText show none of it either", (_, input, registered) => {
    expect(registeredShown(scrubValue(input, registered), registered)).toEqual([]);
    expect(registeredShown(scrubText(input, registered), registered)).toEqual([]);
  });
});

describe("the reference-order read is joined unless all it adds is quoting, judged by where each kept part stands", () => {
  // The read of the string keeps "rd=" (the rest of "password=" after the registered "note passwo"); "rd=" also stands in the raw bytes
  // in "password=", so that kept part has two places. The reference-order read of the raw bytes hides the first: the attribute does
  // not show it, as it is not known to be shown only where the read hides nothing but quoting.
  test("a kept part that stands at two places in the raw bytes", () => {
    const input = "\"note password=PLNTaaaa1111bbbb ; p\\u0061ssword=PLNTcccc2222dddd end\"";
    expect(scrubAttribute(input, ["note passwo"])).toBe("\"<redacted>\"");
    expect(scrubAttribute(input, ["note passwo"])).not.toContain("rd=");
  });
});

describe("a read of a quoted root that cannot be joined by position with another read", () => {
  // The read of the string writes the JSON text it holds again (no space after ':' or ','), so it cannot be placed in the raw bytes;
  // the read of the raw bytes with no registered value (as scrubValue reads them) reads the pair's value to the end of the text and so
  // hides '"m": 1}'. The read of the string shows '","m":1}': it is not kept, and the attribute is the quoted placeholder.
  test("the read of the string shows what the raw-bytes read with no registered value hides", () => {
    const input = "\"{\\\"note\\\": \\\"ok \\u0061pi_key=PLNTq7Wd2Hn4Kc9Lz end\\\", \\\"m\\\": 1}\"";
    expect(scrubValue(input, [])).toBe("\"{\\\"note\\\": \\\"ok \\u0061pi_key=<redacted>");
    expect(scrubAttribute(input, ["ok api_k"])).toBe("\"<redacted>\"");
  });
  // Where the other read hides nothing the kept read shows, the kept read stays as it is: a JSON text written again is kept.
  test("a read written again is kept where no other read hides what it shows", () => {
    const input = JSON.stringify('{"k": "LQ64F0FD13\\u00e9 pxx"}');
    expect(scrubAttribute(input, ["LQ64F0FD13"])).toBe(JSON.stringify('{"k":"<redacted>é pxx"}'));
  });
});

describe("a budget a read of a quoted root runs out of fails closed for that attribute alone", () => {
  // The read of the string reads the JSON array it holds and runs out of the structure budget: the attribute is the quoted placeholder
  // (no error is thrown for it), and the next call with a small text is read as before.
  test("a quoted root holding a JSON array of 20,000 strings and a registered value", () => {
    const input = JSON.stringify("[" + Array(20_000).fill('"x"').join(",") + "] PLNTq7Wd2Hn4Kc9Lz");
    expect(scrubAttribute(input, ["PLNTq7Wd2Hn4Kc9Lz"])).toBe("\"<redacted>\"");
    expect(scrubAttribute("\"note PLNTq7Wd2Hn4Kc9Lz end\"", ["PLNTq7Wd2Hn4Kc9Lz"])).toBe("\"note <redacted> end\"");
  });
});
