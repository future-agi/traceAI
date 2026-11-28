import { describe, expect, test } from "@jest/globals";
import { scrubAttribute } from "../privacy-runtime";

// A quoted JSON-string attribute (a JSON text that is one string) given registered values, where one read of it cannot be joined by
// position with the others: it is kept only where it shows nothing another read hides. Planted markers only.

/** The attribute's output read as the string it encodes. */
const decoded = (output: string) => JSON.parse(output) as string;

describe("a kept read where another read hides a run under four characters", () => {
  // The read with no registered value hides the one-character value of "secret" (and the read in the reference's order hides the
  // registered value in "note"); the kept read writes the JSON text again and hides both, so it is kept as it is.
  test("a one-character JSON value under a credential key", () => {
    const input = "\"{\\\"secret\\\": \\\"O\\\", \\\"note\\\": \\\"PLNTR8sj4snRewC1GOj0\\\"}\"";
    const output = scrubAttribute(input, ["NTR8sj4snRewC1GOj", "\"note\": \"PLNTR8s"]);
    expect(output).toBe("\"{\\\"secret\\\":\\\"<redacted>\\\",\\\"note\\\":\\\"PL<redacted>0\\\"}\"");
    expect(decoded(output)).not.toContain("\"O\"");
  });
  test("a one-character JSON value under the authorization key, with whitespace around the root", () => {
    const input = "\n \t\"reported {\\\"authorization\\\": \\\"k\\\", \\\"note\\\": \\\"PLNTLlhvrQ0ARe95FPso\\\"} / done\"\t\n";
    const output = scrubAttribute(input, ["0ARe95FP"]);
    expect(output).toBe("\"reported {\\\"authorization\\\":\\\"<redacted>\\\",\\\"note\\\":\\\"PLNTLlhvrQ<redacted>so\\\"} / done\"");
    expect(decoded(output)).not.toContain("\"k\"");
  });
});

describe("the reads of a quoted attribute where they cannot be joined as the strings they encode", () => {
  // The registered value is spelled only in the string (the raw bytes write its "p" as the escape \u0070): the read of the string writes the
  // JSON text again, the read in the reference's order hides the start of the text, and the read with no registered value hides the
  // pair's value. Nothing of the value is shown.
  test("a registered value spelled only in the string, over a JSON text and a pair name", () => {
    const output = scrubAttribute("\"{\\\"m\\\": \\\"\\u0070wd=x\\\", \\\"k\\\": 1}\"", ["{\"m\": \"pwd"]);
    expect(output).toBe("\"\\\"<redacted>\\\"\"");
    expect(decoded(output)).not.toMatch(/=x/);
  });
  // The raw bytes hold the registered value. The read of the string (kept) writes the JSON text again and hides the pair's value;
  // the reads in the reference's order show the value after the registered part of its name, and the read with no registered value
  // hides it: the kept read shows nothing they hide, and is kept.
  test.each([
    ["a registered value over the end of a pair name", "\"{\\\"m\\\": \\\"log token=x; next=1\\\", \\\"k\\\": 1}\"", ["og token"], "\"{\\\"m\\\":\\\"l<redacted>=<redacted>; next=1\\\",\\\"k\\\":1}\""],
    ["a registered value that is the whole pair name", "\"{\\\"m\\\": \\\"passcode=Qx; next=1\\\", \\\"k\\\": 1}\"", ["passcode"], "\"{\\\"m\\\":\\\"<redacted>=<redacted>; next=1\\\",\\\"k\\\":1}\""],
  ])("%s", (_, input, registered, expected) => {
    const output = scrubAttribute(input, registered);
    expect(output).toBe(expected);
    expect(decoded(output)).toContain("=<redacted>; next=1");
  });
});

describe("a union written on the raw bytes that hides part of an escape", () => {
  // The registered "api.invalid" and the URL catalog hide the backslash of "\/" after the host, not the "/": the whole escape is hidden,
  // and the whitespace after the root's closing quote is kept.
  test("an escaped slash after a registered host", () => {
    const input = "\"update --token=\\\"PLNTnZFheV4lw0Ea1IntfY2V\\\"; wss://api.invalid\\/read?token=PLNT6YxLCRNGO66nAvrbJHps&note=PLNTkvNXXKYMDk1MHNz4#token=PL\\u004eTJgL0oA2vUuWq0XsJ1Niw; sk-proj-PLNT32bbKgUaOGD2Wh2IYqfWRh28\" ";
    expect(scrubAttribute(input, ["api.invalid"])).toBe("\"update --token=<redacted>; wss://<redacted>read?token=<redacted>\" ");
  });
});

describe("a kept read that writes a JSON text again, where another read hides a run under four characters", () => {
  // The kept read writes the JSON text inside the string again, with each JSON escape written as its character (\\u0071 as q); the
  // dropped read keeps the escape as spelled. The one- to three-character value is hidden by both; the kept read is kept as it is.
  test.each([
    ["a JSON escape in a value the kept read writes as its character",
      "\"[\\u0050LNTuKq44YmD] {  \\\"m\\\"  :  \\\"PLNTuKq44YmD a\\\\u0063cess_token=PLN; next=PLNTcpBpx9FMu\\\\u0071cAFfQ\\\",  \\\"z\\\"  :  \\\"PLNTyNAYye5h4M7eTUw\\\"  }\" ",
      ["PLNTuKq44YmD access_token"],
      "\"[PLNTuKq44YmD] {\\\"m\\\":\\\"<redacted>=<redacted>; next=PLNTcpBpx9FMuqcAFfQ\\\",\\\"z\\\":\\\"PLNTyNAYye5h4M7eTUw\\\"}\"", "=PLN;"],
    ["a JSON escape in a key the kept read writes as its character",
      "\n\t\"event {  \\\"password\\\"  :  \\\"P\\\",  \\\"note\\\"  :  \\\"PLNTzX7\\\\u0075HNk4afsb9ax é\\\",  \\\"\\\\u0073afe\\\"  :  \\\"💡\\\"  } PLNTYUWvcejR\" ",
      ["PLNTzX7uHNk4afsb9ax", "PLNTEBGCRSAQZ6VREdf"],
      "\"event {\\\"password\\\":\\\"<redacted>\\\",\\\"note\\\":\\\"<redacted> é\\\",\\\"safe\\\":\\\"💡\\\"} PLNTYUWvcejR\"", ":\"P\""],
    ["an escaped key name over a two-character value",
      "\n\t\"log:\\n{ \\\"authorizati\\\\u006fn\\\" : \\\"PL\\\", \\\"note\\\" : \\\"PLNTXxXAkp9aBMX8\\\\u0032Es é\\\", \\\"safe\\\" : \\\"PLNTEEMWsf93\\\" }\\n done\" ",
      ["PLNTXxXAkp9aBMX82Es", "PLNTfQQTbDHCUJrvv86"],
      "\"log:\\n{\\\"authorization\\\":\\\"<redacted>\\\",\\\"note\\\":\\\"<redacted> é\\\",\\\"safe\\\":\\\"PLNTEEMWsf93\\\"}\\n done\"", ":\"PL\""],
  ])("%s", (_, input, registered, expected, short) => {
    expect(scrubAttribute(input, registered)).toBe(expected);
    expect(decoded(scrubAttribute(input, registered))).not.toContain(short);
  });
});
