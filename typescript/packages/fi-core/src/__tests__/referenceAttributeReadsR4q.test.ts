import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A quoted JSON-string attribute is read twice: as the string it encodes, and on its raw bytes with the known secrets replaced first.
// A known secret replaced on the raw bytes can take part of a credential's name, so the pair rule no longer reads that pair. Whichever
// read is kept, scrubAttribute shows no character of the credential value that the read of the string hides, and no more of it than
// scrubValue shows on the same text.

/** The 4-character windows of `value` that `output` shows. */
const valueWindowsShown = (output: string, value: string) => {
  const shown: string[] = [];
  for (let i = 0; i + 4 <= value.length; i++) if (output.includes(value.slice(i, i + 4))) shown.push(value.slice(i, i + 4));
  return shown;
};

describe("a quoted JSON-string attribute whose raw bytes hold a registered value that cuts a credential pair's name", () => {
  test("a PEM, then an escaped quoted password pair, with three registered values", () => {
    const value = "PLNTt7ij25NH4ubZUGX9pRV4";
    const input = "\" start record: -----BEGIN PRIVATE KEY-----\\nPLNTcjRG2GncFURXOU74Aa6ZMrR2Rq\\nPLNTPpouJRvWG6kLpp56n2QgF4Ej\\n-----END PRIVATE KEY----- diagnostic \\\"password = " + value + "\\\" ; end note \"";
    const secrets = ["E KEY-----", "-- diagnosti", "stic \\\"pa"];
    expect(valueWindowsShown(scrubText(input, secrets), value)).toEqual([]);
    expect(valueWindowsShown(scrubValue(input, secrets), value)).toEqual([]);
    expect(valueWindowsShown(scrubAttribute(input, secrets), value)).toEqual([]);
    expect(scrubAttribute(input, secrets)).toBe("\" start record: <redacted>ssword = <redacted>\"");
  });

  // Pair names x layouts x the offset the registered value cuts the name at x how far before the name it starts x a longer registered
  // value that ends 1 to 3 characters inside it (so one read replaces the longer one first and keeps part of the cut) x a PEM prefix.
  const names = ["password", "secret", "token", "api_key", "access_token", "authorization"];
  const layouts: [string, (name: string, value: string) => string][] = [
    ["escaped quoted pair", (name, value) => "\\\"" + name + " = " + value + "\\\""],
    ["escaped JSON member", (name, value) => "{\\\"" + name + "\\\": \\\"" + value + "\\\"}"],
  ];
  const pem = "-----BEGIN PRIVATE KEY-----\\nPLNTcjRG2GncFURXOU74Aa6ZMrR2Rq\\nPLNTPpouJRvWG6kLpp56n2QgF4Ej\\n-----END PRIVATE KEY-----";
  const cases: { label: string; input: string; secrets: string[]; value: string }[] = [];
  let n = 0;
  for (const name of names) for (const [layout, pair] of layouts) for (let k = 1; k < name.length; k++) for (const before of [3, 5, 7]) for (const inside of [1, 2, 3]) for (const prefix of ["", pem + " "]) {
    const value = "PLNT" + (100000 + n++).toString(36) + "QzWx7Rk2Vb9Ty";
    const input = "\" start record: " + prefix + "diagnostic " + pair(name, value) + " ; end note \"";
    const at = input.indexOf(name, input.indexOf("diagnostic ")), start = at - before;
    const cut = input.slice(start, at + k), longer = input.slice(start + inside - cut.length - 3, start + inside);
    cases.push({ label: `${name} / ${layout} / cut at ${k} / ${before} before / ${inside} inside / ${prefix ? "PEM" : "plain"}`, input, secrets: prefix ? ["E KEY-----", longer, cut] : [longer, cut], value });
  }

  test("each registered value that cuts the name: scrubAttribute shows no value character that scrubValue hides", () => {
    const failures = cases.filter(c => valueWindowsShown(scrubAttribute(c.input, c.secrets), c.value).some(w => !valueWindowsShown(scrubValue(c.input, c.secrets), c.value).includes(w))).map(c => c.label);
    expect(failures).toEqual([]);
  });

  test("each registered value that cuts the name: the credential value is hidden whole where scrubValue hides it whole", () => {
    const failures = cases.filter(c => !valueWindowsShown(scrubValue(c.input, c.secrets), c.value).length && valueWindowsShown(scrubAttribute(c.input, c.secrets), c.value).length).map(c => c.label);
    expect(cases.length).toBe(2 * 3 * 3 * 2 * (7 + 5 + 4 + 6 + 11 + 12));
    expect(failures).toEqual([]);
  });
});

describe("a quoted JSON-string attribute where the raw-bytes read is taken and holds no credential", () => {
  test("what either read hides is hidden, and the text neither read hides is kept", () => {
    const input = "\" start record: -----BEGIN PRIVATE KEY-----\\nPLNTcjRG2GncFURXOU74Aa6ZMrR2Rq\\n-----END PRIVATE KEY----- diagnostic \\\"status = ok\\\" ; end note \"";
    const secrets = ["E KEY-----", "-- diagnosti", "stic \\\"st"];
    expect(scrubAttribute(input, secrets)).toBe("\" start record: <redacted>atus = ok\\\" ; end note \"");
    expect(scrubAttribute(input, secrets)).toBe(scrubValue(input, secrets));
  });
});

describe("a quoted JSON-string attribute where the read of the string is kept and the raw-bytes read hides more", () => {
  // The read of the string shows no run of a registered value more often than the raw-bytes read, so it is kept; what the raw-bytes
  // read hides besides (the rest of a URL fragment or query after a credential, a flag's value) is added to it by position.
  test("a URL fragment api%5Fkey whose registered value runs from the scheme into the credential", () => {
    const input = "\"https://e.invalid/x?safe=PLNTSsGPdqu9QyEjjuoMd9MXKk#tag=ok&api%5Fkey=PLNToS3QUdkCzqciObErRP&keep=PLNTBezRfLjeZQWnYbOJ7B4n\"";
    const secrets = ["https://e.invalid/x?safe=PLNTSsGPdqu9QyEjjuoMd9MXKk#tag=ok&api%5Fkey=PLNToS", "&keep=PLNTBezRf"];
    // The registered value the raw bytes hold is hidden whole, though the catalog cuts it at the credential.
    expect(scrubAttribute(input, secrets)).toBe("\"<redacted>\"");
  });
  test("a URL query api_key with a registered value inside the credential", () => {
    const input = "\"https://e.invalid/x?safe=PLNT0UvtLrMwEDHXfJOhlJ&api_key=PLNTlUjte6mlvdEKgpvxgLyPEV&keep=PLNTKduFpH83dShcOu3#anchor\"";
    const secrets = ["LNTlUjte6mlvdEKgpvxgLyP", "tps://e.in"];
    expect(scrubAttribute(input, secrets)).toBe("\"ht<redacted>valid/x?safe=PLNT0UvtLrMwEDHXfJOhlJ&api_key=<redacted>\"");
  });
  test("an escaped-tab api-key flag whose registered value runs over the flag's value", () => {
    const input = "\"PLNTpZsSJE7amYKOkyQhH\\t--api-key\\tPLNTWguEcvOu1i --control PLNT4NlrA5VI3SPIrLWTna\"";
    const secrets = ["H\t--api-key\tPLNTWguEcvOu", "cvOu1i --c"];
    expect(scrubAttribute(input, secrets)).toBe("\"PLNTpZsSJE7amYKOkyQh<redacted> PLNT4NlrA5VI3SPIrLWTna\"");
  });
  test("a registered value that ends at an escaped backslash: its head is hidden too", () => {
    const input = "\"ka'm9Sp\\u005C37p5\"", secrets = ["'m9Sp\\37p5", "ka'm9Sp\\"];
    expect(scrubAttribute(input, secrets)).toBe("\"<redacted>\"");
  });
});

describe("a quoted JSON-string attribute whose raw-bytes read cannot be placed in it", () => {
  // The read of the string writes the escapes again as JSON writes them, so its kept parts do not stand in the attribute. The raw-bytes
  // read hides every character the read with no registered value hides and shows nothing it does not show, read as the strings they
  // encode: it is kept, as scrubValue and the reference export it (the second registered value is spelled only as the decoded string
  // spells it, so neither replaces it on the raw bytes).
  test("a registered value spelled with an astral character and a tab, escaped in the attribute", () => {
    const input = "\n\"jfFRNgQqM\\ud83d\\udd11j\\tDDxZ+\"", secrets = ["jfFRNgQqM", "gQqM\u{1F511}j\tDDxZ+"];
    expect(scrubAttribute(input, secrets)).toBe("\n\"<redacted>\\ud83d\\udd11j\\tDDxZ+\"");
    expect(scrubAttribute(input, secrets)).toBe(scrubValue(input, secrets));
  });
  test("a credential pair after an escape: the raw-bytes read is kept only where it shows nothing the read with no registered value hides", () => {
    const input = "\"note \\u0041 \\\"password = PLNTq7Wx2Rk9Vb3Ty\\\" diagnostic\"", secrets = ["note \\u0041 \\\"pa"];
    expect(valueWindowsShown(scrubAttribute(input, secrets), "PLNTq7Wx2Rk9Vb3Ty")).toEqual([]);
  });
});

describe("a quoted JSON-string attribute whose reads cannot be placed in it, where the raw-bytes read is taken", () => {
  // An escape the read of the string writes again (\\/, \\u0041) leaves the reads unplaced in the raw bytes. They are joined as the
  // strings they encode: the raw-bytes read, the read with no registered value, and every occurrence of a registered value in either
  // spelling; so a registered value spelled as the string spells it is hidden too.
  test("a URL whose escaped solidus leaves the reads unplaced: the userinfo the raw-bytes read shows is hidden", () => {
    expect(scrubAttribute("\"https:\\//PLNT0N4rtZCI2X:PLNTpDwicsglg8xVjmAWkYTQ@example.invalid/x?mode=none\"", ["AWkYTQ@examp","8xVjmAWkYTQ@example.inv"])).toBe("\"https://<redacted>alid/x?mode=none\"");
  });
  test("a URL with escaped solidi: a registered value spelled as the string spells it is hidden with what the raw-bytes read hides", () => {
    expect(scrubAttribute("\n  \"detail \\\\\\n https:\\/\\/example.invalid\\/x?mode=debug#tag=ok&access_token=PLNT0WKExLjXJ8uL5KjfrGRx&keep=world ;  \\u00e9 fin\"", ["mode=debug#ta","//example.invalid/x?mod"])).toBe("\"detail \\\\\\n https:<redacted>g=ok&access_token=<redacted>\"");
  });
  test("escaped solidus in a URL path: read as the strings they encode, the raw-bytes read is kept", () => {
    expect(scrubAttribute("\n  \"http://PLNTvu2qxc4hbi:PLNTXN4xfb48yG1hotaA5cMS@example.invalid\\/x?mode=none\"\n", ["@example.invalid\\/x?mode","4hbi:PLNTXN4xfb48yG1hotaA5cMS@exampl","1hotaA5cMS@example.i"])).toBe("\n  \"http://<redacted><redacted>=none\"\n");
  });
  test("a token query after an escaped solidus: the token the raw-bytes read shows is hidden", () => {
    expect(scrubAttribute("\"diagnostic https:\\//example.invalid\\/x?safe=ready&token=PLNTdvkPUniR4NmlbbvIc0LX&keep=world#anchor ; end note\"", ["s://example.invalid/x?safe=re","diagnostic https:"])).toBe("\"<redacted>ady&token=<redacted>\"");
  });
});
