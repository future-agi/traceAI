import { describe, expect, it } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// A text that begins as a JSON object or array and is not one JSON value (trailing prose, a second document, a stray quote or bracket).
// The d2dab4e8 Reference (spec/auto-instrumentation/generate_privacy_fixtures.py, Reference.unreadable(), version 6 round 3) makes the
// whole value the placeholder when such a text holds a credential name as a quoted member name (read as JSON reads names, so a \uXXXX
// form stands for what it spells), or holds more credential pairs once its JSON string forms are spelled out than as written. The text
// path keeps the text and redacts only the credential values and the known secrets each owner reads, so runs of a known secret that
// straddle an owner, or that overlap a second secret spelled apart in a JSON string, are left in the export.
// Expected values are the d2dab4e8 Reference outputs: Reference(privacy_catalog()).scrub(input, path, None, secrets, _depth=depth, framed=framed).

describe("referenceWholeValueR4i: a text that opens as JSON, is not one JSON value and holds a credential member name is the placeholder", () => {
  const cases: [id: string, input: string, secrets: string[], path: string, depth: number, framed: boolean, expected: unknown][] = [
    // --- prop-pw
    // task example: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\": \"hello\", \"password\": \"<redacted>\"}\nlog"
    // (not LESS: 5a7f9514 shows no run of the secret here; the example the task names, kept as the plain case of the rule)
    ["task example", "{\"note\": \"hello\", \"password\": \"pw123456789\"}\nlog", ["pw123456789"], "", 0, true, "<redacted>"],
    // prop1601-1027 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\":\"<redacted>K DArN8\",\"password\":\"<redacted>\"}\nlog"
    // runs 5a7f9514 shows that d2 hides: " DAr", "ArN8", "DArN", "K DA"
    ["prop1601-1027 (shrunk)", "{\"note\": \"8K\\\"R8MZr8X\\\"l/K DArN8\", \"password\": \"pw123456789\"}\nlog", ["X\"l/K DArN8","8K\"R8MZr8X\"l/"], "", 0, true, "<redacted>"],
    // prop1601-12014: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\":\"<redacted>M%/X V\",\"password\":\"<redacted>\"}\nlog"
    // runs 5a7f9514 shows that d2 hides: "M%/X"
    ["prop1601-12014", "{\"note\": \"wGqv😀/N€TM%/X V\", \"password\": \"pw123456789\"}\nlog", ["wGqv😀/N€T","/N€TM%/X"], "", 4, true, "<redacted>"],
    // --- prop-pwA
    // prop1601-441 #1 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\": \"<redacted>\\u00e9 pxx\", \"password\": \"<redacted>\"}\nlog"
    // runs 5a7f9514 shows that d2 hides: " pxx", "é px"
    ["prop1601-441 #1 (shrunk)", "{\"note\": \"LQ64F0FD13\\u00e9 pxx\", \"password\": \"pw123456789\"}\nlog", ["LQ64F0FD13","F0FD13é pxx"], "", 17, true, "<redacted>"],
    // prop1601-441 #2 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\":\"<redacted>7€/u\",\"password\":\"<redacted>\"}\nlog"
    // runs 5a7f9514 shows that d2 hides: "7€/u"
    ["prop1601-441 #2 (shrunk)", "{\"note\": \"/5I\\ud83d\\ude00\\\\y834q67\\u20ac/u\", \"password\": \"pw123456789\"}\nlog", ["34q67€/u","/5I😀\\y834q6"], "", 0, true, "<redacted>"],
    // --- prop-pw-L2
    // prop1601-2902 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\": \"hA 4D8ué450\\n%r5AA%\", \"password\": \"<redacted>\"}\nlog\nlog"
    // runs 5a7f9514 shows that d2 hides: "\n%r5", "%r5A", "0\n%r", "450\n"
    ["prop1601-2902 (shrunk)", "{\"note\": \"hA 4D8ué450\\n%r5AA%\", \"password\": \"pw123456789\"}\nlog\nlog", ["4D8ué450\n%r5AA%"], "", 31, true, "<redacted>"],
    // --- prop-pwA-L2
    // prop1601-11589 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"note\":\"6<redacted> 6EN\\n<redacted> XU\",\"password\":\"<redacted>\"}\nlog\nlog"
    // runs 5a7f9514 shows that d2 hides: "6EN\n"
    ["prop1601-11589 (shrunk)", "{\"note\": \"6EN\\n8G/80BFv15 6EN\\n8G/80BFey7HxaUK XU\", \"password\": \"pw123456789\"}\nlog\nlog", ["6EN\n8G/80BF","8G/80BFey7HxaUK","EN\n8G/80BFv15"], "", 1, true, "<redacted>"],
    // --- gaps-1 (F-6a)
    // gap-1-1000-param-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name ":\"TAsession" is a credential name); 5a7f9514 output "{\"\":\"\"}                                                                                                                                ...
    // runs 5a7f9514 shows that d2 hides: "\":\\\"", "\\\":\\"
    ["gap-1-1000-param-0", "{\"\":\"\"}" + " ".repeat(1000) + "\\\":\\\"TAsession\":TAILVF", ["}\\\":\\\"TA"], "", 0, true, "<redacted>"],
    // --- gaps-3
    // gap-3-1000-param-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "token" is a credential name); 5a7f9514 output "{\"a\":1}                                                                                                                                  ...
    // runs 5a7f9514 shows that d2 hides: "\":1}", ":1} ", "a\":1"
    ["gap-3-1000-param-0", "{\"a\":1}" + " ".repeat(1001) + "\"token\":\"TAILV9A8B7C\"{", ["a\":1} "], "", 0, true, "<redacted>"],
    // gap-3-1000-param-28 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "token" is a credential name); 5a7f9514 output "{\"a\":1} \"token\":\"<redacted>\"{"
    // runs 5a7f9514 shows that d2 hides: "\":1}", ":1} ", "a\":1"
    ["gap-3-1000-param-28 (shrunk)", "{\"a\":1} \"token\":\"TAILV9A8B7C\"{", ["a\":1} "], "", 28, true, "<redacted>"],
    // --- matrix J1
    // matrix-J1-' '-1-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"password\": \"<redacted>\"}  {\"token\":\"<redacted>\"}"
    // runs 5a7f9514 shows that d2 hides: " {\"t", "\"tok", "{\"to"
    ["matrix-J1-' '-1-0", "{\"password\": \"MK5949289F\"}  {\"token\":\"TAILV77AB12\"}", ["\"} {\"tok"], "", 0, true, "<redacted>"],
    // --- matrix J3
    // matrix-J3-' '-1-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "token" is a credential name); 5a7f9514 output "{\"token\": \"<redacted>\"} {\\\"pwd\\\":\\\"<redacted>\\\""
    // runs 5a7f9514 shows that d2 hides: "\"pwd", "\\\"pw", "{\\\"p"
    ["matrix-J3-' '-1-0", "{\"token\": \"MK9F8E7D\"} {\\\"pwd\\\":\\\"TAILVA1B2C3", ["}{\\\"pwd"], "", 0, true, "<redacted>"],
    // --- matrix F-4 (unframed)
    // matrix-F-4-' '-1-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "X-Api-Key" is a credential name); 5a7f9514 output "{\"a\": true} {\"X-Api-Key\":\"<redacted>\"}                       "
    // runs 5a7f9514 shows that d2 hides: "\"X-A", "-Api", "-Key", "Api-"
    ["matrix-F-4-' '-1-0", "{\"a\": true} {\"X-Api-Key\":\"MK7AAE369C\"}                       ", ["true}{\"X-Api-Key"], "", 0, false, "<redacted>"],
    // --- matrix F-3a
    // matrix-F-3a-' '-1-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{x \"password\" : \"<redacted>\""
    // runs 5a7f9514 shows that d2 hides: " \"pa", "\"pas", "assw", "ord\""
    ["matrix-F-3a-' '-1-0", "{x \"password\" : \"MK5949289F\"", ["x \"password\": \"MK59"], "", 0, true, "<redacted>"],
    // --- matrix F-6a
    // matrix-F-6a-'x'-1-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name ":\"TAsession" is a credential name); 5a7f9514 output "{\"\":\"\"}x \\\":\\\"TAsession\":\"<redacted>\""
    // runs 5a7f9514 shows that d2 hides: "\":\\\"", ":\\\"T", "\\\":\\", "\\\"TA"
    ["matrix-F-6a-'x'-1-0", "{\"\":\"\"}x \\\":\\\"TAsession\":TAILVF", ["}\\\":\\\"TA"], "", 0, true, "<redacted>"],
    // --- matrix J2 (marker run)
    // matrix-J2-'x'-1-0: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "api_key" is a credential name); 5a7f9514 output "[{\"api_<redacted>AB34\"}x] note \"pwd\":\"<redacted>\""
    // (LESS on a planted marker run only: "AB34")
    ["matrix-J2-'x'-1-0", "[{\"api_key\":\"MK12AB34\"}x] note \"pwd\":\"TAILV1A2B3C\"", ["key\":\"MK12"], "", 0, true, "<redacted>"],
    // --- repros F-6a
    // repro-F-6a-param-31-f: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name ":\"TAsession" is a credential name); 5a7f9514 output "{\"\":\"\"}                                                                                                                                ...
    // runs 5a7f9514 shows that d2 hides: "\":\\\"", "\\\":\\"
    ["repro-F-6a-param-31-f", "{\"\":\"\"}" + " ".repeat(1030) + "\\\":\\\"TAsession\":TAILVF", ["}\\\":\\\"TA"], "", 31, true, "<redacted>"],
    // --- repros F-3a (marker run)
    // repro-F-3a-param-29-f: d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{<redacted>49289F\""
    // (LESS on a planted marker run only: "289F", "4928", "9289")
    ["repro-F-3a-param-29-f", "{x \"password\": \"MK5949289F\"", ["x \"password\": \"MK59"], "", 29, true, "<redacted>"],
    // --- fz-name-plain
    // fz-4164-9911 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "password" is a credential name); 5a7f9514 output "{\"password\":\"<redacted>\"\"373F7\"}"
    // runs 5a7f9514 shows that d2 hides: "373F", "73F7"
    ["fz-4164-9911 (shrunk)", "{\"password\":\"\"\"373F7\"}", ["373F7"], "", 0, false, "<redacted>"],
    // fz-4163-27545 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "Authorizatio" is a credential name); 5a7f9514 output " {\"Au<redacted>\":"
    // runs 5a7f9514 shows that d2 hides: " {\"A", "{\"Au"
    ["fz-4163-27545 (shrunk)", " {\"Authorizatio\":", [" {\"Autho","thorizatio"], "", 0, true, "<redacted>"],
    // --- fz-name-glued
    // fz-4162-52543 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "secret\\:\"751891" is a credential name); 5a7f9514 output "[:\"secret\\:\\\"751891\":"
    // runs 5a7f9514 shows that d2 hides: "\"751", "1891", "5189", "7518"
    ["fz-4162-52543 (shrunk)", "[:\"secret\\:\\\"751891\":", [":\"751891"], "", 0, true, "<redacted>"],
    // fz-4163-2992 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "secret\"" is a credential name); 5a7f9514 output "[\"\"]\"secret\\\"\":"
    // runs 5a7f9514 shows that d2 hides: "cret", "ret\""
    ["fz-4163-2992 (shrunk)", "[\"\"]\"secret\\\"\":", ["cret\""], "", 0, false, "<redacted>"],
    // --- fz-name-literal
    // fz-4164-41946 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "{\"Authorization\": \"TAILQ02B80F357\", \"n\": 31415926535}" is a credential name); 5a7f9514 output "[\"MKDB050C\"] note\"{\\\"Authorization\\\": \\\"<redacted>\\\", \\\"n\\\": 31415926535}\" :"
    // runs 5a7f9514 shows that d2 hides: " \"n\"", "\"n\":", "050C", "B050"
    ["fz-4164-41946 (shrunk)", "[\"MKDB050C\"] note\"{\\\"Authorization\\\": \\\"TAILQ02B80F357\\\", \\\"n\\\": 31415926535}\" :", [" \"n\":"], "", 0, true, "<redacted>"],
    // fz-4161-16714 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): quoted member name "{\"k\": {\"k\": {\"Authorization\": \"MK1518A047\", \"n\": 1.5}}}" is a credential name); 5a7f9514 output "[\"x\"]\"{\\\"k\\\": {\\\"k\\\": {\\\"Authorization\\\": \\\"<redacted>\\\", \\\"n\\\": 1.5}}}\": "
    // runs 5a7f9514 shows that d2 hides: "\": \"", "atio", "ion\"", "n\": "
    ["fz-4161-16714 (shrunk)", "[\"x\"]\"{\\\"k\\\": {\\\"k\\\": {\\\"Authorization\\\": \\\"MK1518A047\\\", \\\"n\\\": 1.5}}}\": ", ["zation\": ","n\": \"MK1"], "", 28, true, "<redacted>"],
    // --- fz-spelled-pairs
    // fz-4164-71206 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): credential pairs once JSON string forms are spelled out: 1 > 0 on the raw text); 5a7f9514 output "[\"MKF3C7FB\"]\":\\,\"TAILQAE<redacted>et'{\\\":["
    // runs 5a7f9514 shows that d2 hides: "3C7F", "AILQ", "C7FB", "F3C7"
    ["fz-4164-71206 (shrunk)", "[\"MKF3C7FB\"]\":\\,\"TAILQAE81\"}'secret'{\\\":[", ["TAILQAE81","81\"}'secr"], "", 31, true, "<redacted>"],
    // fz-4162-61842 (shrunk): d2dab4e8 Reference output "<redacted>" (unreadable(): credential pairs once JSON string forms are spelled out: 1 > 0 on the raw text); 5a7f9514 output "[C<redacted>63404021{\\\":["
    // runs 5a7f9514 shows that d2 hides: "0402", "3404", "4021", "4040"
    ["fz-4162-61842 (shrunk)", "[C\"]'api_key'863404021{\\\":[", ["863404021","\"]'api_key'8"], "", 0, false, "<redacted>"],
  ];
  it.each(cases)("%s", (_id, input, secrets, path, depth, framed, expected) => {
    expect(scrubValue(input, secrets, path, depth, false, undefined, indexPrivacyHints([]), framed)).toEqual(expected);
  });
});
