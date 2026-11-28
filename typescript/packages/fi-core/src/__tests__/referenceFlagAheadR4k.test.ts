import { describe, expect, test } from "@jest/globals";
import { scrubValue } from "../privacy-runtime";

// A credential flag without a value leaves the following flag to read its own value, on the same or the next line. A single dash
// starts that next flag only for a credential or undecided name followed by real whitespace and more text. Percent forms in its
// name are read once; a name that still has an encoded pair after that read is undecided. A double dash starts the next flag even
// for a name that is not a credential. The flag names that are kept retain their written spelling.

const VALUE = "PLNTq7Zr4kXw9";
const HIDDEN = "<redacted>";
const AT_CAP = `-%74oken${"%".repeat(120)}`;

// [case, input, reference output]
const HIDDEN_VALUES: [string, string, string][] = [
  ["minimal value", "--password -tok%65n VAL1", `--password -tok%65n ${HIDDEN}`],
  ["encoded first letter", `--password -%74oken ${VALUE}`, `--password -%74oken ${HIDDEN}`],
  ["encoded middle letter", `--password -tok%65n ${VALUE}`, `--password -tok%65n ${HIDDEN}`],
  ["encoded last letter", `--password -toke%6E ${VALUE}`, `--password -toke%6E ${HIDDEN}`],
  ["lowercase hex", `--password -to%6ben ${VALUE}`, `--password -to%6ben ${HIDDEN}`],
  ["uppercase hex and mixed case", `--password -To%6BeN ${VALUE}`, `--password -To%6BeN ${HIDDEN}`],
  ["all letters encoded", `--password -%74%6F%6B%65%6E ${VALUE}`, `--password -%74%6F%6B%65%6E ${HIDDEN}`],
  ["encoded underscore", `run --api-key -%61pi%5fkey ${VALUE} --verbose`, `run --api-key -%61pi%5fkey ${HIDDEN} --verbose`],
  ["encoded hyphen", `run --api-key -api%2Dkey ${VALUE} --verbose`, `run --api-key -api%2Dkey ${HIDDEN} --verbose`],
  ["single dash before the next flag", `-password -tok%65n ${VALUE}`, `-password -tok%65n ${HIDDEN}`],
  ["both names percent-spelled", `--pass%77ord -tok%65n ${VALUE}`, `--pass%77ord -tok%65n ${HIDDEN}`],
  ["double dash with an encoded middle letter", `--password --tok%65n ${VALUE}`, `--password --tok%65n ${HIDDEN}`],
  ["double dash with an encoded first letter", `-password --%74oKeN ${VALUE}`, `-password --%74oKeN ${HIDDEN}`],
  ["tabs before flag and value", `--password\t-tok%65n\t${VALUE} tail`, `--password\t-tok%65n\t${HIDDEN} tail`],
  ["single dash on a new line", `--token\n-%70assword ${VALUE}`, `--token\n-%70assword ${HIDDEN}`],
  ["single dash after CRLF and indentation", `--token\r\n\t-Tok%65N ${VALUE} tail`, `--token\r\n\t-Tok%65N ${HIDDEN} tail`],
  ["double dash on a new line", `--token\n--%70assword ${VALUE}`, `--token\n--%70assword ${HIDDEN}`],
  ["line break before the second flag's value", `--password -tok%65n\r\n\t${VALUE}`, `--password -tok%65n\r\n\t${HIDDEN}`],
  ["stray percents", `--token -%%token ${VALUE}`, `--token -%%token ${HIDDEN}`],
  ["encoded percent that leaves a stray percent", `--password -t%25oken ${VALUE}`, `--password -t%25oken ${HIDDEN}`],
  ["encoded pair remains after one read", `--password -tok%2565n ${VALUE}`, `--password -tok%2565n ${HIDDEN}`],
  ["undecided even if a second read would be harmless", `--password -nam%2565 ${VALUE}`, `--password -nam%2565 ${HIDDEN}`],
  ["plain credential flag", `--password -token ${VALUE}`, `--password -token ${HIDDEN}`],
  ["encoded gap is not a lookahead gap", `--password -tok%65n%20${VALUE}`, `--password ${HIDDEN}`],
  ["single dash at the name window limit", `--password ${AT_CAP} ${VALUE}`, `--password ${AT_CAP} ${HIDDEN}`],
];

const BOUNDARIES: [string, string, string][] = [
  ["single dash with a non-credential name", `--password -tok%65ns ${VALUE}`, `--password ${HIDDEN} ${VALUE}`],
  ["single dash with a non-credential name on a new line", `--token\n-tok%65ns ${VALUE}`, `--token\n${HIDDEN} ${VALUE}`],
  ["double dash with a non-credential name", `--password --n%61me ${VALUE}`, `--password --n%61me ${VALUE}`],
  ["double dash with a non-credential name on a new line", `--token\n--n%61me ${VALUE}`, `--token\n--n%61me ${VALUE}`],
  ["decoded name is a catalog exclusion", `--password -tok%65n_count ${VALUE}`, `--password ${HIDDEN} ${VALUE}`],
  ["encoded slash is not part of a flag name", `--password -tok%65n%2F ${VALUE}`, `--password ${HIDDEN} ${VALUE}`],
  ["single dash flag at end of text", "--password -tok%65n", `--password ${HIDDEN}`],
  ["single dash flag with only whitespace after it", "--password -tok%65n \t", `--password ${HIDDEN} \t`],
  ["encoded one-letter name", `--password -%74 ${VALUE}`, `--password ${HIDDEN} ${VALUE}`],
  ["single dash past the name window limit", `--password ${AT_CAP}% ${VALUE}`, `--password ${HIDDEN} ${VALUE}`],
];

/** Four-character runs still readable in the result, including ASCII percent escapes. */
function shownRuns(out: string, value: string): string[] {
  const runs = new Set<string>();
  for (let i = 0; i + 4 <= value.length; i++) runs.add(value.slice(i, i + 4));
  const shown = new Set<string>();
  for (let text = out;;) {
    for (const run of runs) if (text.includes(run)) shown.add(run);
    const decoded = text.replace(/%([0-9a-f]{2})/gi, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
    if (decoded === text) return [...shown];
    text = decoded;
  }
}

describe("a credential flag followed by another flag", () => {
  test.each(HIDDEN_VALUES)("hides the value: %s", (_name, input, expected) => {
    const out = scrubValue(input, [], "");
    expect(typeof out).toBe("string");
    expect(shownRuns(String(out), input.endsWith("VAL1") ? "VAL1" : VALUE)).toEqual([]);
    expect(out).toBe(expected);
  });

  test.each(BOUNDARIES)("keeps the reference flag boundary: %s", (_name, input, expected) => {
    expect(scrubValue(input, [], "")).toBe(expected);
  });
});
