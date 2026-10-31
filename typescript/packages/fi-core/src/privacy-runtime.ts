/** Dependency-free S6 recognition shared by Node transports, exporters and the edge adapter.
 * Recognition data lives only in privacy.json; keep its shipping copies byte-identical.
 */
interface PrivacyCatalog {
  credentials: {
    replacement: string;
    patterns: { id: string; contains: string }[];
    exclusions: { regex: string; unless_contains_patterns?: string[] }[];
    contextual_exclusions: { path_regex: string }[];
    seed_names: string[];
  };
  signed_url: { parameters: string[] };
  media: {
    base64_alphabet: string; base64_prefix_decoded_bytes: number;
    replacement: string; other_replacement: string;
    unrecognized_base64_fields: string[]; unrecognized_base64_min_chars_exclusive: number;
    magic: { tests: { offset: number; hex: string; mask?: string }[] }[];
    data_uri: { other_replacement: string };
    mime_siblings: { names: string[]; prefixes: string[]; exact: string[]; payload_fields: string[] };
    audio: { fields: string[]; payload_fields: string[]; type_names: string[]; metadata_fields: string[] };
  };
  limits: { max_depth: number; max_nodes: number; max_text_chars: number };
}
// Literal require also supports source consumers whose tsconfig does not enable
// JSON imports. ESM distributions bridge to this same CJS leaf; bundlers inline it.
const catalog: PrivacyCatalog = require("./privacy.json");

export const MAX_DISCOVERY_BODY_BYTES = catalog.limits.max_text_chars;
const replacement = catalog.credentials.replacement;
const media = catalog.media, mime = media.mime_siblings, audio = media.audio;
export const normalizePrivacyName = (name: string): string => name.replace(/[A-Z]/g, c => c.toLowerCase()).replace(/[-_.]/g, "");
const pathParts = (path: string) => path.split(/[.\[\]]+/).filter(Boolean);
const components = (path: string) => pathParts(path).filter(part => !/^\d+$/.test(part));
const leaf = (path: string) => normalizePrivacyName(components(path).at(-1) ?? "");
const patterns = catalog.credentials.patterns.map(item => item.contains);
const exclusions = catalog.credentials.exclusions.map(item => ({
  regex: new RegExp(item.regex), guards: ("unless_contains_patterns" in item ? item.unless_contains_patterns ?? [] : []).map(id => catalog.credentials.patterns.find(p => p.id === id)!.contains),
}));
const contextual = catalog.credentials.contextual_exclusions.map(item => new RegExp(item.path_regex));
const signed = new Set(catalog.signed_url.parameters.map(normalizePrivacyName));
const excluded = (folded: string) => exclusions.some(({ regex, guards }) => regex.test(folded) && !guards.some(marker => folded.includes(marker)));
/** The ONE credential-name predicate. Query mode adds signed-URL parameters.
 * S5 environment seeding intentionally includes metric/identifier names: its
 * initialization contract protects every credential-marked environment value.
 */
export function isCredentialName(name: string, query: boolean | "environment" = false): boolean {
  if (query === "environment") return patterns.some(pattern => normalizePrivacyName(name).includes(pattern));
  if (query && signed.has(normalizePrivacyName(name))) return true;
  const parts = components(name).map(normalizePrivacyName);
  let exempt = false;
  for (let i = 0; i < parts.length; i++) {
    if (excluded(parts[i]) || contextual.some(regex => regex.test(parts.slice(0, i + 1).join(".")))) exempt = true;
    else if (patterns.some(pattern => parts[i].includes(pattern))) return true;
  }
  const folded = normalizePrivacyName(name);
  return !exempt && !excluded(folded) && patterns.some(pattern => folded.includes(pattern));
}
/** Compatibility/test inventory, not a second recognition list. */
export const CREDENTIAL_HEADERS: readonly string[] = Object.freeze([...catalog.credentials.seed_names]);
const alphabet = new Set(media.base64_alphabet);
const encoded = (value: string) => {
  if (!value.length) return false;
  for (let i = 0; i < value.length; i++) if (!alphabet.has(value[i])) return false;
  return true;
};
const omitted = (value: string) => media.replacement.replace("{length}", String(value.length));
const omission = /^<omitted: (?:[0-9]+ base64 chars|media|data URI)>$/;
const bytes = (hex: string) => hex.match(/../g)!.map(pair => parseInt(pair, 16));
const magicRules = media.magic.map(rule => rule.tests.map(test => ({ offset: test.offset, expected: bytes(test.hex), mask: bytes(test.mask ?? "ff".repeat(test.hex.length / 2)) })));
function magic(value: string): boolean {
  if (!encoded(value)) return false;
  // Read just the decoded prefix; no Buffer/Node globals or full-media decode.
  const decoded: number[] = []; let bits = 0, accumulator = 0;
  const base64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  for (const char of value) {
    if (char === "\r" || char === "\n") continue;
    if (char === "=") break;
    const n = base64.indexOf(char === "-" ? "+" : char === "_" ? "/" : char);
    if (n < 0) return false;
    accumulator = (accumulator << 6) | n; bits += 6;
    if (bits >= 8) { bits -= 8; decoded.push((accumulator >> bits) & 255); }
    if (decoded.length === media.base64_prefix_decoded_bytes) break;
  }
  return magicRules.some(tests => tests.every(test => test.expected.every((expected, i) => decoded[test.offset + i] !== undefined && (decoded[test.offset + i] & test.mask[i]) === (expected & test.mask[i]))));
}
// URLSearchParams supplies forgiving, once-only UTF-8 decoding (including malformed escapes).
const decode = (value: string, plus = true) => new URLSearchParams("v=" + (plus ? value : value.replace(/\+/g, "%2B")).replace(/&/g, "%26")).get("v")!;
function dataURI(value: string): string | undefined {
  if (!/^data:/i.test(value)) return undefined;
  const comma = value.indexOf(","); if (comma < 0) return undefined;
  const payload = decode(value.slice(comma + 1), false);
  return /;base64$/i.test(value.slice(5, comma)) && (!payload || encoded(payload)) ? omitted(payload) : media.data_uri.other_replacement;
}
const DATA = String.raw`(?<![A-Za-z0-9+.\-_:])data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*`;
const URL_TOKEN = String.raw`(?:https?://|(?<!\S)[/?])(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\s"<>\\])+`;
const URL_WHOLE = new RegExp(`^(?:${URL_TOKEN})$`, "i");
const TEXT = new RegExp(`(${URL_TOKEN})|(${DATA})`, "gi");
const dataText = (value: string, framed = false): string => (framed ? dataURI(value) : undefined) ?? value.replace(new RegExp(DATA, "gi"), original => dataURI(original) ?? original);

type Hint = readonly [boolean, boolean];
export type PrivacyHints = ReadonlyMap<string, Hint>;
function hint(name: string, value: unknown): Hint {
  if (Array.isArray(value)) {
    let m = false, a = false;
    for (const item of value) if (typeof item === "string") { const next = hint(name, item); m ||= next[0]; a ||= next[1]; }
    return [m, a];
  }
  if (typeof value !== "string") return [false, false];
  const nameLeaf = leaf(name), type = value.split(";", 1)[0].trim().toLowerCase();
  if (mime.names.includes(nameLeaf)) return [mime.prefixes.some(prefix => type.startsWith(prefix)) || mime.exact.includes(type), type.startsWith("audio/")];
  return [false, nameLeaf === "type" && audio.type_names.includes(normalizePrivacyName(value))];
}
/** Index all siblings once, preserving exact indexed parents and combining duplicate query hints. */
export function indexPrivacyHints(items: Iterable<readonly [string, unknown]>, path = "", previous: PrivacyHints = new Map()): PrivacyHints {
  const hints = new Map(previous);
  for (const [name, value] of items) {
    const next = hint(name, value); if (!next[0] && !next[1]) continue;
    const parts = pathParts(path ? `${path}.${name}` : name);
    while (parts.length && /^\d+$/.test(parts.at(-1)!)) parts.pop();
    parts.pop(); const parent = parts.join("."), old = hints.get(parent);
    hints.set(parent, [next[0] || !!old?.[0], next[1] || !!old?.[1]]);
  }
  return hints;
}
function context(path: string, hints: PrivacyHints, inheritedMedia = false, inheritedAudio = false) {
  let m = inheritedMedia || !!hints.get("")?.[0], a = inheritedAudio || !!hints.get("")?.[1], payload = false, direct = false;
  const prefix: string[] = [];
  for (const part of pathParts(path)) {
    prefix.push(part); const name = normalizePrivacyName(part);
    if (!/^\d+$/.test(part)) {
      if (audio.metadata_fields.includes(name)) { m = false; a = false; payload = false; }
      direct = audio.fields.includes(name); a ||= direct;
      if (m && mime.payload_fields.includes(name) || a && audio.payload_fields.includes(name)) payload = true;
    }
    const next = hints.get(prefix.join(".")); m ||= !!next?.[0]; a ||= !!next?.[1];
  }
  return { media: m, audio: a, payload: payload || direct };
}
const noHints: PrivacyHints = new Map();
/** The ONE structural-media predicate; returns an omission, or undefined for non-media.
 * Exact scalar boundaries precede context; only free text uses delimiter-limited data scanning.
 */
export function mediaReplacement(value: unknown, path = "", hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, inheritedAudio = false): string | undefined {
  if (typeof value === "string") {
    if (value === replacement || omission.test(value)) return undefined;
    if (framed) { const uri = dataURI(value); if (uri !== undefined) return uri; }
    const payload = context(path, hints, inheritedMedia, inheritedAudio).payload;
    if (magic(value) || payload) return encoded(value) ? omitted(value) : media.other_replacement;
    if (encoded(value) && media.unrecognized_base64_fields.includes(leaf(path)) && value.length > media.unrecognized_base64_min_chars_exclusive) return omitted(value);
  } else if (value != null && context(path, hints, inheritedMedia, inheritedAudio).payload) {
    if (!Array.isArray(value) || value.length > 0 && value.every(item => typeof item === "number" || typeof item === "boolean")) return media.other_replacement;
  }
  return undefined;
}
const partition = (text: string, separator: string): [string, string, string] => {
  const i = text.indexOf(separator); return i < 0 ? [text, "", ""] : [text.slice(0, i), separator, text.slice(i + separator.length)];
};
function queryParts(url: string) {
  const [target, hash, fragment] = partition(url, "#"), [base, question, raw] = partition(target, "?");
  const pairs = raw.split("&").map(pair => { const [name, equals, value] = partition(pair, "="); return { name, equals, value, decodedName: decode(name), decodedValue: decode(value) }; });
  return { base, question, pairs, hash, fragment };
}
function urlValue(original: string): string {
  const { base, question, pairs, hash, fragment } = queryParts(original);
  const hints = indexPrivacyHints(pairs.filter(pair => pair.equals).map(pair => [pair.decodedName, pair.decodedValue]));
  const query = pairs.map(pair => {
    let value = pair.value;
    if (pair.equals) {
      if (isCredentialName(pair.decodedName, true)) value = replacement;
      else {
        let clean = mediaReplacement(pair.decodedValue, pair.decodedName, hints);
        if (clean === undefined) { const text = dataText(pair.decodedValue, true); if (text !== pair.decodedValue) clean = text; }
        if (clean !== undefined) value = clean;
      }
    }
    return pair.name + pair.equals + value;
  }).join("&");
  return dataText(base.replace(/^(https?:\/\/)[^\s/@]+:[^\s/@]+@/i, `$1${replacement}@`)) + question + query + hash + dataText(fragment);
}
function isURLValue(value: string): boolean {
  if (URL_WHOLE.test(value)) return true;
  if (!/^(?:https?:\/\/|[/?])/i.test(value)) return false;
  const { base, question, pairs, hash, fragment } = queryParts(value);
  if (!question) return false;
  const masked = base + question + pairs.map(pair => pair.name + pair.equals + (pair.equals && dataURI(pair.decodedValue) !== undefined ? media.data_uri.other_replacement : pair.value)).join("&") + hash + fragment;
  return masked !== value && URL_WHOLE.test(masked);
}
function text(value: string, secrets: readonly string[], framed: boolean): string {
  if (framed && isURLValue(value)) value = urlValue(value);
  else value = value.replace(TEXT, (original: string, url: string | undefined, _data: string | undefined, offset: number) => {
    if (!url) return dataText(original);
    const closing = offset > 0 && value[offset - 1] === "'" && original.endsWith("'") ? "'" : "";
    return urlValue(closing ? original.slice(0, -1) : original) + closing;
  });
  for (const secret of [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))].sort((a, b) => b.length - a.length)) value = value.split(replacement).map(part => part.split(secret).join(replacement)).join(replacement);
  return value;
}
export interface PrivacyBudget { nodes: number; text: number; seen: WeakSet<object> }
const newBudget = (): PrivacyBudget => ({ nodes: catalog.limits.max_nodes, text: catalog.limits.max_text_chars, seen: new WeakSet() });
function charge(budget: PrivacyBudget, depth: number, length = 0): void {
  if (--budget.nodes < 0 || depth > catalog.limits.max_depth) throw new Error("Privacy structure budget");
  if ((budget.text -= length) < 0) throw new Error("Privacy text budget");
}
function dataEntries(value: object): [string, unknown][] {
  const keys = Object.keys(value);
  if (keys.length > catalog.limits.max_nodes) throw new Error("Privacy structure budget");
  return keys.flatMap(name => { const d = Object.getOwnPropertyDescriptor(value, name); return d && "value" in d ? [[name, d.value] as [string, unknown]] : []; });
}
/** Walk only plain, already-materialized data. The Node S1 boundary rejects proxies before calling. */
export function scrubValue(value: any, secrets: readonly string[] = [], path = "", depth = 0, inheritedAudio = false, budget: PrivacyBudget = newBudget(), hints: PrivacyHints = noHints, framed = true, inheritedMedia = false): any {
  charge(budget, depth);
  if (isCredentialName(path)) return replacement;
  if (value && typeof value === "object") {
    if (budget.seen.has(value)) return "<omitted>";
    budget.seen.add(value);
    if (!Array.isArray(value)) {
      if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return typeof value;
      const items = dataEntries(value);
      if (items.length > budget.nodes) throw new Error("Privacy structure budget");
      budget.text -= items.reduce((n, [name]) => n + name.length, 0); if (budget.text < 0) throw new Error("Privacy text budget");
      hints = indexPrivacyHints(items, path, hints);
      return Object.fromEntries(items.map(([name, item]) => [text(name, secrets, false), scrubValue(item, secrets, path ? `${path}.${name}` : name, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia)]));
    }
    const length = Object.getOwnPropertyDescriptor(value, "length")!.value;
    if (length > budget.nodes) throw new Error("Privacy structure budget");
    const items = Array.from({ length }, (_, i) => { const d = Object.getOwnPropertyDescriptor(value, String(i)); return d && "value" in d ? d.value : undefined; });
    const clean = mediaReplacement(items, path, hints, framed, inheritedMedia, inheritedAudio);
    if (clean !== undefined) return clean;
    return items.map((item, i) => scrubValue(item, secrets, `${path}.${i}`, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia));
  }
  if (typeof value === "string") {
    budget.text -= value.length; if (budget.text < 0) throw new Error("Privacy text budget");
    const clean = mediaReplacement(value, path, hints, framed, inheritedMedia, inheritedAudio); if (clean !== undefined) return clean;
    // JSON tool arguments are independently framed. Share budgets across nested encodings.
    if (/^\s*[\[{]/.test(value)) {
      let parsed: unknown; let valid = false;
      try { parsed = JSON.parse(value); valid = true; } catch { /* Prose is handled below. */ }
      if (valid) {
        const ctx = context(path, hints, inheritedMedia, inheritedAudio);
        const scrubbed = scrubValue(parsed, secrets, "", depth + 1, ctx.audio, budget, noHints, true, ctx.media);
        if (JSON.stringify(parsed) !== JSON.stringify(scrubbed)) return text(JSON.stringify(scrubbed), secrets, false);
      }
    }
    return scrubEmbeddedText(value, secrets, depth + 1, budget, framed);
  }
  const clean = mediaReplacement(value, path, hints, framed, inheritedMedia, inheritedAudio); if (clean !== undefined) return clean;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  return value == null || typeof value === "boolean" ? value : typeof value;
}
/** S1's additional malformed/embedded JSON policy; recognition still delegates to S6. */
function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean): string {
  if (depth > catalog.limits.max_depth) throw new Error("Privacy structure budget");
  // A whole data URI or URL owns its delimiters, including brackets and quotes.
  if (framed) { const uri = mediaReplacement(value); if (uri !== undefined) return uri; if (isURLValue(value)) return text(value, secrets, true); }
  let start = -1, nesting = 0, quoted = false, escaped = false, end = 0, out = "";
  for (let i = 0; i < value.length; i++) {
    const c = value[i];
    if (start < 0) { if (c !== "{" && c !== "[") continue; start = i; nesting = 1; quoted = false; escaped = false; continue; }
    if (quoted) { if (escaped) escaped = false; else if (c === "\\") escaped = true; else if (c === '"') quoted = false; }
    else if (c === '"') quoted = true;
    else if (c === "{" || c === "[") nesting++;
    else if ((c === "}" || c === "]") && --nesting === 0) {
      const raw = value.slice(start, i + 1); let clean = raw, parsed: unknown, valid = false;
      try { parsed = JSON.parse(raw); valid = true; } catch { /* A prose wrapper can enclose real JSON. */ }
      if (valid) { const scrubbed = scrubValue(parsed, secrets, "", depth + 1, false, budget); if (JSON.stringify(parsed) !== JSON.stringify(scrubbed)) clean = JSON.stringify(scrubbed); }
      else clean = raw[0] + scrubEmbeddedText(raw.slice(1, -1), secrets, depth + 1, budget, false) + raw.at(-1);
      out += value.slice(end, start) + clean; end = i + 1; start = -1;
    }
  }
  if (start >= 0) {
    const tail = value.slice(start);
    // A truncated JSON object cannot safely retain structural media context. Use
    // the same catalog hint/payload predicate rather than another spelling list.
    const fields: [string, unknown][] = [];
    for (const match of tail.matchAll(/("(?:\\.|[^"\\])*"\s*:\s*)("(?:\\.|[^"\\])*")/g)) {
      try { fields.push([JSON.parse(match[1].replace(/\s*:\s*$/, "")), JSON.parse(match[2])]); } catch { /* malformed */ }
    }
    const hints = indexPrivacyHints(fields);
    const mediaFragment = [...hints.values()].some(h => h[0] || h[1]) || [...tail.matchAll(/"([^"\\]+)"\s*:/g)].some(m => mediaReplacement(0, m[1]) !== undefined);
    out += value.slice(end, start) + (tail[0] === "{" && mediaFragment ? "<omitted: incomplete audio>" : tail[0] + scrubEmbeddedText(tail.slice(1), secrets, depth + 1, budget, false));
    end = value.length;
  }
  value = (out + value.slice(end)).replace(/("(?:\\.|[^"\\])*"\s*:\s*)("(?:\\.|[^"\\])*")/g, (all, prefix: string, quoted: string) => {
    let key: string, original: string;
    try { key = JSON.parse(prefix.replace(/\s*:\s*$/, "")); original = JSON.parse(quoted); } catch { return all; }
    // Do not re-walk already valid JSON here: contextual logprob exclusions and
    // ancestry require its full path. This fallback handles unbalanced prose only.
    if (start < 0 && end > 0) return all;
    const clean = scrubValue(original, secrets, key, depth + 1, false, budget);
    return clean === original ? all : prefix + JSON.stringify(clean);
  });
  return text(value, secrets, framed);
}
/** Free-text entry point: unlike scalar values, data: prefixes do not own trailing prose. */
export function scrubText(value: string, secrets: readonly string[] = [], depth = 0): string {
  const budget = newBudget(); charge(budget, depth, value.length);
  return scrubEmbeddedText(value, secrets, depth, budget, false);
}
export function scrubAttribute(value: string, secrets: readonly string[] = []): string {
  // Preserve S1's JSON-scalar handling as well as S6's object/array walker. SDKs
  // can serialize a primitive media value as a quoted JSON output.value.
  if (value.length > catalog.limits.max_text_chars) throw new Error("Privacy text budget");
  let parsed: unknown;
  try { parsed = JSON.parse(value); } catch { return scrubValue(value, secrets); }
  const clean = scrubValue(parsed, secrets);
  return JSON.stringify(parsed) === JSON.stringify(clean) ? scrubValue(value, secrets) : text(JSON.stringify(clean), secrets, false);
}

/** S5 harvesting over already-parsed data, sharing exactly the export predicate.
 * No body reads, user conversions or per-trace credential groups. Exhaustion throws.
 */
export function discoverSecrets(sources: Iterable<unknown>, entries = dataEntries): string[] {
  const found = new Set<string>(), seen = new Set<object>(); const budget = newBudget();
  const add = (value: string) => { if (value.length < 8 || value === replacement) return; if (found.size >= 1024 && !found.has(value)) throw new Error("Privacy credential budget"); found.add(value); };
  const urls = (value: string) => {
    const visit = (url: string) => { for (const pair of queryParts(url).pairs) if (pair.equals && isCredentialName(pair.decodedName, true)) { add(pair.value); add(pair.decodedValue); } };
    if (isURLValue(value)) visit(value);
    else for (const match of value.matchAll(new RegExp(URL_TOKEN, "gi"))) {
      const closing = match.index! > 0 && value[match.index! - 1] === "'" && match[0].endsWith("'");
      visit(closing ? match[0].slice(0, -1) : match[0]);
    }
  };
  const visit = (value: unknown, path = "", depth = 0, query = false) => {
    if (typeof value === "string" && /^\s*[\[{]/.test(value) && value.length > MAX_DISCOVERY_BODY_BYTES) throw new Error("Privacy discovery body budget");
    charge(budget, depth, typeof value === "string" ? value.length : 0);
    const credential = isCredentialName(path, query);
    if (typeof value === "string") {
      if (credential) { add(value); if (leaf(path).endsWith("authorization")) add(value.replace(/^\S+\s+/, "")); }
      else {
        urls(value);
        if (/^\s*[\[{]/.test(value)) {
          let parsed: unknown; try { parsed = JSON.parse(value); } catch { return; }
          visit(parsed, "", depth + 1);
        }
      }
    } else if (credential && (typeof value === "number" || typeof value === "boolean")) add(String(value));
    else if (value && typeof value === "object" && !seen.has(value)) {
      seen.add(value);
      const items = entries(value); if (items.length > budget.nodes) throw new Error("Privacy discovery budget");
      for (const [name, item] of items) {
        if ((budget.text -= name.length) < 0) throw new Error("Privacy text budget");
        visit(item, path ? `${path}.${name}` : name, depth + 1, query || /^(?:query|defaultQuery|searchParams)$/.test(path));
      }
    }
  };
  for (const source of sources) visit(source);
  return [...found].sort((a, b) => b.length - a.length);
}
export function credentialSecrets(headers: Record<string, string>, configured: readonly string[] = []): string[] {
  const values = new Set(configured);
  for (const [name, value] of Object.entries(headers)) if (isCredentialName(name)) {
    values.add(value); if (leaf(name).endsWith("authorization")) values.add(value.replace(/^\S+\s+/, ""));
  }
  return [...values].filter(value => value.length >= 8 && value !== replacement).sort((a, b) => b.length - a.length);
}
