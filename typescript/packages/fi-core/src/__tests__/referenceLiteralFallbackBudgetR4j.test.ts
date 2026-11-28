import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// A JSON string literal in a text that holds a JSON text, in a value whose other members use most of the node budget. The reference
// charges such a literal as one node, as it charges any string, so the value is exported whole up to the node budget whatever the
// literal holds and in whichever order the members come; only a value whose own nodes pass the budget fails closed. Where the literal's
// decoded read is compared with its read on the escaped bytes, that comparison is not charged to the value's budget.

const S = "PLNTq7Zr4kXw9";
const zeros = (n: number): number[] => Array<number>(n).fill(0);
/** The literal as Python's json.dumps spells it (", " and ": " separators), as the reference's fixtures are written. */
function spelled(m: number, near: boolean): string {
  return '{"a": [' + zeros(m).join(", ") + '], "s": "' + S + '"' + (near ? ', "t": "PLNT x"' : "") + "}";
}
const message = (m: number, near: boolean): string => "note " + JSON.stringify(spelled(m, near)) + " end";
/** The export of the message: the literal read as the JSON text it encodes, with the known secret hidden and its other strings kept. */
function exported(m: number, near: boolean): string {
  return "note " + JSON.stringify(JSON.stringify(near ? { a: zeros(m), s: "<redacted>", t: "PLNT x" } : { a: zeros(m), s: "<redacted>" })) + " end";
}
/** The JSON text the exported literal encodes, decoded: what d2dab4e8's export of the same message decodes to. */
function decoded(msg: string): unknown {
  expect(msg.startsWith("note ") && msg.endsWith(" end")).toBe(true);
  return JSON.parse(JSON.parse(msg.slice(5, -4)) as string);
}
/** Whether a scrub fails closed: it throws a budget error, or exports the whole value as the placeholder. */
function failsClosed(read: () => unknown): boolean {
  let out: unknown;
  try { out = read(); } catch (error) {
    if (error instanceof Error && /^Privacy (structure|text) budget$/.test(error.message)) return true;
    throw error;
  }
  return out === "<redacted>";
}

describe("a JSON string literal that holds a JSON text, beside members that use most of the node budget", () => {
  test("16000 numbers beside a literal of 500 numbers, the secret and a string that holds a run of it: the value is exported", () => {
    const input = { pad: zeros(16000), msg: message(500, true) };
    const out = scrubValue(input, [S]) as { pad: number[]; msg: string };
    expect(out).toEqual({ pad: zeros(16000), msg: exported(500, true) });
    expect(decoded(out.msg)).toEqual({ a: zeros(500), s: "<redacted>", t: "PLNT x" });
    expect(JSON.stringify(out)).not.toContain(S);
  });

  // Pads up to 16380 numbers: the value has at most 16383 nodes of its own with the literal counted as one, which the node budget holds.
  const rows: [pad: number, m: number, near: boolean][] = [];
  for (const pad of [0, 15876, 15877, 16276, 16277, 16380]) for (const m of [100, 500]) for (const near of [true, false]) rows.push([pad, m, near]);
  test.each(rows)("%i numbers beside a literal of %i numbers (a string with a run of the secret beside it: %s): exported whole, in either member order", (pad, m, near) => {
    const expected = exported(m, near);
    const first = scrubValue({ pad: zeros(pad), msg: message(m, near) }, [S]) as { pad: number[]; msg: string };
    expect(first).toEqual({ pad: zeros(pad), msg: expected });
    expect(decoded(first.msg)).toEqual(near ? { a: zeros(m), s: "<redacted>", t: "PLNT x" } : { a: zeros(m), s: "<redacted>" });
    expect(scrubValue({ msg: message(m, near), pad: zeros(pad) }, [S])).toEqual({ msg: expected, pad: zeros(pad) });
    expect(scrubValue([zeros(pad), message(m, near)], [S])).toEqual([zeros(pad), expected]);
    // The same value as one JSON document in a text has one node more, so it is held up to 16379 numbers.
    if (pad <= 16379) expect(JSON.parse(scrubText(JSON.stringify({ pad: zeros(pad), msg: message(m, near) }), [S]))).toEqual({ pad: zeros(pad), msg: expected });
  });

  const past: [pad: number, m: number, near: boolean][] = [];
  for (const pad of [16381, 16500]) for (const m of [100, 500]) for (const near of [true, false]) past.push([pad, m, near]);
  test.each(past)("%i numbers beside a literal of %i numbers (%s): the value's own nodes pass the node budget and it fails closed", (pad, m, near) => {
    expect(failsClosed(() => scrubValue({ pad: zeros(pad), msg: message(m, near) }, [S]))).toBe(true);
    expect(failsClosed(() => scrubValue({ msg: message(m, near), pad: zeros(pad) }, [S]))).toBe(true);
    expect(failsClosed(() => scrubValue({ pad: zeros(pad), msg: message(m, near) }, []))).toBe(true);
    expect(failsClosed(() => scrubText(JSON.stringify({ pad: zeros(pad - 1), msg: message(m, near) }), [S]))).toBe(true);
  });

  test("the node budget's edge does not depend on how many nodes the literal that holds the secret encodes", () => {
    const secrets = [S];
    for (const m of [0, 1, 100, 500]) {
      expect(failsClosed(() => scrubValue({ pad: zeros(16380), msg: message(m, true) }, secrets))).toBe(false);
      expect(failsClosed(() => scrubValue({ pad: zeros(16381), msg: message(m, true) }, secrets))).toBe(true);
      expect(failsClosed(() => scrubText(JSON.stringify({ pad: zeros(16379), msg: message(m, true) }), secrets))).toBe(false);
      expect(failsClosed(() => scrubText(JSON.stringify({ pad: zeros(16380), msg: message(m, true) }), secrets))).toBe(true);
    }
  });
});

describe("a JSON string literal read on its escaped bytes, beside members that use most of the node budget", () => {
  // The longer secret is spelled as given only once the literal's é is decoded, so the decoded read would show LQ64; the literal
  // is read on its escaped bytes, as d2dab4e8 reads it. Whatever the budget left, the export is that read or the value fails closed;
  // it is never the decoded read.
  const A = "LQ64F0FD13", secrets = [A, "F0FD13é pxx"];
  const literal = (m: number) => '{"a": [' + zeros(m).join(", ") + '], "k": "LQ64F0FD13\\u00e9 pxx"}';
  const reference = (m: number) => "note " + JSON.stringify('{"a": [' + zeros(m).join(", ") + '], "k": "<redacted>\\u00e9 pxx"}') + " end";
  const rows: [pad: number, m: number][] = [];
  for (const pad of [0, 2, 15000, 16000, 16380]) for (const m of [0, 3, 500]) rows.push([pad, m]);
  test.each(rows)("%i numbers beside a literal of %i numbers", (pad, m) => {
    for (const order of ["pad", "msg"]) {
      const input = order === "pad" ? { pad: zeros(pad), msg: "note " + JSON.stringify(literal(m)) + " end" } : { msg: "note " + JSON.stringify(literal(m)) + " end", pad: zeros(pad) };
      let out: unknown;
      try { out = scrubValue(input, secrets); } catch (error) {
        expect(error instanceof Error && /^Privacy (structure|text) budget$/.test(error.message)).toBe(true);
        expect(pad).toBeGreaterThan(15000);
        continue;
      }
      if (out === "<redacted>") { expect(pad).toBeGreaterThan(15000); continue; }
      expect(out).toEqual(order === "pad" ? { pad: zeros(pad), msg: reference(m) } : { msg: reference(m), pad: zeros(pad) });
      expect(JSON.stringify(out)).not.toContain("LQ64<redacted>");
    }
  });
});
