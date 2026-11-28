import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubValue } from "../privacy-runtime";

// A quoted JSON-string attribute (a JSON text that is one string) with SEVERAL registered values, each taking part of the neighbourhood
// of a value shape, a credential pair's name or value, or an escape: scrubAttribute hides every planted character that both References
// hide, and every one that scrubValue hides on the same text with the same registered values where scrubValue does not hide the whole
// string; its output is still one JSON string. A registered value only adds redaction: the raw bytes read in the References' order (the
// URL catalog, then the registered values, then the value shapes and the pair rule) is joined by position with the read of the string.

/** What an output shows: the output, the string it encodes where it is one JSON string, and the output with its escapes read; each split
 * at the placeholders. */
const views = (output: string): string[] => {
  const all = [output];
  try { const node: unknown = JSON.parse(output); if (typeof node === "string") all.push(node); } catch { /* not JSON */ }
  all.push(output.replace(/\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g, (whole, hex, ch) => hex ? String.fromCharCode(parseInt(hex, 16)) : JSON.parse(`"\\${ch}"`)));
  return all.flatMap(view => view.split(/<redacted>|<omitted[^<>\n]*>/));
};
/** Every PLNT run in the input and in the string it encodes. */
const plants = (input: string): string[] => {
  const texts = [input];
  try { const node: unknown = JSON.parse(input); if (typeof node === "string") texts.push(node); } catch { /* not JSON */ }
  return [...new Set(texts.flatMap(text => text.match(/PLNT[0-9A-Za-z]{4,}/g) ?? []))];
};
/** The 4-character windows of `values` that `output` shows. */
const windowsShown = (output: string, values: string[]): Set<string> => {
  const parts = views(output), shown = new Set<string>();
  for (const value of values) for (let i = 0; i + 4 <= value.length; i++) if (parts.some(part => part.includes(value.slice(i, i + 4)))) shown.add(value.slice(i, i + 4));
  return shown;
};
const isJSONString = (output: string) => { try { return typeof JSON.parse(output) === "string"; } catch { return false; } };
/** scrubValue hides the whole string: the placeholder, bare or quoted, with only JSON whitespace around it. */
const wholeValue = (output: string) => /^[ \t\r\n]*"?<redacted>"?[ \t\r\n]*$/.test(output);
/** Windows scrubAttribute shows that scrubValue (same text, same registered values) hides. */
const aboveValue = (input: string, registered: string[]): string[] => {
  const values = plants(input), attr = windowsShown(scrubAttribute(input, registered), values), value = windowsShown(scrubValue(input, registered), values);
  return [...attr].filter(window => !value.has(window)).sort();
};

/** The review/A row: three registered values, each overlapping the neighbourhood of a flag pair's value and a ghp_ value. Both References
 * show the ghp_ value's middle and tail (`REVIEW_REFERENCE`, the d2dab4e8 and in-tree outputs, identical); scrubValue hides the whole string
 * (a registered value, as given, matches the raw bytes from inside the escape for a backslash). */
const REVIEW_ROW = {"id": "review/A", "input": "\r\n\"n --secret PLNT6GLLBF24VMF8WT\\\\ ghp_PLNTY97TKNQN6LYA3S26PLNTQG8BRCBDKZ32FNEQY + ; ordinary update remains\"  \t\r\n", "registered": ["secret PLNT6GLLB", "NQN6LYA3S26PLNT", "\\ ghp_PL"]};
const REVIEW_REFERENCE = "\r\n\"n --<redacted>F24VMF8WT\\<redacted>NTY97TK<redacted>QG8BRCBDKZ32FNEQY + ; ordinary update remains\"  \t\r\n";

/** Generated rows (gen_r4t.py) whose planted windows `windows` both References hide (d2dab4e8 generate_privacy_fixtures.py sha256
 * 4dbcc945ff312342e50baaec7e5b7e6dc6985281381f492a32da49369b5192c6 and the in-tree 40c761f6, sha256
 * 20a4e2840ace069714abd04a5cd394162c2c3812e3ae3bc3c808212061f32117): a registered value ends at a pair value's escaped closing quote,
 * so the References read the value to the end. */
const BOTH_ROWS: { id: string; input: string; registered: string[]; windows: string[] }[] = [
  {"id": "R4T-14496", "input": "\"\\u006e \\u2003--secret PLNTgnc9WbGWyjj7\\\\ ghp_PLNT4bMjPSFe8aqp5Mex9UQPLgKwFHNmVAMR\\u2003 + ; AKIAPLNTNBCB3MDV4PMS ordinary update remains\\u2003\"", "registered": ["VAMR\u2003 + ; AKIAP", "\\ ghp_PLNT", "n \u2003--secre"], "windows": ["4bMj", "5Mex", "8aqp", "9UQP", "FHNm", "Fe8a", "KwFH", "LgKw", "Mex9", "MjPS", "PLgK", "PSFe", "QPLg", "SFe8", "UQPL", "aqp5", "bMjP", "e8aq", "ex9U", "gKwF", "jPSF", "p5Me", "qp5M", "wFHN", "x9UQ"]},
  {"id": "R4T-14568", "input": "\"n \u00e9\\\"access_token\\\": \\\"PLNTF6fcXUa8K2gE\\\" xoxb-817774-PLNTDXPPSFcXYJGkdreYRQNf\u00e9 + ; eyJPLNTtaxh7ybJaL.eyJPLNT6WDiaQdfeU.PLNTfJShSn7YiKmG ordinary update remains\u00e9\"", "registered": ["RQNf\u00e9 + ; eyJPL", "\" xoxb-817", "n \u00e9\"access_"], "windows": ["DXPP", "FcXY", "Gkdr", "JGkd", "LNTD", "NTDX", "PLNT", "PPSF", "PSFc", "SFcX", "TDXP", "XPPS", "XYJG", "YJGk", "cXYJ", "dreY", "kdre"]},
  {"id": "R4T-14576", "input": "\"\\u006e \\u00e9\\\"access_token\\\": \\\"PLNTcJsPGWLcmyFa\\\" xoxb-996528-PLNTQHS4PTQmiFM25EupGow5\\u00e9 + ; eyJPLNTAM26Ze6FMS.eyJPLNTQFngZosTN6.PLNTnpZVdugDm4DH ordinary update remains\\u00e9\"", "registered": ["Gow5\u00e9 + ; eyJPL", "\" xoxb-996", "n \u00e9\"access_"], "windows": ["25Eu", "4PTQ", "5Eup", "FM25", "HS4P", "LNTQ", "M25E", "NTQH", "PLNT", "PTQm", "QHS4", "QmiF", "S4PT", "TQHS", "TQmi", "iFM2", "miFM"]},
  {"id": "R4T-14584", "input": "\r\n\"n \u00e9\\\"access_token\\\": \\\"PLNTow3RPrKrHAgQ\\\" xoxb-802898-PLNTv8N3bnbfwyBHbt2Rfvza\u00e9 + ; eyJPLNTtHPU3eFFGc.eyJPLNTejthJXJ8cX.PLNTqhVcVBPUsoex ordinary update remains\u00e9\"  \t\r\n", "registered": ["fvza\u00e9 + ; eyJPL", "\" xoxb-802", "n \u00e9\"access_"], "windows": ["3bnb", "8N3b", "BHbt", "Hbt2", "LNTv", "N3bn", "NTv8", "PLNT", "Tv8N", "bfwy", "bnbf", "bt2R", "fwyB", "nbfw", "v8N3", "wyBH", "yBHb"]},
];
/** Generated rows whose planted windows `windows` scrubValue hides (not the whole string) and scrubAttribute showed. */
const VALUE_ROWS: { id: string; class: string; input: string; registered: string[]; windows: string[] }[] = [
  {"id": "R4T-14496", "class": "escaped/flag/pair-shape", "input": "\"\\u006e \\u2003--secret PLNTgnc9WbGWyjj7\\\\ ghp_PLNT4bMjPSFe8aqp5Mex9UQPLgKwFHNmVAMR\\u2003 + ; AKIAPLNTNBCB3MDV4PMS ordinary update remains\\u2003\"", "registered": ["VAMR\u2003 + ; AKIAP", "\\ ghp_PLNT", "n \u2003--secre"], "windows": ["4bMj", "5Mex", "8aqp", "9UQP", "FHNm", "Fe8a", "KwFH", "LgKw", "Mex9", "MjPS", "PLgK", "PSFe", "QPLg", "SFe8", "UQPL", "aqp5", "bMjP", "e8aq", "ex9U", "gKwF", "jPSF", "p5Me", "qp5M", "wFHN", "x9UQ"]},
];

/** The r4t family: gen_r4t.py, ported (family/gen_r4t_port.cjs); the same 23,040 cases. */
function familyR4t(): { id: number; input: string; registered: string[]; cls: Record<string, string | number> }[] {
  let state = 41871 >>> 0;
  const next = (): number => {
    state = (state + 0x6D2B79F5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1) >>> 0;
    t = (t ^ ((t + (Math.imul(t ^ (t >>> 7), t | 61) >>> 0)) >>> 0)) >>> 0;
    return (t ^ (t >>> 14)) >>> 0;
  };
  const ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789", UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const rnd = (n: number, alphabet = ALNUM): string => { let s = ""; for (let i = 0; i < n; i++) s += alphabet[next() % alphabet.length]; return s; };
  const shape = (kind: string): string => {
    switch (kind) {
      case "sk": return "sk-PLNT" + rnd(24);
      case "skproj": return "sk-proj-PLNT" + rnd(24);
      case "ghp": return "ghp_PLNT" + rnd(32);
      case "akia": return "AKIAPLNT" + rnd(12, UPPER);
      case "xoxb": return "xoxb-" + rnd(6, "0123456789") + "-PLNT" + rnd(20);
      case "jwt": { const a = "eyJPLNT" + rnd(10); const b = ".eyJPLNT" + rnd(10); return a + b + ".PLNT" + rnd(12); }
      case "bearer": return "Bearer PLNT" + rnd(24);
      case "pem": return "-----BEGIN PRIVATE KEY-----\nPLNT" + rnd(32) + "\n-----END PRIVATE KEY-----";
    }
    throw new Error(kind);
  };
  const SHAPES = ["sk", "skproj", "ghp", "akia", "xoxb", "jwt", "bearer", "pem"];
  const RELATIONS = ["prefix", "suffix", "interior", "spanning", "adjacent", "nested"];
  const TARGETS = ["shape", "name", "value"];
  const SPELLINGS = ["raw", "quoted", "escaped", "wsquoted", "object"];
  const PAIRS = [["password", "eq"], ["api_key", "colon"], ["secret", "flag"], ["client_secret", "dq"], ["access_token", "json"], ["Authorization", "colon"], ["passwd", "dq"], ["x-api-key", "eq"]];
  const NEIGHBOURS = ["", "é", "中", "\u{1F600}", " ", " "];
  const LAYOUTS = ["pair-shape", "shape-value", "shape-pair"];
  // Lengths and positions count code points, as Python's do.
  const cp = (s: string): number => [...s].length;
  const pairText = (name: string, form: string, value: string): [string, number[], number[]] => {
    const [pre, mid, post] = ({ eq: ["", "=", ""], colon: ["", ": ", ""], flag: ["--", " ", ""], dq: ["", "=\"", "\""], json: ["\"", "\": \"", "\""] } as Record<string, string[]>)[form];
    const n0 = cp(pre), v0 = n0 + cp(name) + cp(mid);
    return [pre + name + mid + value + post, [n0, n0 + cp(name)], [v0, v0 + cp(value)]];
  };
  const build = (layout: string, name: string, form: string, kind: string, kind2: string, nb: string): [string[], Record<string, number[]>] => {
    const pv = "PLNT" + rnd(12);
    const s1 = shape(kind), s2 = shape(kind2);
    const [pt, ns, vs] = pairText(name, form, pv);
    const parts: string[] = [];
    const add = (piece: string): number => { const start = parts.reduce((n, p) => n + cp(p), 0); parts.push(piece); return start; };
    let a = 0, b = 0, p0 = 0;
    if (layout === "pair-shape") { add("n " + nb); p0 = add(pt); add(form === "flag" ? "\\ " : " "); a = add(s1); add(nb + " + ; "); b = add(s2); add(" ordinary update remains" + nb); }
    else if (layout === "shape-value") { add("ordinary " + nb); a = add(s1); add(nb + " then "); p0 = add(pt); add(nb + " and "); b = add(s2); add(" remains"); }
    else { add(nb + "note "); a = add(s1); add(" " + nb); b = add(s2); add(" ; "); p0 = add(pt); add(nb + " ordinary text remains"); }
    const t = [...parts.join("")];
    return [t, { shape: [a, a + cp(s1)], shape2: [b, b + cp(s2)], name: [p0 + ns[0], p0 + ns[1]], value: [p0 + vs[0], p0 + vs[1]] }];
  };
  const pick = (t: string[], spans: Record<string, number[]>, rel: string, target: string, k: number): string => {
    const [s0, s1] = spans[target];
    const o = target !== "shape2" ? spans.shape2 : spans.shape;
    const L = s1 - s0, d = 1 + k % 3;
    let a = 0, b = 0;
    if (rel === "prefix") { a = s0 - (3 + d); b = s0 + Math.max(1, Math.min(L - 1, 4 + d)); }
    else if (rel === "suffix") { a = s1 - Math.max(1, Math.min(L - 1, 4 + d)); b = s1 + 3 + d; }
    else if (rel === "interior") { a = s0 + 1 + d; b = s1 - 1 - d; if (b - a < 8) { a = s0 + 1; b = s1 - 1; } }
    else if (rel === "spanning") {
      if (o[0] >= s1) { a = s1 - Math.max(1, Math.min(L - 1, 3 + d)); b = o[0] + 4 + d; }
      else { a = o[1] - (4 + d); b = s0 + Math.max(1, Math.min(L - 1, 3 + d)); }
    } else if (rel === "adjacent") { if (k % 2 === 0) { a = s0 - 8 - d; b = s0; } else { a = s1; b = s1 + 8 + d; } }
    else if (rel === "nested") { a = s0 - 2; b = s0 + Math.max(1, Math.min(L - 1, 10 + d)); }
    a = Math.max(0, a); b = Math.min(t.length, b);
    if (b - a < 8) { if (a === 0) b = Math.min(t.length, 8); else a = Math.max(0, b - 8); }
    // Python slicing with a > b gives the empty string.
    return a < b ? t.slice(a, b).join("") : "";
  };
  const nestedInner = (v: string, k: number): string => { const c = [...v]; return c.length >= 10 ? c.slice(1 + k % 2, c.length - 1).join("") : v; };
  // Python json.dumps with ensure_ascii=True (\uXXXX, lowercase hex, surrogate pairs) or False (JSON.stringify spells the same otherwise).
  const asciiDumps = (s: string): string => JSON.stringify(s).replace(/[\u007f-\uffff]/g, (c: string) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  const spell = (t: string, spelling: string, k: number): string => {
    if (spelling === "raw") return t;
    if (spelling === "quoted") return JSON.stringify(t);
    if (spelling === "escaped") {
      let s = asciiDumps(t).replace(/\//g, "\\/");
      const letters: number[] = [];
      for (let i = 1; i < s.length - 1;) {
        if (s[i] === "\\") { i += s[i + 1] === "u" ? 6 : 2; continue; }
        if (/[A-Za-z]/.test(s[i])) letters.push(i);
        i++;
      }
      if (letters.length) { const i = letters[(k * 7) % letters.length]; s = s.slice(0, i) + "\\u" + s.charCodeAt(i).toString(16).padStart(4, "0") + s.slice(i + 1); }
      return s;
    }
    if (spelling === "wsquoted") return "\r\n" + JSON.stringify(t) + "  \t\r\n";
    if (spelling === "object") return "{\"msg\": " + JSON.stringify(t) + ", \"n\": 1}";
    throw new Error(spelling);
  };
  const combos: string[][] = []; for (const r of RELATIONS) for (const g of TARGETS) combos.push([r, g]);
  const rows: { id: number; input: string; registered: string[]; cls: Record<string, string | number> }[] = [];
  let cid = 0;
  for (let count = 1; count <= 4; count++) for (let ri = 0; ri < RELATIONS.length; ri++) for (const target of TARGETS) for (let si = 0; si < SHAPES.length; si++) for (const spelling of SPELLINGS) for (let k = 0; k < 8; k++) {
    const rel = RELATIONS[ri], kind = SHAPES[si];
    const [name, form] = PAIRS[(k + si) % PAIRS.length];
    const layout = LAYOUTS[(k + ri) % LAYOUTS.length];
    const nb = NEIGHBOURS[(k + si + ri) % NEIGHBOURS.length];
    let kind2 = SHAPES[(si + 1 + k) % SHAPES.length];
    if (kind2 === kind) kind2 = SHAPES[(si + 2) % SHAPES.length];
    const [t, spans] = build(layout, name, form, kind, kind2, nb);
    const reg: string[] = [];
    const first = pick(t, spans, rel, target, k);
    reg.push(first);
    const j = combos.findIndex(([r, g]) => r === rel && g === target);
    let step = 1;
    while (reg.length < count) {
      let v: string;
      if (rel === "nested" && reg.length === 1) v = nestedInner(first, k);
      else { const [r2, g2] = combos[(j + step * 5 + k) % combos.length]; v = pick(t, spans, r2, g2, k + step); }
      step++;
      if (!reg.includes(v) && cp(v) >= 8) reg.push(v);
      if (step > 40) break;
    }
    let regsp = "decoded";
    if (spelling === "escaped" && k % 2 === 1) {
      const last = asciiDumps(reg[reg.length - 1]).replace(/\//g, "\\/").slice(1, -1);
      if (last !== reg[reg.length - 1]) { reg[reg.length - 1] = last; regsp = "raw-bytes"; }
    }
    rows.push({ id: cid, input: spell(t.join(""), spelling, k), registered: reg, cls: { count, rel, target, shape: kind, spelling, layout, pair: name, form, nb: nb ? "U+" + nb.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0") : "ascii", regsp, nreg: reg.length } });
    cid++;
  }
  return rows;
}


describe("scrubAttribute: a quoted root with several overlapping registered values", () => {
  test("the review/A row shows no window both References hide, and stays one JSON string", () => {
    const output = scrubAttribute(REVIEW_ROW.input, REVIEW_ROW.registered), values = plants(REVIEW_ROW.input), reference = windowsShown(REVIEW_REFERENCE, values);
    expect([...windowsShown(output, values)].filter(window => !reference.has(window))).toEqual([]);
    expect(isJSONString(output)).toBe(true);
  });
  test.each(BOTH_ROWS)("$id shows no window both References hide", row => {
    const output = scrubAttribute(row.input, row.registered);
    expect([...windowsShown(output, row.windows)]).toEqual([]);
    expect(isJSONString(output)).toBe(true);
  });
  test.each(VALUE_ROWS)("$id ($class) shows no window scrubValue hides", row => {
    expect(aboveValue(row.input, row.registered)).toEqual([]);
    expect(isJSONString(scrubAttribute(row.input, row.registered))).toBe(true);
  });
  test("the generated family: no window above scrubValue where scrubValue does not hide the whole string; JSON kept; no throw", () => {
    const cases = familyR4t();
    expect(cases.length).toBe(23040);
    const above: number[] = [], invalid: number[] = [];
    for (const c of cases) {
      const attr = scrubAttribute(c.input, c.registered), value = scrubValue(c.input, c.registered);
      let kind = "text";
      try { const node: unknown = JSON.parse(c.input); kind = typeof node; } catch { /* prose */ }
      if (kind === "string" && !isJSONString(attr)) invalid.push(c.id);
      if (wholeValue(value)) continue;
      const values = plants(c.input), shown = windowsShown(value, values);
      if ([...windowsShown(attr, values)].some(window => !shown.has(window))) above.push(c.id);
    }
    expect({ above: above.length, invalid: invalid.length, firstAbove: above.slice(0, 5) }).toEqual({ above: 0, invalid: 0, firstAbove: [] });
  }, 300000);
});

describe("scrubAttribute: many registered values that the attribute does not hold", () => {
  // 1,024 and 2,000 registered values, none in the attribute; URL tokens the raw bytes separate with an escaped é: the attribute is read
  // as with no registered value (the URL's key value hidden), not made the placeholder by the work of locating absent values.
  const registered = (n: number) => Array.from({ length: n }, (_, i) => "PLNTReg" + String(i).padStart(5, "0"));
  test.each([[7, 2000], [14, 1024], [15, 1024]])("%i URL tokens, %i absent registered values", (count, n) => {
    const input = "\"note " + Array(count).fill("http://x?key=PLNTdata").join("\\u00e9") + " end\"";
    const output = scrubAttribute(input, registered(n));
    expect(output).toBe(scrubAttribute(input, []));
    expect(output).not.toBe(JSON.stringify("<redacted>"));
  });
});

describe("scrubAttribute: the raw-text read adds no escaped quotes alone", () => {
  // A pair value written with escaped quotes inside a quoted root, with registered values beside it: the value's escaped quotes stay
  // shown, as both References show them (d2dab4e8 and in-tree outputs below); the raw-text read adds no hidden quote or backslash alone.
  test.each([
    ["\" start record: diagnostic {\\\"password\\\": \\\"PLNTg27mlDHCwMNynkeBbEuP\\\"} ; end note \"", ["c {\\\"p", "iagnostic"], "\" start record: d<redacted> {\\\"password\\\": \\\"<redacted>\\\"} ; end note \""],
    ["\" start record: diagnostic {\\\"password\\\": \\\"PLNTRpv536ibJ4qLHLb4KtZu\\\"} ; end note \"", ["c {\\\"p", "agnostic "], "\" start record: di<redacted>{\\\"password\\\": \\\"<redacted>\\\"} ; end note \""],
  ])("%s", (input, registered, reference) => {
    expect(scrubAttribute(input, registered as string[])).toBe(reference);
  });
});
