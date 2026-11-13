// Fail-closed r4 witness controls, derived from the fail-closed r3 witnesses; they fail on the r3 code.
import { capBase64Images } from "../trace/trace-config/imageCap";

const BS = String.fromCharCode(92);
const P = "QUJD".repeat(40);
const WHOLE = "[image-redacted]";
const cap = (value: string, max = 64) => capBase64Images(value, max) as string;

test("transported non-final flag in a genuine MIME header is redacted", () => {
  const value = BS + "u0064ata:image/png;" + BS + 'u0062ase64;name="a' + BS + '" b",' + P;
  expect(cap(value)).not.toContain("QUJDQUJD");
});

test("transported final flag after a 1100-unit quoted parameter is redacted", () => {
  const value = BS + 'u0064ata:image/png;name="a' + BS + '" b' + "q".repeat(1100) + '";' + BS + "u0062ase64," + P;
  expect(cap(value)).not.toContain("QUJDQUJD");
});

test("Khmer invisible vowels in prefix and payload are removed before matching", () => {
  for (const z of [String.fromCharCode(0x17b4), String.fromCharCode(0x17b5)]) {
    expect(cap("d" + z + "ata:image/png;base64," + P)).not.toContain("QUJDQUJD");
    expect(cap("data:image/png;base64," + P + z + P)).not.toContain("QUJDQUJD");
  }
});

test("a value still changing after three passes is replaced whole", () => {
  expect(cap("%252522%25252541%252522")).toBe(WHOLE);
});

test("strict JSON keeps its syntax: only the matching string token is replaced", () => {
  const doc = '{"keep":"ordinary words","x":"' + BS + "u0064ata:image/png;" + BS + "u0062ase64;name=" + BS + '"a' + BS + '",' + P + '"}';
  const out = cap(doc);
  expect(out.startsWith('{"keep":"ordinary words","x":"')).toBe(true);
  expect(out).not.toContain("QUJDQUJD");
  expect(() => JSON.parse(out)).not.toThrow();
});

test("benign values are byte-identical", () => {
  for (const value of ["ordinary words", "see https://example.com/a,b?c=1", '{"a":"b","n":1}', "data:image/svg+xml,%3Csvg%3E",
    "data:image/png;base64,QUJD", "emoji \u{1F468}\u200D\u{1F469} family"]) expect(cap(value)).toBe(value);
});

test("default-ignorable split continuation is not left behind", () => {
  for (const code of [0x17b4, 0x17b5, 0x115f, 0x3164, 0xffa0, 0x1d173, 0xe0000]) {
    expect(cap("data:image/png;base64," + P + String.fromCodePoint(code) + P)).not.toContain("QUJDQUJD");
    expect(cap("d" + String.fromCodePoint(code) + "ata:image/png;base64," + P)).not.toContain("QUJDQUJD");
  }
});

test("backstop measures the payload, not an escaped header", () => {
  const url = 'data:image/png;name="sample";base64,' + P;
  for (const doc of [JSON.stringify("[" + url + "]"), JSON.stringify(JSON.stringify(url))]) {
    expect(cap(doc, url.length)).toBe(doc);
    expect(cap(doc, url.length + 1)).toBe(doc);
  }
  const out = cap(JSON.stringify(url), 64);
  expect(out).not.toContain("QUJDQUJD");
  expect(typeof JSON.parse(out)).toBe("string");
});
