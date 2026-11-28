import { afterEach, describe, expect, jest, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// A registered value only adds redaction, and the check that keeps it so stays linear and fails closed.
// 1. When the joined read (value shapes and pair rule on the text before any registered value is replaced) and the reference-order
//    read disagree both ways, the forced pieces are looked for in the reference-order output in linear time: four times the text
//    takes at most about five and a half times as long, and the search does not rescan that output once per piece.
// 2. When the joined read runs out of a budget, the text fails closed on its own: the whole text is the placeholder, never the
//    reference-order output with less hidden, and every other field of the same record is exported as it would be on its own.

const repeat = (n: number, chunk: string, pad = " ") => chunk.repeat(Math.floor(n / chunk.length)) + pad.repeat(n % chunk.length);
// The userinfo shape: a query value that holds a URL whose userinfo is n times "PLNTx".
const userinfo = (n: number) => "https://e.invalid/?token=V&safe=https://u:" + "PLNTx".repeat(n) + "@b.invalid/z";
const userinfoSecrets = ["alid/?token=V&safe=https://u:PLNTxPLNTxPLNTxPLNTx"];
// Many forced pieces: a pair whose name a registered value takes out precedes a key the value shapes hide in either read; one pair at
// the start makes the two reads disagree both ways; a long run of 's' is shown by both reads.
const pieceSeed = '": pwd="MKE58F711" end';
const manyPieces = (length: number) => {
  let text = pieceSeed + "\n\n" + ("s".repeat(63) + " ").repeat(Math.floor(length / 128)) + "\n\ncfg ";
  for (let i = 0; text.length < length; i++) text += "PLNTpre password=sk-" + i.toString(36).padStart(4, "0") + "q".repeat(16) + " ";
  return text;
};
const pieceSecrets = ["PLNTpre pass", 'pwd="MKE58F7'];
const apis: [name: string, run: (text: string, secrets: string[]) => unknown][] = [
  ["scrubText", (text, secrets) => scrubText(text, secrets)],
  ["scrubValue", (text, secrets) => scrubValue(text, secrets, "")],
];
const best = (run: () => unknown) => {
  let low = Infinity;
  for (let r = 0; r < 3; r++) { const started = process.hrtime.bigint(); run(); low = Math.min(low, Number(process.hrtime.bigint() - started) / 1e6); }
  return low;
};
// The least of two measurements of each 4x ratio (a noisy point is measured again once).
const ratios = (make: (n: number) => string, secrets: string[], run: (text: string, secrets: string[]) => unknown, sizes: number[]) => {
  const texts = sizes.map(make);
  run(texts[0], secrets);
  return texts.slice(1).map((text, i) => {
    let ratio = Infinity;
    for (let attempt = 0; attempt < 2 && ratio >= 5.5; attempt++) ratio = Math.min(ratio, best(() => run(text, secrets)) / Math.max(best(() => run(texts[i], secrets)), 1));
    return ratio;
  });
};

describe("forced pieces are looked for in linear time", () => {
  afterEach(() => { jest.restoreAllMocks(); });
  test.each(apis)("%s: the userinfo shape at 16k, 64k and 256k repeats", (_, run) => {
    for (const ratio of ratios(userinfo, userinfoSecrets, run, [16_000, 64_000, 256_000])) expect(ratio).toBeLessThan(5.5);
    const output = run(userinfo(1000), userinfoSecrets) as string;
    expect(output).not.toContain("PLNTx");
  }, 300_000);
  test.each(apis)("%s: many forced pieces at 80k, 320k and 1.28M characters", (_, run) => {
    for (const ratio of ratios(manyPieces, pieceSecrets, run, [80_000, 320_000, 1_280_000])) expect(ratio).toBeLessThan(5.5);
  }, 300_000);
  // A long shown run and forced pieces of more and more lengths (up to 30): the search reads the reference-order output a bounded
  // number of times however many lengths there are.
  const growingLengths = (length: number) => {
    const count = Math.min(30, Math.floor(length / 20_000));
    let text = pieceSeed + "\n\n" + ("s".repeat(63) + " ").repeat(Math.floor((length - 4096) / 64)) + "\n\ncfg ";
    for (let i = 0; i < count; i++) text += "PLNTpre password=sk-" + i.toString(36).padStart(4, "0") + "q".repeat(26 + i) + " ";
    return text;
  };
  test.each(apis)("%s: forced pieces of a growing number of lengths at 80k, 320k and 1.28M characters", (_, run) => {
    for (const ratio of ratios(growingLengths, pieceSecrets, run, [80_000, 320_000, 1_280_000])) expect(ratio).toBeLessThan(5.5);
    for (const length of [80_000, 1_280_000]) {
      const output = run(growingLengths(length), pieceSecrets) as string;
      for (const shown of ["PLNTpre pass", "sk-0", "qqqq", "MKE58F7"]) expect(output).not.toContain(shown);
    }
  }, 300_000);
  test.each(apis)("%s: the reference-order output is not searched once per forced piece", (_, run) => {
    // The characters every String includes() call with a 4+ character argument reads, at 16k and 64k characters.
    const scanned = (length: number) => {
      let read = 0;
      const includes = String.prototype.includes;
      const spy = jest.spyOn(String.prototype, "includes").mockImplementation(function (this: string, search: string, position?: number) {
        if (typeof search === "string" && search.length >= 4) read += this.length;
        return includes.call(this, search, position);
      });
      const output = run(manyPieces(length), pieceSecrets) as string;
      spy.mockRestore();
      for (const shown of ["PLNTpre pass", "sk-0", "qqqq", "MKE58F7"]) expect(output).not.toContain(shown);
      return read;
    };
    const small = scanned(16_000), large = scanned(64_000);
    expect(large).toBeLessThan(5.5 * small + 4 * 64_000);
  });
});

describe("which read is kept when the two reads disagree both ways", () => {
  // A forced piece of exactly four characters that the reference-order output shows: the text is written once with what either read
  // hides replaced.
  const four: [input: string, secrets: string[], expected: string][] = [
    ["\\\"api_key\\\"Bearer Bearer {\"secret\"=\": \"", ["er Bearer {\"sec", "\"secret\"=\": \""], "\\<redacted>"],
    [" : [\"TAILVA712\"]auth\"=a b,", ["TAILVA712\"]aut", "2\"]auth\"=a b,", "auth\"=a "], " : <omitted: incomplete JSON>"],
  ];
  test.each(apis)("%s: a shown forced piece of four characters is hidden", (_, run) => {
    for (const [input, secrets, expected] of four) expect(run(input, secrets)).toBe(expected);
  });
  // Forced pieces of 64 different lengths in a long text. Both reads only replace parts of the text with placeholders; their kept parts
  // ("word=", the rest of the pair name "password=" after the registered "PLNTpre pass"; both References show it, 58 times at 4k
  // characters, so it is kept: it is not part of a credential value) stand at more than one place, so a character counts as shown only where every way of reading the output shows it. The
  // text is written once with what either read hides replaced (no piece search is made): no forced piece and no registered value is
  // shown, at 4k, 16k and 64k characters alike, never the joined read with what only the other read hides shown.
  const manyLengths = (length: number) => {
    let text = pieceSeed + "\n\ncfg ";
    for (let i = 0; text.length < length; i++) text += "PLNTpre password=sk-" + i.toString(36).padStart(4, "0") + "q".repeat(16 + (i % 64)) + " ";
    return text;
  };
  test.each(apis)("%s: forced pieces of many lengths are hidden by the union of both reads", (_, run) => {
    for (const length of [4_000, 16_000, 64_000]) {
      const output = run(manyLengths(length), pieceSecrets) as string;
      expect(output).toMatch(/^<redacted>word=<redacted>word=<redacted>word=/);
      expect(output.split("<redacted>word=").join("")).toBe("<redacted>");
    }
  }, 120_000);
});

const crossing = (n: number) => "cfg " + repeat(n - 4, "PLNTpre password=PLNTvalue ");
const sparseCrossing = (n: number) => "cfg " + repeat(n - 4, "PLNTpre password=PLNTvalue " + " ".repeat(60));
const crossingSecrets = ["PLNTpre pass"];
const shownCount = (output: unknown) => typeof output === "string" ? (output.match(/PLNTvalue|PLNTpre|pre pass/g) ?? []).length : -1;

describe("a budget the joined read runs out of fails closed for that text alone", () => {
  test.each(apis)("%s: the 262,144-character crossing text shows no value and no registered character", (_, run) => {
    const output = run(crossing(262_144), crossingSecrets);
    expect(shownCount(output)).toBe(0);
    expect(output).toBe("<redacted>");
  }, 120_000);
  const family: [name: string, text: () => string][] = [
    ["crossing 300,000", () => crossing(300_000)],
    ["crossing 400,000", () => crossing(400_000)],
    ["crossing 440,000", () => crossing(440_000)],
    ["sparse crossing 1,048,576", () => sparseCrossing(1_048_576)],
  ];
  for (const [api, run] of apis) {
    test.each(family)(`${api}: %s shows no value and no registered character`, (_, text) => {
      const output = run(text(), crossingSecrets);
      expect(shownCount(output)).toBe(0);
      expect(output).toBe("<redacted>");
    }, 120_000);
  }
  test.each(apis)("%s: a crossing text the reference's order cannot read either is still refused", (_, run) => {
    expect(() => run(crossing(524_288), crossingSecrets)).toThrow("Privacy structure budget");
    expect(scrubText("hello world", crossingSecrets)).toBe("hello world");
  }, 120_000);
  test("sixteen crossing texts in one record each fail closed; the record's text budget still holds", () => {
    expect(scrubValue(Array.from({ length: 16 }, () => crossing(262_144)), crossingSecrets, "")).toEqual(Array(16).fill("<redacted>"));
    expect(() => scrubValue(Array.from({ length: 17 }, () => crossing(262_144)), crossingSecrets, "")).toThrow("Privacy text budget");
  }, 120_000);
  test("a shorter crossing text the joined read can read is still read in full", () => {
    const output = scrubText(crossing(200_000), crossingSecrets) as string;
    expect(shownCount(output)).toBe(0);
    expect(output.startsWith("cfg <redacted>word=<redacted>")).toBe(true);
  }, 120_000);
  test("the other fields of the same record are exported as they are on their own", () => {
    const record = { note: crossing(262_144), other: "see password=PLNTother1 next", plain: "hello world", list: ["a password=PLNTlist1 b", crossing(262_144)] };
    const output = scrubValue(record, crossingSecrets, "") as Record<string, unknown>;
    expect(output.note).toBe("<redacted>");
    expect(output.other).toBe(scrubValue("see password=PLNTother1 next", crossingSecrets, ""));
    expect(output.other).toBe("see password=<redacted>");
    expect(output.plain).toBe("hello world");
    expect(output.list).toEqual([scrubValue("a password=PLNTlist1 b", crossingSecrets, ""), "<redacted>"]);
    // Nothing is held over to the next call.
    expect(scrubText("hello world", crossingSecrets)).toBe("hello world");
    expect(scrubText("see password=PLNTnext1 now", [])).toBe(scrubText("see password=PLNTnext1 now", ["unrelated1"]));
  }, 120_000);
});
