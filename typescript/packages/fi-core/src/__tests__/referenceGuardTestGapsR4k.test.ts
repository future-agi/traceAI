import { describe, expect, test } from "@jest/globals";
import { scrubValue, type PrivacyBudget } from "../privacy-runtime";

// A quoted name with a string value in prose is charged for the read it exports, even when it falls back to reading the raw bytes.
// A JSON string literal read on its escaped bytes uses the containing value's node budget for the nodes it reads. A single-dash flag
// with a percent-spelled name start hides its value if the name reads as a credential name, while retaining the flag's spelling.

const marker = "PLNTq7Zr4kXw9";
const placeholder = "<redacted>";
const budgetError = /^Privacy structure budget$/;
const zeros = (count: number): number[] => Array<number>(count).fill(0);

function field(value: string): string {
  let nested = JSON.stringify({ s: value, a: zeros(50), x: "ab".repeat(400) });
  for (let layer = 0; layer < 3; layer++) nested = JSON.stringify(nested);
  return `"k": ${JSON.stringify(nested)}`;
}

function charged(input: string) {
  const budget: PrivacyBudget = { nodes: 16384, text: 4194304, seen: new WeakSet() };
  const out = scrubValue(input, [marker], "", 0, false, budget);
  return { out, nodes: 16384 - budget.nodes, text: 4194304 - budget.text };
}

describe("a name and string value in prose are charged for the raw-bytes read they export", () => {
  test("a nested string value charges the same read as the pair with its secret already replaced", () => {
    const input = `pre ${field(marker)} post`;
    const expected = `pre ${field(placeholder)} post`;
    const control = charged(expected);
    expect(control.out).toBe(expected);
    expect(charged(input)).toEqual({
      out: expected,
      nodes: control.nodes,
      text: control.text + (input.length - expected.length),
    });
  });

  test("150 such pairs in prose exhaust the structure budget", () => {
    const pair = field(marker);
    const input = `pre ${Array<string>(150).fill(pair).join(" and ")} post`;
    expect(() => scrubValue(input, [marker])).toThrow(budgetError);
  });
});

describe("a JSON string literal read on its escaped bytes charges those nodes to the containing value", () => {
  // The longer known secret overlaps the shorter one only after the inner Unicode escape is read. Reading the escaped bytes first
  // hides the shorter secret whole, instead of leaving its first four characters visible.
  const secrets = ["LQ64F0FD13", "F0FD13é pxx"];
  const message = "note " + JSON.stringify('{"a": [0, 0, 0], "k": "LQ64F0FD13\\u00e9 pxx"}') + " end";
  const expected = "note " + JSON.stringify('{"a": [0, 0, 0], "k": "<redacted>\\u00e9 pxx"}') + " end";

  test("the escaped spelling is exported when the pad leaves room for the escaped read", () => {
    const pad = zeros(16370);
    expect(scrubValue({ pad, msg: message }, secrets)).toEqual({ pad, msg: expected });
  });

  test.each(["first", "last"])("the escaped read and the pad exhaust the budget with the literal %s", (order) => {
    const pad = zeros(16380);
    const input = order === "first" ? { msg: message, pad } : { pad, msg: message };
    expect(() => scrubValue(input, secrets)).toThrow(budgetError);
  });
});

describe("a single-dash flag with a percent-spelled name start", () => {
  // The value marker is not supplied as a known secret; the flag name itself decides whether to hide it.
  const rows: [input: string, expected: string][] = [
    [`note -%74oken ${marker} end`, "note -%74oken <redacted> end"],
    [`run -%63lient_secret ${marker} now`, "run -%63lient_secret <redacted> now"],
    [`-%%74oken ${marker}`, "-%%74oken <redacted>"],
    [`run -%6Eame ${marker} now`, `run -%6Eame ${marker} now`],
  ];
  test.each(rows)("%j reads the name without changing its spelling", (input, expected) => {
    expect(scrubValue(input, [])).toBe(expected);
  });
});
