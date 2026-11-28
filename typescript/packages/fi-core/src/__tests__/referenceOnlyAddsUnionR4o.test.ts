import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A registered value only adds redaction. Planted markers only; every host is under .invalid.

/** The 4-character runs of `marker` that `output` shows. */
const runsShown = (output: string, marker: string) => {
  const shown: string[] = [];
  for (let i = 0; i + 4 <= marker.length; i++) if (output.includes(marker.slice(i, i + 4))) shown.push(marker.slice(i, i + 4));
  return shown;
};

describe("an attribute that is one JSON string holding a URL, with a registered value over the URL's credential", () => {
  // Both References (d2dab4e8 and the in-tree 40c761f6) read the raw bytes with the URL catalog first and then the known secrets: the
  // userinfo is the placeholder and the scheme and the host stay. The attribute hides more than they do: the registered value the raw
  // bytes hold is hidden whole, so the part of it the catalog leaves (the scheme, or the start of the host) is hidden with the userinfo.
  test("a registered value from the scheme into the userinfo password", () => {
    const input = JSON.stringify("https://u:PLNTtQqvsbQehGgwZzXiEOV@e.invalid/p?safe=1");
    expect(scrubAttribute(input, ["https://u:PLNTtQqvsbQehGg"])).toBe('"<redacted>@e.invalid/p?safe=1"');
  });
  test("a registered value from the password tail into the host", () => {
    const input = JSON.stringify("https://u:PLNTHbrL4vuA9ijgSTTbS@e.invalid/p");
    expect(scrubAttribute(input, ["gSTTbS@e.inva"])).toBe('"https://<redacted>lid/p"');
  });

  // Userinfo, query, fragment and path credentials, with registered values over each part of the credential and its context.
  const shapes: Record<string, [string, string, string][]> = {
    userinfo: [["https://login:", "@edge.invalid/v1?safe=1", "login:"], ["http://reader:", "@host.invalid/p", "reader:"]],
    query: [["https://edge.invalid/v1?password=", "&safe=keep", "password="], ["http://host.invalid/v2?safe=keep&%70assword=", "&other=keep", "%70assword="]],
    fragment: [["https://edge.invalid/v1#password=", "&safe=keep", "password="], ["http://host.invalid/v2?safe=keep#view=all&%70assword=", "&other=keep", "%70assword="]],
    path: [["https://edge.invalid/a/password=", "/b?safe=keep", "password="], ["http://host.invalid/a;password%3D", ";safe=keep/b", "password%3D"]],
  };
  const cases: [string, string, string, string][] = [];
  Object.entries(shapes).forEach(([shape, variants], si) => variants.forEach(([before, after, name], vi) => {
    const marker = `PLNT${["mNprTV", "BxKzFQ", "DwLhSY", "CjGqJM"][si]}${["AceU8V2xyRK7", "OrbW6H9zfTX3"][vi]}`;
    const registered: Record<string, string> = {
      "scheme to value head": before + marker.slice(0, 7),
      "name to value head": name + marker.slice(0, 7),
      "end of name to value head": name.slice(-5) + marker.slice(0, 8),
      "value head": marker.slice(0, 9),
      "value interior": marker.slice(6, 16),
      "value tail into what follows": marker.slice(-9) + after.slice(0, 6),
    };
    for (const [mode, secret] of Object.entries(registered)) cases.push([`${shape} ${vi + 1}, ${mode}`, before + marker + after, secret, marker]);
  }));
  test.each(cases)("%s: no 4 characters of the credential are shown", (_, url, secret, marker) => {
    expect(runsShown(scrubAttribute(JSON.stringify(url), [secret]), marker)).toEqual([]);
    expect(runsShown(scrubValue(JSON.stringify(url), [secret], "") as string, marker)).toEqual([]);
    expect(runsShown(scrubText(JSON.stringify(url), [secret]), marker)).toEqual([]);
  });
});

describe("a registered value that runs from before a quoted credential name to its closing quote", () => {
  // The read in the reference's order and the read with the pair rule's spans joined each hide part of the pair; the text is written
  // once with what either hides replaced, so the pair's value is hidden.
  for (const [api, scrub] of [["scrubValue", (x: string, s: string[]) => scrubValue(x, s, "") as string], ["scrubText", scrubText], ["scrubAttribute", scrubAttribute]] as const) {
    test(`${api}: a registered value from a prefix through the name's closing quote`, () => {
      expect(scrub('" "PLNTpreqwHbDppassword": "Pd5khxE3pyIgKpaUnArl63Xy" and', ['PLNTpreqwHbDppassword"'])).toBe('" "<redacted> and');
    });
    test(`${api}: a registered value from inside the name through its closing quote`, () => {
      expect(scrub('" "password": "PLNTv5fkojueLH64" and', ['assword"'])).toBe('" "p<redacted> and');
    });
  }

  // The same shape with random prefixes and values and 300 other registered values (spec note section 2 probe).
  let state = 777;
  const randomChars = (length: number, alphabet: string) => {
    let s = "";
    for (let i = 0; i < length; i++) { state = (Math.imul(1664525, state) + 1013904223) >>> 0; s += alphabet[Math.floor(state / 2 ** 32 * alphabet.length)]; }
    return s;
  };
  const lower = "abcdefghijklmnopqrstuvwxyz0123456789", mixed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  const fillers = Array.from({ length: 300 }, () => "PLNTfill" + randomChars(8, lower));
  test("40 random pairs: 0 value characters shown (scrubValue and scrubText)", () => {
    const shown: string[] = [];
    for (let k = 0; k < 40; k++) {
      const pre = "PLNTpre" + randomChars(6, lower), value = randomChars(24, mixed);
      const text = `" "${pre}password": "${value}" and`, secrets = [...fillers, `${pre}password"`];
      for (const output of [scrubValue(text, secrets, "") as string, scrubText(text, secrets)]) if (runsShown(output, value).length) shown.push(output);
    }
    expect(shown).toEqual([]);
  });
});

describe("two overlapping registered values of which one runs past a replaced URL value", () => {
  // The two occurrences overlap and the catalog cuts one: both are hidden whole, so no run of either is left after the placeholder.
  const text = 'log {"m":": https://e.invalid/p#access_token=PLNTS3DvSvXDJaa9wB3Xu ="} end';
  const secrets = ["e.invalid/p#access_to", "token=PLNTS3DvSvXDJaa9wB3Xu "];
  for (const [api, scrub] of [["scrubValue", (x: string, s: string[]) => scrubValue(x, s, "") as string], ["scrubText", scrubText]] as const) {
    test(`${api}: no 4 characters of either registered value are shown`, () => {
      const output = scrub(text, secrets);
      for (const secret of secrets) expect(runsShown(output, secret)).toEqual([]);
      // The truncated JSON text holds no media field: it is omitted as incomplete JSON.
      expect(output).toBe("log <omitted: incomplete JSON>");
    });
  }
});

describe("two overlapping registered values of which the URL catalog cuts one, inside a URL token", () => {
  // Both occurrences are hidden whole with the replaced value, as one placeholder from the first of them.
  const cases: [string, string[], string][] = [
    ["& https://e.invalid/p?q=1&api_key=PLNTV8nrrx5WJ&b=2 log", ["5WJ&b=2 log", "://e.invalid/p?q=1&api_key=PLNT", "tps://e.invalid/p?q=1&api_key="], "& ht<redacted>"],
    ["= https://e.invalid/p#access_token=PLNTkZFbBi3NCLePU x", ["n=PLNTkZFbBi3NCL", "ttps://e.invalid/p#access_token=", "TkZFbBi3NCLePU x"], "= h<redacted>"],
  ];
  test.each(cases)("%s", (text, secrets, expected) => {
    expect(scrubText(text, secrets)).toBe(expected);
    expect(scrubValue(text, secrets, "")).toBe(expected);
  });
});

describe("two reads whose kept parts could stand at more than one place", () => {
  // A kept part that occurs more than once in the text could stand at more than one place, so a character counts as shown by a read
  // only where every way of reading its output shows it: no character either read hides is shown at a place the other read shows.
  const text = 'log {"m":": ghp_PLNTR2c4Zku64oXPLNTR2c4Zku64oX see = \\"password\\": \\"PLNT7JoQYfhXjZViYqv2HN\\" PLNTfillUWaC & sk-proj-PLNTYNTkWeaKTbYMb log"} end';
  const secrets = ["p_PLNTR2c4Zku64oXPLNT", '64oX see = "pas'];
  for (const [api, scrub] of [["scrubValue", (x: string, s: string[]) => scrubValue(x, s, "") as string], ["scrubText", scrubText]] as const) {
    test(`${api}: a token that repeats its value shows none of it`, () => {
      const output = scrub(text, secrets);
      // The values without the planted prefix, which "PLNTfillUWaC" shows.
      expect(runsShown(output, "R2c4Zku64oX")).toEqual([]);
      expect(runsShown(output, "7JoQYfhXjZViYqv2HN")).toEqual([]);
      expect(output).toBe('log {"m":": gh<redacted> PLNTfillUWaC & <redacted> log"} end');
    });
  }
  // A quoted password value whose words repeat after it: the kept words could stand inside the value or after it.
  const repeated = '" "password": "PLNTleft and PLNTcopy and PLNTright" and PLNTcopy and PLNTsecond tail';
  for (const [api, scrub] of [["scrubValue", (x: string, s: string[]) => scrubValue(x, s, "") as string], ["scrubText", scrubText], ["scrubAttribute", scrubAttribute]] as const) {
    test(`${api}: a value whose words repeat after it shows none of it`, () => {
      const output = scrub(repeated, ['assword"', "PLNTsecond"]);
      expect(runsShown(output, "PLNTleft and PLNTcopy and PLNTright")).toEqual([]);
      expect(output).toBe('" "p<redacted> tail');
    });
  }
});

describe("two reads of which one writes more than placeholders, neither hiding all the other hides", () => {
  // The joined read rewrites quotes, so it cannot be placed on the text; the reference-order read shows 4 characters of a part the
  // joined read forced, so the text is the placeholder.
  const cases: [string, string[]][] = [
    ['}["TAILVD9E0F93B"] pwd="]"="Authorization" password=', ["MK181385", '="Author', '9E0F93B"] pwd="]"']],
    ['\\":\\"key\\" password=a b,\\""=MK051B424C x \\true pwd=', ["MK051B424C", '\\"key\\" p']],
    ['"={{\\": pwd=client_secret\\"wd": "MK523BE86C56"} pwd=', ['": pwd=client_sec']],
  ];
  test.each(cases)("%s", (text, secrets) => {
    expect(scrubText(text, secrets)).toBe("<redacted>");
  });
});
