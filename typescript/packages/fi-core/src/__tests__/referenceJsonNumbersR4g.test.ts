import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A JSON text held as a JSON string literal (a value that is the JSON encoding of a JSON text, as a value, an attribute or a leaf of
// a JSON text nested 1 to 30 deep): a known secret that is a number of that text is replaced by a quoted placeholder, so the text the
// literal encodes is still JSON, and it reads as the text with each such number, each string that is the secret and each number
// inside a JSON text held by one of its strings replaced. No run of the secret is readable.
const hidden = "<redacted>";
const secret = "31415926";
const raw = { n: 31415926, values: [1, 31415926, 3], text: secret, arguments: JSON.stringify({ n: 31415926, city: "Oslo" }) };
const before = JSON.stringify(raw);
const expected = { n: hidden, values: [1, hidden, 3], text: hidden, arguments: JSON.stringify({ n: hidden, city: "Oslo" }) };
const runs: [string, (value: string) => string][] = [["scrubValue", value => scrubValue(value, [secret])], ["scrubAttribute", value => scrubAttribute(value, [secret])]];
const encode = (value: string, layers: number) => { for (let i = 0; i < layers; i++) value = JSON.stringify(value); return value; };
const decode = (value: string, layers: number): unknown => { let out: unknown = value; for (let i = 0; i < layers; i++) out = JSON.parse(out as string); return out; };
const nestText = (depth: number, leaf: string) => '{"k": '.repeat(depth) + JSON.stringify(leaf) + "}".repeat(depth);
const leafAt = (text: string, depth: number) => { let out = JSON.parse(text); for (let i = 0; i < depth; i++) out = out.k; return out as string; };
const outcome = (run: () => string): { thrown: string } | { out: string } => { try { return { out: run() }; } catch (error) { return { thrown: String((error as Error).message) }; } };

describe("a JSON text held as a JSON string literal keeps its numbers' placeholders quoted", () => {
  test("the value is the JSON encoding of the JSON text (I1, scrubValue)", () => {
    const double = scrubValue(JSON.stringify(before), [secret]);
    expect(double).not.toContain(secret);
    expect(JSON.parse(JSON.parse(double))).toEqual(expected);
    expect(scrubValue(double, [secret])).toBe(double);
  });

  describe.each(runs)("%s", (_name, run) => {
    test.each([1, 2, 3, 4])("the JSON text encoded %i more times", layers => {
      const out = run(encode(before, layers));
      expect(out).not.toContain(secret);
      expect(decode(out, layers + 1)).toEqual(expected);
    });

    // The leaf of a JSON text nested `depth` deep is the JSON text encoded once (a JSON text) or twice and three times (a JSON
    // string literal of it). Where the scrubber's depth budget runs out for the JSON text leaf, reading the JSON text the literal
    // holds reaches past the depth limit too: the literal is read on its escaped bytes, with no throw and no run of the secret
    // readable, and it is still a JSON string literal: the JSON text with each spelling of the secret replaced by the bare placeholder.
    const depths = Array.from({ length: 30 }, (_, i) => i + 1);
    test.each(depths.flatMap(depth => [2, 3].map(layers => [depth, layers])))("in a JSON text %i deep, the leaf encoded %i times", (depth, layers) => {
      const plain = outcome(() => run(nestText(depth, before)));
      const result = outcome(() => run(nestText(depth, encode(before, layers - 1))));
      expect("out" in result).toBe(true);
      const out = (result as { out: string }).out;
      expect(out).not.toContain(secret);
      if ("thrown" in plain) { expect(decode(leafAt(out, depth), layers - 1)).toBe(before.split(secret).join(hidden)); return; }
      expect(decode(leafAt(out, depth), layers)).toEqual(expected);
    });
  });

  // The numeric lexemes of I1-NUMERIC-ORIGINAL-TOKEN, inside a JSON text that is held as a JSON string literal.
  test.each(["10000000000000001", "1.25000000e+3", "-16180339", "0.12345678", "1.23456789e+400"])("the numeric lexeme %s", lexeme => {
    const text = ` { "n" : ${lexeme}, "values": [1, ${lexeme}, 3], "city": "Oslo" } `;
    const clean = ' { "n" : "<redacted>", "values": [1, "<redacted>", 3], "city": "Oslo" } ';
    for (const scrub of [scrubValue, scrubAttribute]) {
      for (const layers of [1, 2]) {
        const out = scrub(encode(text, layers), [lexeme]) as string;
        expect(out).not.toContain(lexeme);
        expect(decode(out, layers)).toBe(clean);
      }
    }
    expect(scrubText("401 " + JSON.stringify(text) + " retry", [lexeme])).toBe("401 " + JSON.stringify(clean) + " retry");
  });
});
