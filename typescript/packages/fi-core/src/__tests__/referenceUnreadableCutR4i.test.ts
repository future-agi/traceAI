import { describe, expect, it } from "@jest/globals";
import { scrubValue } from "../privacy-runtime";

// Rows from the r4i differential against the gate Reference d2dab4e8 (spec/auto-instrumentation/generate_privacy_fixtures.py sha256 4dbcc945...2c5192c6,
// privacy.json sha256 32ba1dbb...18f669775). Each row is [source row id, input, known secrets, param depth, framed, d2 Reference output]:
// the value is Reference(privacy_catalog()).scrub(input, "", None, tuple(secrets), _depth=depth, framed=framed), and scrubValue with the
// same arguments must export exactly that. Every row fails at 5a7f9514.
// The rows are the cut rows as they are (texts longer than 6000 characters).

type Row = [id: string, input: unknown, secrets: string[], depth: number, framed: boolean, expected: unknown];
const NL5000 = "\n".repeat(5000);
/** A value under `levels` objects that each have the single member "k". */
function nest(levels: number, leaf: unknown): unknown {
  let value = leaf;
  for (let i = 0; i < levels; i++) value = { k: value };
  return value;
}
const exported = (input: unknown, secrets: string[], depth: number, framed: boolean): unknown =>
  scrubValue(input, secrets, "", depth, false, undefined, undefined, framed);

describe("referenceUnreadableCutR4i: whole value", () => {
  // Cut rows (over 6000 characters, so cls.py cut them): a text that begins as a JSON object, is not one JSON value and holds a
  // credential member name is the placeholder as a whole (d2 unreadable(), credential member name branch).
  const rows: Row[] = [
    // matrix-F-6a-'\n'-5000-0 [cut] d2 Reference output: "<redacted>"
    ["matrix-F-6a-'\\n'-5000-0", "{\"\":\"\"}" + NL5000 + " \\\":\\\"TAsession\":TAILVF", ["}\\\":\\\"TA"], 0, true, "<redacted>"],
    // matrix-F-3a-'\n'-5000-0 [cut] d2 Reference output: "<redacted>"
    ["matrix-F-3a-'\\n'-5000-0", "{x \"password\"" + NL5000 + ": \"MK5949289F\"", ["x \"password\": \"MK59"], 0, true, "<redacted>"],
    // matrix-F-4-'\n'-5000-0 [cut] d2 Reference output: "<redacted>"
    ["matrix-F-4-'\\n'-5000-0", "{\"a\": true}" + NL5000 + "{\"X-Api-Key\":\"MK7AAE369C\"}                       ", ["true}{\"X-Api-Key"], 0, false, "<redacted>"],
    // matrix-J1-'\n'-5000-0 [cut] d2 Reference output: "<redacted>"
    ["matrix-J1-'\\n'-5000-0", "{\"password\": \"MK5949289F\"}" + NL5000 + " {\"token\":\"TAILV77AB12\"}", ["\"} {\"tok"], 0, true, "<redacted>"],
    // matrix-J3-'\n'-5000-0 [cut] d2 Reference output: "<redacted>"
    ["matrix-J3-'\\n'-5000-0", "{\"token\": \"MK9F8E7D\"}" + NL5000 + "{\\\"pwd\\\":\\\"TAILVA1B2C3", ["}{\\\"pwd"], 0, true, "<redacted>"],
  ];
  it.each(rows)("%s", (_id, input, secrets, depth, framed, expected) => {
    expect(exported(input, secrets, depth, framed)).toEqual(expected);
  });
  // The example from the task (prop pw shape): d2 unreadable(), credential member name "password". At 5a7f9514 the export hides the secret
  // but keeps the rest of the text, so it is not LESS by runs; it is RED for the exact output.
  it("task example: an object with a password member followed by text", () => {
    expect(scrubValue("{\"note\": \"hello\", \"password\": \"pw123456789\"}\nlog", ["pw123456789"])).toEqual("<redacted>");
  });
});

describe("referenceUnreadableCutR4i: whole leaf", () => {
  // Cut rows under {"k": ...} objects: the leaf text is the placeholder (d2 unreadable(), credential member name branch).
  const rows: Row[] = [
    // matrix-F-3a-'\n'-5000-5 [cut] d2 Reference output: 5 levels of {"k": ...} around "<redacted>"
    ["matrix-F-3a-'\\n'-5000-5", nest(5, "{x \"password\"" + NL5000 + ": \"MK5949289F\""), ["x \"password\": \"MK59"], 0, true, nest(5, "<redacted>")],
    // matrix-J1-'\n'-5000-30 [cut] d2 Reference output: 30 levels of {"k": ...} around "<redacted>"
    ["matrix-J1-'\\n'-5000-30", nest(30, "{\"password\": \"MK5949289F\"}" + NL5000 + " {\"token\":\"TAILV77AB12\"}"), ["\"} {\"tok"], 0, true, nest(30, "<redacted>")],
  ];
  it.each(rows)("%s", (_id, input, secrets, depth, framed, expected) => {
    expect(exported(input, secrets, depth, framed)).toEqual(expected);
  });
});
