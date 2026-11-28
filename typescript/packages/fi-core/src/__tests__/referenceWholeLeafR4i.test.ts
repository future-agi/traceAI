import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// The whole-leaf class of the r4i differential against the d2dab4e8 Reference. A string leaf, one or more object levels down (or inside a JSON text of
// such objects), begins as a JSON object or array and is not one JSON value. Reference.unreadable() (version 6, round 3; called from
// _scrub when json read fails) makes that leaf the placeholder when it repeats a member name in an object read to its end, holds a
// credential name as a quoted member name (read as JSON reads names), or holds a pair the text pair rule reads only once its JSON
// string forms are spelled out. Read as prose, the leaf would have only the values the pair rule can pair redacted, and the credential
// names, the frame around them and the parts of a known secret the text does not spell as given would stay readable.
// Every expected value is the d2dab4e8 Reference output: Reference(privacy_catalog()).scrub(input, "", None, secrets, _depth=0, framed).

const P = "<redacted>";
/** The leaf under `levels` objects {"k": ...}. */
function nest(levels: number, leaf: unknown): unknown {
  let value = leaf;
  for (let i = 0; i < levels; i++) value = { k: value };
  return value;
}
/** A JSON text of `levels` objects {"k": ...} around an encoded leaf, spelled as the generators spell it ('{"k": ' ... '}'). */
function jsonSource(levels: number, encoded: string): string {
  return '{"k": '.repeat(levels) + encoded + "}".repeat(levels);
}
/** The same JSON text as the Reference writes it back (no spaces). */
function jsonText(levels: number, encoded: string): string {
  return '{"k":'.repeat(levels) + encoded + "}".repeat(levels);
}
function input(leaf: string, frame: string, levels: number): unknown {
  return frame === "top" ? leaf : frame === "obj" ? nest(levels, leaf) : jsonSource(levels, JSON.stringify(leaf));
}

describe("referenceWholeLeafR4i", () => {
  // [case id, leaf, frame (top: the leaf itself; obj: under `levels` objects; json: in a JSON text of `levels` objects), levels,
  //  known secrets, framed, d2dab4e8 output]
  const cases: [id: string, leaf: string, frame: string, levels: number, secrets: string[], framed: boolean, expected: unknown][] = [
    // task (task-example; d2 unreadable() c:member-name:'password'; task example, top level, not a harness row; TS differs but shows no run of the secret): d2dab4e8 gives "<redacted>"
    ["00-task", "{\"note\": \"hello\", \"password\": \"pw123456789\"}\nlog", "top", 0, ["pw123456789"], true, P],
    // task (task-example; d2 unreadable() c:member-name:'password'; task example one object level down; TS differs but shows no run of the secret): d2dab4e8 gives {"k": "<redacted>"}
    ["01-task", "{\"note\": \"hello\", \"password\": \"pw123456789\"}\nlog", "obj", 1, ["pw123456789"], true, nest(1, P)],
    // gap-1-1000-obj-28 (gaps/gap1(F-6a)/obj; d2 unreadable() c:member-name:':"TAsession'): d2dab4e8 gives {"k": x28 levels ... "<redacted>"}
    ["02-gap-1-1000-obj-28", "{\"\":\"\"}" + " ".repeat(1000) + "\\\":\\\"TAsession\":TAILVF", "obj", 28, ["}\\\":\\\"TA"], true, nest(28, P)],
    // gap-1-1000-json-28 (gaps/gap1(F-6a)/json; d2 unreadable() c:member-name:':"TAsession'): d2dab4e8 gives "{\"k\":x28 levels ... \"<redacted>\"}..."
    ["03-gap-1-1000-json-28", "{\"\":\"\"}" + " ".repeat(1000) + "\\\":\\\"TAsession\":TAILVF", "json", 28, ["}\\\":\\\"TA"], true, jsonText(28, JSON.stringify(P))],
    // gap-3-1000-obj-28 (gaps/gap3(token)/obj; d2 unreadable() c:member-name:'token'): d2dab4e8 gives {"k": x28 levels ... "<redacted>"}
    ["04-gap-3-1000-obj-28", "{\"a\":1}" + " ".repeat(1001) + "\"token\":\"TAILV9A8B7C\"{", "obj", 28, ["a\":1} "], true, nest(28, P)],
    // gap-3-1000-json-30 (gaps/gap3(token)/json; d2 unreadable() c:member-name:'token'): d2dab4e8 gives "{\"k\":x30 levels ... \"<redacted>\"}..."
    ["05-gap-3-1000-json-30", "{\"a\":1}" + " ".repeat(1001) + "\"token\":\"TAILV9A8B7C\"{", "json", 30, ["a\":1} "], true, jsonText(30, JSON.stringify(P))],
    // gap-3-1000-obj-28 (gaps/gap3(token)/obj; d2 unreadable() c:member-name:'token'; shrunk: gap 1, one level): d2dab4e8 gives {"k": "<redacted>"}
    ["06-gap-3-1000-obj-28", "{\"a\":1}  \"token\":\"TAILV9A8B7C\"{", "obj", 1, ["a\":1} "], true, nest(1, P)],
    // matrix-F-6a-' '-1-5 (matrix/F-6a/obj; d2 unreadable() c:member-name:':"TAsession'; shrunk: depth 5 -> 1): d2dab4e8 gives {"k": "<redacted>"}
    ["07-matrix-F-6a-' '-1-5", "{\"\":\"\"}  \\\":\\\"TAsession\":TAILVF", "obj", 1, ["}\\\":\\\"TA"], true, nest(1, P)],
    // matrix-F-3a-' '-1-5 (matrix/F-3a/obj; d2 unreadable() c:member-name:'password'; shrunk: depth 5 -> 1): d2dab4e8 gives {"k": "<redacted>"}
    ["08-matrix-F-3a-' '-1-5", "{x \"password\" : \"MK5949289F\"", "obj", 1, ["x \"password\": \"MK59"], true, nest(1, P)],
    // matrix-F-4-'x'-1-5 (matrix/F-4/obj; d2 unreadable() c:member-name:'X-Api-Key'; shrunk: depth 5 -> 1, trailing spaces dropped): d2dab4e8 gives {"k": "<redacted>"}
    ["09-matrix-F-4-'x'-1-5", "{\"a\": true}x{\"X-Api-Key\":\"MK7AAE369C\"}", "obj", 1, ["true}{\"X-Api-Key"], false, nest(1, P)],
    // matrix-J1-'\n'-1-30 (matrix/J1/obj; d2 unreadable() c:member-name:'password'): d2dab4e8 gives {"k": x30 levels ... "<redacted>"}
    ["10-matrix-J1-'\\n'-1-30", "{\"password\": \"MK5949289F\"}\n {\"token\":\"TAILV77AB12\"}", "obj", 30, ["\"} {\"tok"], true, nest(30, P)],
    // matrix-J2-'x'-1-5 (matrix/J2/obj; d2 unreadable() c:member-name:'api_key'; shrunk: depth 5 -> 1): d2dab4e8 gives {"k": "<redacted>"}
    ["11-matrix-J2-'x'-1-5", "[{\"api_key\":\"MK12AB34\"}x] note \"pwd\":\"TAILV1A2B3C\"", "obj", 1, ["key\":\"MK12"], true, nest(1, P)],
    // matrix-J3-'x'-1-5 (matrix/J3/obj; d2 unreadable() c:member-name:'token'; shrunk: depth 5 -> 1): d2dab4e8 gives {"k": "<redacted>"}
    ["12-matrix-J3-'x'-1-5", "{\"token\": \"MK9F8E7D\"}x{\\\"pwd\\\":\\\"TAILVA1B2C3", "obj", 1, ["}{\\\"pwd"], true, nest(1, P)],
    // repro-F-6a-json-1-f (repros/F-6a/json; d2 unreadable() c:member-name:':"TAsession'): d2dab4e8 gives "{\"k\":\"<redacted>\"}"
    ["13-repro-F-6a-json-1-f", "{\"\":\"\"}" + " ".repeat(1030) + "\\\":\\\"TAsession\":TAILVF", "json", 1, ["}\\\":\\\"TA"], true, jsonText(1, JSON.stringify(P))],
    // repro-F-6a-obj-28-f (repros/F-6a/obj; d2 unreadable() c:member-name:':"TAsession'): d2dab4e8 gives {"k": x28 levels ... "<redacted>"}
    ["14-repro-F-6a-obj-28-f", "{\"\":\"\"}" + " ".repeat(1030) + "\\\":\\\"TAsession\":TAILVF", "obj", 28, ["}\\\":\\\"TA"], true, nest(28, P)],
    // repro-F-3a-obj-28-f (repros/F-3a/obj; d2 unreadable() c:member-name:'password'): d2dab4e8 gives {"k": x28 levels ... "<redacted>"}
    ["15-repro-F-3a-obj-28-f", "{x \"password\": \"MK5949289F\"", "obj", 28, ["x \"password\": \"MK59"], true, nest(28, P)],
    // repro-F-3a-json-1-f (repros/F-3a/json; d2 unreadable() c:member-name:'password'): d2dab4e8 gives "{\"k\":\"<redacted>\"}"
    ["16-repro-F-3a-json-1-f", "{x \"password\": \"MK5949289F\"", "json", 1, ["x \"password\": \"MK59"], true, jsonText(1, JSON.stringify(P))],
    // fz-4164-31609 (fz/obj/plain-name; d2 unreadable() c:member-name:'auth'): d2dab4e8 gives {"k": "<redacted>"}
    ["17-fz-4164-31609", "{\"auth\": ", "obj", 1, ["uth\":"], true, nest(1, P)],
    // fz-4161-52478 (fz/obj/name-with-junk; d2 unreadable() c:member-name:':[Authorization'; shrunk: depth 29 -> 1): d2dab4e8 gives {"k": "<redacted>"}
    ["18-fz-4161-52478", "{\\\":[Authorization\":", "obj", 1, ["{\\\":[Aut", "uthorizati"], true, nest(1, P)],
    // fz-4162-64361 (fz/obj/name-holds-json; d2 unreadable() c:member-name:'{"token": "MKC677B", "n": 1.5}'): d2dab4e8 gives {"k": "<redacted>"}
    ["19-fz-4162-64361", "{\"api_key\"\"{\\\"token\\\": \\\"MKC677B\\\", \\\"n\\\": 1.5}\":\\'data'18874400620\"MKE6AB05\"}:", "obj", 1, ["{\"token\": ", "ken\": \"MKC", "n\": 1.5}"], true, nest(1, P)],
    // fz-4163-43444 (fz/obj/spelled-pair; d2 unreadable() d:spelled-pairs): d2dab4e8 gives {"k": "<redacted>"}
    ["20-fz-4163-43444", "[\"TAILQ91DB\"]'token'{\\\":[", "obj", 1, ["TAILQ91DB", "TAILQ91D", "DB\"]'tok", "AILQ9"], true, nest(1, P)],
    // fz-4163-60572 (fz/json/plain-name; d2 unreadable() c:member-name:'Authorization'; shrunk: depth 28 -> 1): d2dab4e8 gives "{\"k\":\"<redacted>\"}"
    ["21-fz-4163-60572", "{\"Authorization\" :\"MK010DB367\"}:\"data\"", "json", 1, ["\"Auth"], false, jsonText(1, JSON.stringify(P))],
    // fz-4162-32543 (fz/json/name-with-junk; d2 unreadable() c:member-name:':["api_key"'; shrunk: depth 29 -> 1): d2dab4e8 gives "{\"k\":\"<redacted>\"}"
    ["22-fz-4162-32543", "{\\\":[\\\"api_key\\\"\": \"", "json", 1, ["api_key\""], true, jsonText(1, JSON.stringify(P))],
    // fz-4161-24442 (fz/json/name-holds-json; d2 unreadable() c:member-name:'{"key": "TAILQC664", "n": 1.5}'): d2dab4e8 gives "{\"k\":{\"k\":\"<redacted>\"}}"
    ["23-fz-4161-24442", "{\"data\"\"{\\\"key\\\": \\\"TAILQC664\\\", \\\"n\\\": 1.5}\": ", "json", 2, ["ata\"\"{\\\"ke", "{\\\"key\\\": ", ": \\\"TAILQC", "ata\"\"{\\\""], false, jsonText(2, JSON.stringify(P))],
    // fz-4162-48749 (fz/json/spelled-pair; d2 unreadable() d:spelled-pairs; shrunk: depth 29 -> 1): d2dab4e8 gives "{\"k\":\"<redacted>\"}"
    ["24-fz-4162-48749", "[\"TAILQ2D59D5D\"]'password'{\\\":[", "json", 1, ["AILQ2"], true, jsonText(1, JSON.stringify(P))],
    // fz-4163-68929 (fz/json/spelled-pair; d2 unreadable() d:spelled-pairs): d2dab4e8 gives "{\"k\":{\"k\":\"<redacted>\"}}"
    ["25-fz-4163-68929", "[\"MKB3060CD\"]MKE946FBAauthdata\\\":\\\"\"TAILQEA2BE507\"", "json", 2, ["ata\\\""], true, jsonText(2, JSON.stringify(P))],
  ];
  test.each(cases)("%s", (_id, leaf, frame, levels, secrets, framed, expected) => {
    expect(scrubValue(input(leaf, frame, levels), secrets, "", 0, false, undefined, indexPrivacyHints([]), framed)).toEqual(expected);
  });
});
