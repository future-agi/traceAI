import { describe, expect, test } from "@jest/globals";
import { scrubValue } from "../privacy-runtime";

// A JSON string literal that holds a JSON text and is read as the string it encodes: the read of that JSON text is bounded by a node
// budget of its own, and the literal is charged to the value's budget as one string, as the reference charges it. So a sibling of the
// literal is read with the same budget whatever the order of the two, and a JSON text too large for a value of its own is read on the
// literal's escaped bytes, with no run of the secret readable.
const secret = "31415926";
const numbers = (length: number, from: number) => Array.from({ length }, (_, i) => from + i);
const holding = (length: number) => { const list = numbers(length, 1000); list[5] = 31415926; return list; };

describe("the read of a JSON text held by a JSON string literal has a node budget of its own", () => {
  const literal = JSON.stringify(JSON.stringify(holding(9000)));
  const sibling = numbers(9000, 0);
  test.each([["literal first", { a: literal, b: sibling }], ["sibling first", { b: sibling, a: literal }]])("%s", (_order, value) => {
    const out = scrubValue(value, [secret]) as { a: string; b: number[] };
    expect(out.a).not.toContain(secret);
    const inner = JSON.parse(JSON.parse(out.a));
    expect(inner).toHaveLength(9000);
    expect(inner[5]).toBe("<redacted>");
    expect(inner[6]).toBe(1006);
    expect(out.b).toEqual(sibling);
  });

  test("a JSON text past the node limit is read on the literal's escaped bytes", () => {
    const out = scrubValue(JSON.stringify(JSON.stringify(holding(17000))), [secret]) as string;
    expect(out).not.toContain(secret);
    const text = JSON.parse(out);
    expect(typeof text).toBe("string");
    expect(text.startsWith("[1000,1001,1002,1003,1004,<redacted>,1006,")).toBe(true);
  });

  test("a literal whose JSON text holds the secret only in a string reads as that JSON text with the string replaced", () => {
    const text = '{"a": "caf\\u00e9 sekret123", "b": 1}';
    const out = scrubValue(JSON.stringify(text), ["sekret123"]) as string;
    expect(out).not.toContain("sekret123");
    expect(JSON.parse(JSON.parse(out))).toEqual({ a: "café <redacted>", b: 1 });
  });

  test("forty literals in one text, each holding a JSON text of 500 numbers, are each read as the JSON text they hold", () => {
    const literal = JSON.stringify(JSON.stringify({ n: 31415926, a: Array(500).fill(7) }));
    const out = scrubValue(Array(40).fill("w " + literal).join(" "), [secret]) as string;
    expect(out).not.toContain(secret);
    const parts = out.split("w ").slice(1).map(part => part.trim());
    expect(parts).toHaveLength(40);
    for (const part of parts) expect(JSON.parse(JSON.parse(part))).toEqual({ n: "<redacted>", a: Array(500).fill(7) });
  });
});

// The runs of the known secrets that decide whether a JSON string's quote goes with its placeholder are read again when the array of
// secrets holds other secrets than when they were last read.
test("an array of secrets changed in place between two calls is read with the secrets it holds", () => {
  const text = 'k "a\\nBCDEFGHI PQRSTUVWXYZ" QRS1 end';
  const secrets = ["nBCDEFGHI", 'PQRSTUVWXYZ"', "zzzzzzzzzz"];
  expect(scrubValue(text, secrets)).toBe('k "<redacted>" QRS1 end');
  secrets[2] = 'XYZ" QRS1';
  expect(scrubValue(text, secrets)).toBe("k <redacted> QRS1 end");
  expect(scrubValue(text, [...secrets])).toBe("k <redacted> QRS1 end");
});
