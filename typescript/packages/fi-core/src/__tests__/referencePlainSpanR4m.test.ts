import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// A registered value only adds redaction: the value shapes and the pair rule read the
// text as the catalog wrote it, before any registered value is replaced, and what they
// hide is joined with every registered-value occurrence into one rewrite. So no
// registered value can take out the context ('sk-proj-', 'password=', 'Bearer', a
// userinfo '@') that hides a credential, and no part of a registered value is left
// where a shorter or a cut one was replaced first.
// `d2` is what d2dab4e8 (generate_privacy_fixtures.py sha256 4dbcc945...) exports for
// scrubValue; the output here hides at least every character that output hides.
const cases = [
  {
    name: "userinfo lookup still reads the '@' after a registered value cut by a query value",
    input: "https://e.invalid/?token=Mwrw9Te4M1PLNT&safe=https://u:vAoX7CSNgvPLNT@b.invalid/yvdNv6T56oPLNT/UJBclwweKgPLNT",
    secrets: ["alid/?token=Mwrw9Te4M1PLNT&safe=https://u:vAoX7CSNgvPLNT"],
    hidden: ["alid/?token=", "Mwrw9Te4M1PLNT", "vAoX7CSNgvPLNT", "&safe="],
    kept: ["https://"],
    d2: "https://<redacted>@b.invalid/yvdNv6T56oPLNT/UJBclwweKgPLNT",
  },
  {
    name: "userinfo lookup in prose after a registered value cut by a query value",
    input: "log https://e.invalid/?%74oken=Mwrw9Te4M1PLNT&safe=https://u:vAoX7CSNgvPLNT@b.invalid/yvdNv6T56oPLNT next",
    secrets: ["alid/?%74oken=Mwrw9Te4M1PLNT&safe=https://u:vAoX7CSNgvPLNT"],
    hidden: ["alid/?%74oken=", "Mwrw9Te4M1PLNT", "vAoX7CSNgvPLNT", "&safe="],
    kept: ["log https://"],
  },
  {
    name: "two overlapping registered values in quoted prose are hidden as one",
    input: 'begin "https://e.invalid/?%74oken=&safe=qLBG3D7DPVPLNT&password=f5FDjxBcI2PLNT&tail=jr8tZIzREWPLNT" end',
    secrets: ["&safe=qLBG3D7DPVPLNT", "VPLNT&password=f5FDjxBcI2PLNT&tail="],
    hidden: ["&safe=", "qLBG3D7DPVPLNT", "&password=", "f5FDjxBcI2PLNT", "&tail="],
    kept: ['begin "https://e.invalid/?%74oken='],
    d2: 'begin "https://e.invalid/?%74oken=<redacted>',
  },
  {
    name: "two overlapping registered values over an encoded credential name",
    input: 'begin "https://e.invalid/?%74oken=&safe=qLBG3D7DPVPLNT&next=pass%77ord%3Df5FDjxBcI2PLNT&tail=jr8tZIzREWPLNT" end',
    secrets: ["&safe=qLBG3D7DPVPLNT", "VPLNT&next=pass%77ord%3Df5FDjxBcI2PLNT&tail="],
    hidden: ["&safe=", "qLBG3D7DPVPLNT", "&next=pass%77ord", "f5FDjxBcI2PLNT", "&tail="],
    kept: ['begin "https://e.invalid/?%74oken='],
  },
  {
    name: "a registered value over 'sk-pr' leaves the key shape hidden",
    input: "note PLNTpreabcdef sk-proj-AbCdEfGhIjKlMnOpQrStUvWx end",
    secrets: ["PLNTpreabcdef sk-pr"],
    hidden: ["PLNTpreabcdef", "AbCdEfGhIjKlMnOpQrStUvWx", "oj-AbCd"],
    kept: ["note ", " end"],
  },
  {
    name: "a registered value over 'pass' leaves the pair value hidden",
    input: "cfg PLNTpreabcdef password=AbCdEfGhIjKlMnOpQrStUvWx end",
    secrets: ["PLNTpreabcdef pass"],
    hidden: ["PLNTpreabcdef", "AbCdEfGhIjKlMnOpQrStUvWx"],
    kept: ["cfg "],
  },
];

describe("registered values only add redaction (pair rule and shapes read the catalog text)", () => {
  for (const item of cases) {
    for (const [api, scrub] of [["scrubValue", (x: string, s: string[]) => scrubValue(x, s, "")], ["scrubText", (x: string, s: string[]) => scrubText(x, s)]] as const) {
      test(`${api}: ${item.name}`, () => {
        const output = scrub(item.input, item.secrets) as string;
        for (const part of item.hidden) expect(output).not.toContain(part);
        for (const part of item.kept) expect(output).toContain(part);
      });
    }
  }
});

// The spec note's INVARIANT A reference shapes: registered values that end inside a
// credential's context. 0 characters of the credential value may be shown.
const lower = "abcdefghijklmnopqrstuvwxyz0123456789";
const mixed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
let state = 4242;
const randomChars = (length: number, alphabet: string) => {
  let result = "";
  for (let i = 0; i < length; i++) {
    state = (Math.imul(1664525, state) + 1013904223) >>> 0;
    result += alphabet[Math.floor((state / 2 ** 32) * alphabet.length)];
  }
  return result;
};
const templates: Record<string, (pre: string, value: string) => [string, string]> = {
  sk: (pre, value) => [`note ${pre} sk-proj-${value} end`, `${pre} sk-pr`],
  query: (pre, value) => [`log https://e.invalid/p?q=${pre}&api_key=${value}&b=1 end`, `${pre}&api`],
  pair: (pre, value) => [`cfg ${pre} password=${value} end`, `${pre} pass`],
  bearer: (pre, value) => [`hdr ${pre} Authorization: Bearer ${value} end`, `${pre} Author`],
  url: (pre, value) => [`log https://u:${value}@e.invalid/?%74oken=${value}&x ${pre} end`, `x ${pre}`],
};
const fillers = Array.from({ length: 300 }, () => "PLNTfill" + randomChars(8, lower));
const leaked = (output: string, value: string) => {
  for (let length = value.length; length >= 4; length--)
    for (let start = 0; start + length <= value.length; start++) if (output.includes(value.slice(start, start + length))) return length;
  return 0;
};

describe("INVARIANT A: a registered value over a credential's context", () => {
  for (const [kind, make] of Object.entries(templates)) {
    test(`${kind}: 0 credential characters shown (scrubValue and scrubText)`, () => {
      let shown = 0;
      for (let k = 0; k < 40; k++) {
        const pre = "PLNTpre" + randomChars(6, lower), value = randomChars(24, mixed);
        const [text, secret] = make(pre, value);
        const secrets = [...fillers, secret];
        shown += leaked(scrubValue(text, secrets, "") as string, value) + leaked(scrubText(text, secrets), value);
      }
      expect(shown).toBe(0);
    });
  }
});

describe("the joined read never refuses a text the reference's order reads", () => {
  test("many registered values that each take out a pair name, 262,144 characters", () => {
    const n = 262_144, chunk = "PLNTpre password=PLNTvalue ";
    const text = "cfg " + chunk.repeat(Math.floor((n - 4) / chunk.length));
    for (const scrub of [(x: string) => scrubValue(x, ["PLNTpre pass"], ""), (x: string) => scrubText(x, ["PLNTpre pass"])]) {
      const output = scrub(text) as string;
      expect(output).not.toContain("PLNTpre pass");
      // The joined read runs out of its budget, so the text fails closed: the whole text is the placeholder.
      expect(output).toBe("<redacted>");
    }
  });
});
