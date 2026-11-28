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
    value_shapes: { id: string; regex: string; flags: string }[];
    vocabulary: { words: string[] };
  };
  logprob_tokens: { version: number; formats: { format: string; paths: string[][]; text_paths: string[][] }[] };
  signed_url: { parameters: string[] };
  scalar_metrics: { version: number; canonical_decimal: { regex: string; max_chars: number; base64_min_chars: number; keys: string[] }; containers: { path: string; keys: string[]; value_type?: "nonnegative_safe_integer" }[]; zero_detail_deferral: { keys: string[] } };
  media: {
    base64_alphabet: string; base64_prefix_decoded_bytes: number;
    replacement: string; other_replacement: string;
    unrecognized_base64_fields: string[]; unrecognized_base64_min_chars_exclusive: number;
    magic: { tests: { offset: number; hex: string; mask?: string }[] }[];
    data_uri: { other_replacement: string };
    mime_siblings: { names: string[]; prefixes: string[]; exact: string[]; payload_fields: string[] };
    image: { type_names: string[]; modality_names: string[]; payload_fields: string[] };
    video: { type_names: string[]; modality_names: string[] };
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
// Names repeat across spans and recognition passes. Bound both cardinality and
// string length; never retain an arbitrarily large user value in this memo.
const normalizedPrivacyNames = new Map<string, string>();
export const normalizePrivacyName = (name: string): string => {
  // Keep the original expression (and its errors/hooks) for non-string callers.
  if (typeof name !== "string" || name.length > 256) return name.replace(/[A-Z]/g, c => c.toLowerCase()).replace(/[-_.]/g, "");
  const cached = normalizedPrivacyNames.get(name);
  if (cached !== undefined) return cached;
  const normalized = name.replace(/[A-Z]/g, c => c.toLowerCase()).replace(/[-_.]/g, "");
  if (normalizedPrivacyNames.size >= 512) normalizedPrivacyNames.delete(normalizedPrivacyNames.keys().next().value!);
  // Detach short slices from potentially large backing strings on cache misses.
  const key = name.split("").join("");
  normalizedPrivacyNames.set(key, normalized === name ? key : normalized.split("").join(""));
  return normalized;
};
const pathParts = (path: string) => path.split(/[.\[\]]+/).filter(Boolean);
const components = (path: string) => pathParts(path).filter(part => !/^\d+$/.test(part));
const leaf = (path: string) => normalizePrivacyName(components(path).at(-1) ?? "");
const patterns = catalog.credentials.patterns.map(item => item.contains);
const exclusions = catalog.credentials.exclusions.map(item => ({
  regex: new RegExp(item.regex), guards: ("unless_contains_patterns" in item ? item.unless_contains_patterns ?? [] : []).map(id => catalog.credentials.patterns.find(p => p.id === id)!.contains),
}));
const contextual = catalog.credentials.contextual_exclusions.map(item => new RegExp(item.path_regex));
const JWT_SHAPE = "\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}";
// Python 3 re, as the reference reads the catalog's value shapes: \b and \w are Unicode (a letter, a number or '_'), \s is
// str.isspace, and IGNORECASE folds U+017F with s, U+212A with k and U+0130/U+0131 with i. JS \s, \b, \w and /i differ on
// each, so a shape is built from explicit classes. A shape this table does not know keeps its catalog regex, unchanged.
const PY_WORD = String.raw`\p{L}\p{N}_`;
const PY_SPACE = String.raw`\t\n\v\f\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const PY_BEFORE = `(?<![${PY_WORD}])`, PY_AFTER = `(?![${PY_WORD}])`;
const PY_FOLD: Record<string, string> = { s: "sS\u017f", k: "kK\u212a", i: "iI\u0130\u0131" };
const pyFold = (word: string) => [...word].map(c => `[${PY_FOLD[c.toLowerCase()] ?? c.toLowerCase() + c.toUpperCase()}]`).join("");
const PY_SHAPES: Record<string, { regex: string; flags: string; lead: string; body: string }> = {
  "provider-api-key": { regex: "\\bsk-[A-Za-z0-9_-]{20,}", flags: "", lead: PY_BEFORE, body: "sk-[A-Za-z0-9_-]{20,}" },
  "aws-access-key": { regex: "\\b(?:AKIA|ASIA)[A-Z0-9]{16}\\b", flags: "", lead: PY_BEFORE, body: `(?:AKIA|ASIA)[A-Z0-9]{16}${PY_AFTER}` },
  "authorization-value": {
    regex: "\\b(?:Bearer|Basic)\\s+[A-Za-z0-9+/_=-]{8,}", flags: "i", lead: PY_BEFORE,
    body: `(?:${pyFold("Bearer")}|${pyFold("Basic")})[${PY_SPACE}]+[A-Za-z0-9+/_=\\-\u0130\u0131\u017f\u212a]{8,}`,
  },
  // Python's $ also holds before a final newline; the lazy body stops there.
  "private-key-pem": {
    regex: "-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\\s\\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)", flags: "", lead: "",
    body: "-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\\s\\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?=\\n?(?![\\s\\S])))",
  },
};
const pyShape = (item: { id: string; regex: string; flags: string }) => {
  const known = PY_SHAPES[item.id];
  return known && known.regex === item.regex && known.flags === item.flags ? known : undefined;
};
// The jwt shape is scanned by replaceJwt (exactly equivalent, linear time); other shapes run as regexes.
const valueShapes: (RegExp | ((value: string) => string))[] = catalog.credentials.value_shapes.map(item => {
  if (item.id === "jwt" && item.regex === JWT_SHAPE && item.flags === "") return replaceJwt;
  const known = pyShape(item);
  return known ? new RegExp(known.lead + known.body, "gu") : new RegExp(item.regex, item.flags + "g");
});
const signed = new Set(catalog.signed_url.parameters.map(normalizePrivacyName));
const excluded = (folded: string) => exclusions.some(({ regex, guards }) => regex.test(folded) && !guards.some(marker => folded.includes(marker)));
/** The ONE credential-name predicate. Query mode adds signed-URL parameters.
 * S5 environment seeding intentionally includes metric/identifier names: its
 * initialization contract protects every credential-marked environment value.
 */
export function isCredentialName(name: string, query: boolean | "environment" = false): boolean {
  // A name of more than limits.max_depth components is not read: it fails closed.
  if (typeof name === "string" && isPathOverBudget(name)) return true;
  const folded = normalizePrivacyName(name);
  if (query && query !== "environment" && signed.has(folded)) return true;
  // Most prose assignments and attribute names contain no credential marker
  // (a pattern or a vocabulary word). Reject those before path splitting/exclusions.
  if (!patterns.some(pattern => folded.includes(pattern)) && !vocabulary(name)) return false;
  if (query === "environment") return true;
  return credentialName(name);
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
function magic(value: string, partial = false): boolean {
  // Reject a non-matching decoded prefix before validating the whole base64
  // string. No full decode and no whole-string scan for ordinary non-media.
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
  const complete = magicRules.some(tests => tests.every(test => test.expected.every((expected, i) => decoded[test.offset + i] !== undefined && (decoded[test.offset + i] & test.mask[i]) === (expected & test.mask[i]))));
  // S1 also receives shortened SDK values. A strong (at least six-byte) prefix
  // of a single offset-zero signature can be omitted there; never infer RIFF's
  // missing second discriminator or alter S6's complete-input matching rule.
  const prefix = partial && decoded.length >= 6 && magicRules.some(tests => tests.length === 1 && tests[0].offset === 0
    && decoded.length < tests[0].expected.length
    && decoded.every((byte, i) => (byte & tests[0].mask[i]) === (tests[0].expected[i] & tests[0].mask[i])));
  return (complete || prefix) && encoded(value);
}
/** @internal S1-only short-signature guard; generic/catalog S6 remains exact. */
export function s1MediaPrefixReplacement(value: string): string | undefined {
  if (value === "__REDACTED__") return value;
  return value.length <= 48 && magic(value, true) ? omitted(value) : undefined;
}
// URLSearchParams supplies forgiving, once-only UTF-8 decoding (including malformed escapes).
const decode = (value: string, plus = true) => new URLSearchParams("v=" + (plus ? value : value.replace(/\+/g, "%2B")).replace(/&/g, "%26")).get("v")!;
function dataURI(value: string, inspect?: (characters: number) => void): string | undefined {
  const prefix = value.slice(0, 5); inspect?.(prefix.length);
  if (!/^data:/i.test(prefix)) return undefined;
  inspect?.(value.length);
  const comma = value.indexOf(","); if (comma < 0) return undefined;
  const body = value.slice(comma + 1); inspect?.(body.length);
  const payload = decode(body, false), metadata = value.slice(5, comma);
  inspect?.(metadata.length);
  if (!/;base64$/i.test(metadata)) return media.data_uri.other_replacement;
  inspect?.(payload.length);
  return !payload || encoded(payload) ? omitted(payload) : media.data_uri.other_replacement;
}
// The reference's whitespace (Python's str.isspace), not JS \s: it adds U+001C-U+001F and U+0085 and leaves out U+FEFF.
const SPACE = String.raw`\t\n\v\f\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const DATA = String.raw`(?<![A-Za-z0-9+.\-_:])data:[^${SPACE}"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*`;
const URL_BODY = String.raw`(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^${SPACE}"<>\\])+`;
const URL_TOKEN = String.raw`(?:https?://|(?<![^${SPACE}])[/?])${URL_BODY}`;
// Fragment-only references own their delimiters without becoming query URLs.
const ASSIGNMENT_URL_TOKEN = String.raw`(?:${URL_TOKEN}|(?<!\S)#${URL_BODY})`;
const URL_WHOLE = new RegExp(`^(?:${URL_TOKEN})$`, "i");
const TEXT = new RegExp(`(${URL_TOKEN})|(${DATA})`, "gi");
const DISCOVERY_URLS = new RegExp(URL_TOKEN, "gi");
const dataText = (value: string, framed = false): string => (framed ? dataURI(value) : undefined) ?? substituteData(value);

// The third bit is a complete, non-media MIME discriminator. It is never set
// for a value at the SDK's truncation boundary (even a syntactically valid prefix).
// The fourth bit is image evidence only; it can tighten omission, never admit text.
type Hint = readonly [boolean, boolean, boolean?, boolean?];
export type PrivacyHints = ReadonlyMap<string, Hint>;
function hint(name: string, value: unknown, valueLimit = Infinity): Hint {
  if (Array.isArray(value)) {
    let m = false, a = false, image = false, safe = value.length > 0;
    for (const item of value) {
      if (typeof item !== "string") { safe = false; continue; }
      const next = hint(name, item, valueLimit); m ||= next[0]; a ||= next[1]; image ||= !!next[3]; safe &&= !!next[2];
    }
    return [m, a, safe && !m && !a, image];
  }
  if (typeof value !== "string") return [false, false];
  const nameLeaf = leaf(name);
  // Do not split/lowercase megabyte exception messages merely to learn that
  // their key is not a MIME/type discriminator.
  if (!mime.names.includes(nameLeaf) && nameLeaf !== "type" && nameLeaf !== "modality") return [false, false];
  const type = value.split(";", 1)[0].trim().toLowerCase();
  if (mime.names.includes(nameLeaf)) {
    const m = mime.prefixes.some(prefix => type.startsWith(prefix)) || mime.exact.includes(type);
    return [m, type.startsWith("audio/"), !m && value.length < valueLimit && /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/.test(type), type.startsWith("image/")];
  }
  const image = nameLeaf === "type" ? catalog.media.image.type_names.includes(normalizePrivacyName(type))
    : nameLeaf === "modality" && catalog.media.image.modality_names.includes(type);
  const video = nameLeaf === "type" ? catalog.media.video.type_names.includes(normalizePrivacyName(type))
    : nameLeaf === "modality" && catalog.media.video.modality_names.includes(type);
  // Video uses the existing MIME payload/metadata boundaries, not image-only
  // URL fields. This bit only tightens omission, including across authored writes.
  return [image || video, nameLeaf === "type" && audio.type_names.includes(normalizePrivacyName(value)), false, image];
}
/** Index all siblings once, preserving exact indexed parents and combining duplicate query hints. */
export function indexPrivacyHints(items: Iterable<readonly [string, unknown]>, path = "", previous: PrivacyHints = new Map(), valueLimit = Infinity): PrivacyHints {
  return hintIndex(items, path, previous, valueLimit, false);
}
/** budgeted: the scrub reads each hint's full name through the path budget (an over-budget name exports the placeholder). */
function hintIndex(items: Iterable<readonly [string, unknown]>, path: string, previous: PrivacyHints, valueLimit: number, budgeted: boolean): PrivacyHints {
  const hints = new Map(previous);
  for (const [name, value] of items) {
    if (budgeted && (typeof value === "string" || (Array.isArray(value) && value.some(item => typeof item === "string")))) pathBudget(path ? `${path}.${name}` : name);
    const next = hint(name, value, valueLimit);
    if (!next[0] && !next[1] && !mime.names.includes(leaf(name))) continue;
    const parts = pathParts(path ? `${path}.${name}` : name);
    while (parts.length && /^\d+$/.test(parts.at(-1)!)) parts.pop();
    parts.pop(); const parent = parts.join("."), old = hints.get(parent);
    const m = next[0] || !!old?.[0], a = next[1] || !!old?.[1];
    hints.set(parent, [m, a, !m && !a && !!next[2] && (old === undefined || !!old[2]), !!next[3] || !!old?.[3]]);
  }
  return hints;
}
function context(path: string, hints: PrivacyHints, inheritedMedia = false, inheritedAudio = false, retainPayload = false) {
  let m = inheritedMedia || !!hints.get("")?.[0], a = inheritedAudio || !!hints.get("")?.[1], payload = false, direct = false;
  const prefix: string[] = [];
  for (const part of pathParts(path)) {
    prefix.push(part); const name = normalizePrivacyName(part);
    if (!/^\d+$/.test(part)) {
      if (audio.metadata_fields.includes(name)) { m = false; a = false; if (!retainPayload) payload = false; }
      direct = audio.fields.includes(name); a ||= direct;
      if (m && mime.payload_fields.includes(name) || a && audio.payload_fields.includes(name)) payload = true;
    }
    const next = hints.get(prefix.join(".")); m ||= !!next?.[0]; a ||= !!next?.[1];
  }
  return { media: m, audio: a, payload: payload || direct };
}
/** Image-specific payload names supplement, never replace, the media/audio policy.
 * In particular, url remains audio metadata and ordinary HTTP image URLs remain
 * text. Only an encoded payload uses this extra image-declaration boundary. */
function declaredImagePayload(path: string, hints: PrivacyHints): boolean {
  let image = !!hints.get("")?.[3], payload = false;
  const prefix: string[] = [];
  for (const part of pathParts(path)) {
    prefix.push(part); const name = normalizePrivacyName(part);
    if (!/^\d+$/.test(part)) {
      const field = catalog.media.image.payload_fields.includes(name);
      if (audio.metadata_fields.includes(name) && !field) { image = false; payload = false; }
      if (image && field) payload = true;
    }
    image ||= !!hints.get(prefix.join("."))?.[3];
  }
  return payload;
}
const zeroDetailDeferral: ReadonlySet<string> = new Set<string>(catalog.scalar_metrics.zero_detail_deferral.keys);
/** Versioned A7 policy data: usage-detail keys whose exact numeric zero may yield projection capacity. */
export function isZeroDetailDeferralKey(key: string): boolean {
  return zeroDetailDeferral.has(key);
}
const metricContainers = new Map(catalog.scalar_metrics.containers.flatMap(container => container.keys.map(key => [key, container.path] as const)));
const integerCountMetrics = new Set(catalog.scalar_metrics.containers.filter(container => container.value_type === "nonnegative_safe_integer").flatMap(container => container.keys));
const canonicalDecimal = new RegExp(catalog.scalar_metrics.canonical_decimal.regex);
const decimalCountMetrics = new Set(catalog.scalar_metrics.canonical_decimal.keys);
function validCount(value: unknown, path: string): boolean {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0;
  // Only the declared byte counter keeps a decimal-string role. ElevenLabs
  // strings and encoded-looking byte payloads use the role-free media policy.
  // Full matching rejects the final newline accepted by JavaScript's '$'.
  return typeof value === "string" && decimalCountMetrics.has(path)
    && value.length <= catalog.scalar_metrics.canonical_decimal.max_chars
    && canonicalDecimal.exec(value)?.[0] === value && Number.isSafeInteger(Number(value))
    && !(value.length >= catalog.scalar_metrics.canonical_decimal.base64_min_chars && encoded(value));
}
// Leaf membership can only tighten omission beneath payload, never grant a role.
const metricLeaves = new Set([...metricContainers.keys()].map(key => pathParts(key).at(-1)!));
function scalarMediaContext(value: unknown, path: string, hints: PrivacyHints, inheritedMedia: boolean, inheritedAudio: boolean, payloadRole = false) {
  const parts = pathParts(path), container = metricContainers.get(path);
  let explicitMedia = inheritedMedia || inheritedAudio;
  for (let i = 0; i <= parts.length; i++) {
    const next = hints.get(parts.slice(0, i).join("."));
    explicitMedia ||= !!next?.[0] || !!next?.[1];
  }
  // The complete key identifies its catalog-declared namespace. No search for
  // a matching suffix within an arbitrary object, and no normalized aliases.
  const scalar = typeof value === "number" || typeof value === "boolean" || typeof value === "string";
  // Confine typed validation to the byte counters. Other G4 scalar roles,
  // including arbitrary detail strings, retain their original behavior.
  // Invalid byte counts fall through to the original structural-media policy.
  const validType = !integerCountMetrics.has(path) || validCount(value, path);
  const metric = scalar && validType && container !== undefined && !explicitMedia && !payloadRole && !isCredentialName(path);
  const ordinary = context(path, hints, inheritedMedia, inheritedAudio).payload;
  const counter = container !== undefined || metricLeaves.has(parts.at(-1) ?? "");
  // JSON ownership can reset a path, never the fact that it came from payload.
  // This private bit only tightens scalar/array policy; metadata text keeps its
  // ordinary transcript/usage boundary instead of inheriting blanket media.
  const payload = ordinary || counter && (payloadRole || explicitMedia || context(path, hints, inheritedMedia, inheritedAudio, true).payload);
  return { metric, payload };
}
const noHints: PrivacyHints = new Map();
const mediaFields = new Set([...media.unrecognized_base64_fields, ...mime.payload_fields, ...audio.fields, ...audio.payload_fields]);
/** Lost context cannot use metadata boundaries to exempt descendants. A complete
 * non-media MIME may identify ordinary text; a missing/truncated MIME never can. */
export function completeNonMediaMime(path: string, hints: PrivacyHints): boolean {
  const parent = pathParts(path);
  while (parent.length && /^\d+$/.test(parent.at(-1)!)) parent.pop();
  parent.pop();
  return !!hints.get(parent.join("."))?.[2];
}
function incompleteMediaPath(path: string): boolean {
  return components(path).some(part => mediaFields.has(normalizePrivacyName(part)));
}
/** The ONE structural-media predicate; returns an omission, or undefined for non-media.
 * Exact scalar boundaries precede context; only free text uses delimiter-limited data scanning.
 * Incomplete is S1's fail-closed mode after SDK/projection context loss, not a
 * change to the golden catalog's complete-input recognition rules.
 */
export function mediaReplacement(value: unknown, path = "", hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, inheritedAudio = false, incomplete = false, ambiguousBase64 = true): string | undefined {
  return replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64);
}
function replaceMedia(value: unknown, path: string, hints: PrivacyHints, framed: boolean, inheritedMedia: boolean, inheritedAudio: boolean, incomplete: boolean, ambiguousBase64 = true, payloadRole = false): string | undefined {
  const { metric, payload } = scalarMediaContext(value, path, hints, inheritedMedia, inheritedAudio, payloadRole);
  const structural = !metric && (payload || incomplete && incompleteMediaPath(path));
  if (typeof value === "string") {
    // FI hide/cap policy already removed the value. Preserve only these exact
    // value-free sentinels, never a prefix or an image-bearing container. The
    // credential checks in walkValue still run before this media-only boundary.
    if (value === "__REDACTED__" || value === "[image-redacted]") return value;
    if (value === replacement || omission.test(value)) return undefined;
    if (framed) { const uri = dataURI(value); if (uri !== undefined) return uri; }
    if (structural) return encoded(value) ? omitted(value) : media.other_replacement;
    if (declaredImagePayload(path, hints) && encoded(value)) return omitted(value);
    if (magic(value)) return omitted(value);
    if (incomplete && ambiguousBase64 && value.length >= 4 && !completeNonMediaMime(path, hints) && encoded(value)) return omitted(value);
    if (value.length > media.unrecognized_base64_min_chars_exclusive && media.unrecognized_base64_fields.includes(leaf(path)) && encoded(value)) return omitted(value);
  } else if (value != null && structural) {
    // Only exact scalar metric roles are exempt; payload numbers/bools and
    // nonempty numeric arrays are media. Other arrays retain recursive walking.
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
function urlValue(original: string, incomplete = false): string {
  return framedUrl(original, incomplete)[0];
}
function isURLValue(value: string, inspect?: (characters: number) => void): boolean {
  // URL_WHOLE requires this prefix too. Ordinary prose needs only this bounded
  // negative gate, not multiple whole-value probes hidden behind one debit.
  const prefix = value.slice(0, 8); inspect?.(prefix.length);
  if (!/^(?:https?:\/\/|[/?])/i.test(prefix)) return false;
  inspect?.(value.length);
  if (URL_WHOLE.test(value)) return true;
  inspect?.(value.length);
  const { base, question, pairs, hash, fragment } = queryParts(value);
  if (!question) return false;
  const masked = base + question + pairs.map(pair => pair.name + pair.equals + (pair.equals && dataURI(pair.decodedValue, inspect) !== undefined ? media.data_uri.other_replacement : pair.value)).join("&") + hash + fragment;
  if (masked === value) return false;
  inspect?.(masked.length);
  return URL_WHOLE.test(masked);
}
// A warm identity is not proof of unchanged contents: even frozen arrays can
// contain accessors. Keep the original filter/iterator reads on EVERY call.
// Only the pure sort is memoized after exact primitive-content validation.
interface PreparedSecrets { input: string[]; ordered: string[] }
let preparedSecrets = new WeakMap<object, PreparedSecrets>();
let preparedSecretAdmissions = 0;
function prepareSecrets(secrets: readonly string[]): readonly string[] {
  const input = [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))];
  const keyed = secrets !== null && (typeof secrets === "object" || typeof secrets === "function");
  let characters = 0;
  const bounded = input.length <= 64 && input.every(secret => typeof secret === "string"
    && secret.length <= 256 && (characters += secret.length) <= 4096);
  if (!keyed || !bounded) {
    if (keyed) preparedSecrets.delete(secrets);
    return input.sort((a, b) => b.length - a.length);
  }
  const cached = preparedSecrets.get(secrets);
  if (cached && cached.input.length === input.length && input.every((secret, i) => secret === cached.input[i])) return cached.ordered;
  // Detach slices before retaining them; the weak key never roots the caller's
  // array. Reset generations, rather than keeping strong keys for FIFO eviction.
  const detached = input.map(secret => secret.split("").join(""));
  const ordered = detached.slice().sort((a, b) => b.length - a.length);
  if (!cached) {
    if (preparedSecretAdmissions >= 64) { preparedSecrets = new WeakMap(); preparedSecretAdmissions = 0; }
    preparedSecretAdmissions++;
  }
  preparedSecrets.set(secrets, { input: detached, ordered });
  return ordered;
}
function knownSecretText(value: string, secrets: readonly string[]): string {
  for (const secret of prepareSecrets(secrets)) {
    // Absent from the whole value implies absent from every sentinel-separated
    // part. This pure no-op has no charge; do NOT apply it to knownSecretRanges.
    // Preserve the original operation for non-string runtime inputs as well.
    if (typeof value === "string" && typeof secret === "string" && value.indexOf(secret) < 0) continue;
    value = value.split(replacement).map(part => part.split(secret).join(replacement)).join(replacement);
  }
  return value;
}
interface SourceRange { start: number; end: number }
/** Match ORIGINAL bytes before partitioning or rewriting. Longest-first removal
 * splits the remaining source gaps, exactly like knownSecretText; inserted
 * sentinels and existing sentinel segments cannot become a new match. */
function knownSecretRanges(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget): SourceRange[] {
  const matches: SourceRange[] = [];
  let gaps: SourceRange[] = [], at = 0;
  for (let next = value.indexOf(replacement); next >= 0; next = value.indexOf(replacement, at)) {
    charge(budget, depth);
    if (next > at) gaps.push({ start: at, end: next });
    at = next + replacement.length;
  }
  if (at < value.length) gaps.push({ start: at, end: value.length });
  for (const secret of prepareSecrets(secrets)) {
    charge(budget, depth);
    const remaining: SourceRange[] = [];
    for (const gap of gaps) {
      charge(budget, depth);
      let start = gap.start;
      for (let found = value.indexOf(secret, start); found >= 0 && found + secret.length <= gap.end; found = value.indexOf(secret, start)) {
        charge(budget, depth);
        if (found > start) remaining.push({ start, end: found });
        matches.push({ start: found, end: found + secret.length });
        start = found + secret.length;
      }
      if (start < gap.end) remaining.push({ start, end: gap.end });
    }
    gaps = remaining;
  }
  return matches.sort((a, b) => a.start - b.start);
}
const wholeSecret = (value: string, secrets: readonly string[]) => value.length >= 8 && value !== replacement && value !== "__REDACTED__" && secrets.includes(value);
function credentialText(value: string, secrets: readonly string[]): string {
  value = knownSecretText(value, secrets);
  for (const shape of valueShapes) value = typeof shape === "function" ? shape(value) : value.replace(shape, replacement);
  return value;
}
const isRunCode = (c: number) => (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95 || c === 45;
const PY_WORD_CHAR = new RegExp(`[${PY_WORD}]`, "u");
/** Whether the code point ending at index i is a Python \\w character (the word side of \\b). */
function wordBefore(value: string, i: number): boolean {
  if (i <= 0) return false;
  const low = value.charCodeAt(i - 1);
  const start = low >= 0xdc00 && low <= 0xdfff && i >= 2 && value.charCodeAt(i - 2) >= 0xd800 && value.charCodeAt(i - 2) <= 0xdbff ? i - 2 : i - 1;
  return PY_WORD_CHAR.test(String.fromCodePoint(value.codePointAt(start)!));
}
/**
 * `\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}` replaced by the marker, in time
 * linear in the text. The classes exclude '.', so each run of [A-Za-z0-9_.-] splits at dots into
 * deterministic segments. A token is a header segment holding an 'eyJ' (at the segment start, or
 * after a '-', the only non-word characters inside a run) with at least 8 characters after it,
 * followed by two segments of at least 8 characters; it ends where the third segment ends. The
 * leftmost eyJ of the header segment decides (a later one has fewer characters after it and the
 * same two following segments). A failed triple advances one segment; a match resumes after it.
 * Each character is read a constant number of times.
 */
function replaceJwt(value: string): string {
  if (!value.includes("eyJ")) return value;
  const n = value.length;
  let result = "", copied = 0;
  for (let i = 0; i < n;) {
    if (!isRunCode(value.charCodeAt(i)) && value.charCodeAt(i) !== 46) { i++; continue; }
    // One maximal run [A-Za-z0-9_.-] and its dot-separated segments.
    const starts: number[] = [], ends: number[] = [];
    let j = i;
    for (let segStart = i; ; j++) {
      const c = j < n ? value.charCodeAt(j) : -1;
      if (c === 46 || !(c !== -1 && isRunCode(c))) {
        starts.push(segStart); ends.push(j);
        if (c !== 46) break;
        segStart = j + 1;
      }
    }
    for (let k = 0; k + 2 < starts.length;) {
      const a = starts[k], b = ends[k];
      let found = -1;
      for (let p = a; p + 3 <= b; p++) {
        if (value.charCodeAt(p) === 101 && value.charCodeAt(p + 1) === 121 && value.charCodeAt(p + 2) === 74 && (p === a ? !wordBefore(value, a) : value.charCodeAt(p - 1) === 45)) { found = p; break; }
      }
      if (found !== -1 && b - found - 3 >= 8 && ends[k + 1] - starts[k + 1] >= 8 && ends[k + 2] - starts[k + 2] >= 8) {
        result += value.slice(copied, found) + replacement;
        copied = ends[k + 2];
        k += 3;
      } else k++;
    }
    i = j;
  }
  return copied === 0 ? value : result + value.slice(copied);
}
function bareMedia(value: string): string {
  // Only free-text gaps enter this lexer, never reconstructed URL/JSON syntax.
  return value.replace(/[A-Za-z0-9+/_=-]{8,}/g, (candidate, offset: number) => {
    // A scheme's authority delimiter is URL syntax, not an MP3 frame. Scan only
    // the adjacent scheme; never exempt the rest of the URL or rescan its prefix.
    if (candidate.startsWith("//") && value[offset - 1] === ":") {
      let start = offset - 1;
      while (start > 0 && /[A-Za-z0-9+.-]/.test(value[start - 1])) start--;
      if (start < offset - 1 && /[A-Za-z]/.test(value[start])) return candidate;
    }
    return candidate !== "__REDACTED__" && magic(candidate) ? omitted(candidate) : candidate;
  });
}
function text(value: string, secrets: readonly string[], framed: boolean, incomplete = false, pairs = false, bare = true): string {
  // This function only receives prose gaps, names or URL-owned values. Match
  // original credentials before URL query/userinfo rewrites can split them.
  value = knownSecretText(value, secrets);
  if (framed && isURLValue(value)) return wholeUrlText(value, secrets, incomplete);
  // A private key runs across URL-like tokens and lines, and a token read on its own (a leading '/' or '?') may be rewritten
  // into a form that no longer holds the key's header: read it once over the whole prose first.
  value = privateKeyText(value);
  let end = 0, result = "";
  // Each data URI is cut where its continuation lines stop being base64; the rest is read as text.
  for (const [start, stop, data] of scanText(typeof value === "string" ? TEXT_SCAN : new RegExp(TEXT.source, TEXT.flags), value, 2)) {
    const original = value.slice(start, stop);
    const gap = credentialText(value.slice(end, start), secrets);
    result += bare ? bareMedia(gap) : gap;
    if (!data) {
      const closing = start > 0 && value[start - 1] === "'" && original.endsWith("'") ? "'" : "";
      const token = closing ? original.slice(0, -1) : original;
      // A complete bare JPEG/MP3 can also begin with '/'. Check the WHOLE
      // framed token, never a slash-prefixed substring of an authority/path.
      result += (mediaReplacement(token) ?? urlValue(token, incomplete)) + closing;
    } else result += dataText(original);
    end = stop;
  }
  const tail = credentialText(value.slice(end), secrets);
  result += bare ? bareMedia(tail) : tail;
  // Known values/shapes still apply to URI components and across text gaps.
  // Do not run bare-media recognition again over the assembled syntax.
  result = credentialText(result, secrets);
  // The pair rule runs last, after known secrets and value shapes.
  return pairs ? credentialPairs(result) : result;
}
// ---- Shared privacy spec v4: credential pair rule, URL catalog pairs, vocabulary and name budgets. ----
// generate_privacy_fixtures.py (class Reference) is the executable reference. Positions are UTF-16 units, as in Java.
/** A name of more than limits.max_depth components: the whole exported value becomes the placeholder (fail closed). */
class PrivacyPathBudget extends Error { constructor() { super("Privacy path budget"); this.name = "PrivacyPathBudget"; } }
const maxDepth = catalog.limits.max_depth;
function isPathOverBudget(name: string): boolean {
  try { pathBudget(name); return false; } catch (error) { if (error instanceof PrivacyPathBudget) return true; throw error; }
}
function pathBudget(name: string): void {
  let separators = 0;
  for (let k = 0; k < name.length; k++) { const c = name.charCodeAt(k); if (c === 46 || c === 91 || c === 93) separators++; }
  if (separators >= maxDepth && name.split(/[.\[\]]+/).filter(Boolean).length > maxDepth) throw new PrivacyPathBudget();
}
/** Java Pattern semantics for the exclusions: '.' matches no line terminator (LF, CR, U+0085, U+2028, U+2029). */
const javaExclusions = exclusions.map(({ regex, guards }) => ({ regex: new RegExp(regex.source.replace(/\./g, "[^\\n\\r\\u0085\\u2028\\u2029]")), guards }));
const javaExcluded = (folded: string) => javaExclusions.some(({ regex, guards }) => regex.test(folded) && !guards.some(marker => folded.includes(marker)));
const vocabularyWords: ReadonlySet<string> = new Set(catalog.credentials.vocabulary.words);
const VOCABULARY_LETTER = /[\p{Lu}\p{Ll}\p{Lt}\p{Lm}\p{Lo}\p{Nd}]/u, UPPERCASE = /\p{Uppercase}/u, LOWERCASE = /\p{Lowercase}/u;
const javaLower = (c: string) => { const lowered = c.toLowerCase(); return lowered.length === 1 ? lowered : c === "İ" ? "i" : c; };
/** Whether a segment of the name is a vocabulary word, over UTF-16 units: a segment is a run of letters and decimal digits,
 * split at every other unit (each surrogate half included) and at a lowercase unit followed by an uppercase one. */
function vocabulary(name: string): boolean {
  const lowered = name.replace(/İ/g, "i").toLowerCase();
  let any = false;
  for (const word of vocabularyWords) if (lowered.includes(word)) { any = true; break; }
  if (!any) return false;
  let word = "", previous: string | undefined;
  for (let k = 0; k <= name.length; k++) {
    const unit = k < name.length ? name.charCodeAt(k) : 45, c = String.fromCharCode(unit);
    const surrogate = unit >= 0xd800 && unit <= 0xdfff;
    const letter = !surrogate && VOCABULARY_LETTER.test(c);
    const boundary = !letter || (previous !== undefined && UPPERCASE.test(c) && LOWERCASE.test(previous));
    if (boundary && word) { if (vocabularyWords.has(word)) return true; word = ""; }
    if (letter) word += javaLower(c);
    previous = surrogate ? undefined : c;
  }
  return false;
}
const markedName = (name: string) => { const folded = normalizePrivacyName(name); return patterns.some(pattern => folded.includes(pattern)) || vocabulary(name); };
/** The catalog's credential name rule, path budget included: patterns, exclusions and vocabulary words per component, then on
 * the whole name. Throws PrivacyPathBudget for a name of more than max_depth components. */
function credentialName(name: string): boolean {
  pathBudget(name);
  if (!markedName(name)) return false;
  const context: string[] = [];
  let exempt = false;
  for (const part of components(name)) {
    const folded = normalizePrivacyName(part);
    context.push(folded);
    if (javaExcluded(folded) || contextual.some(regex => regex.test(context.join(".")))) exempt = true;
    else if (patterns.some(pattern => folded.includes(pattern)) || vocabulary(part)) return true;
  }
  if (exempt) return false;
  return !javaExcluded(normalizePrivacyName(name));
}
const queryName = (name: string) => signed.has(normalizePrivacyName(name)) || credentialName(name);

const PAIR_NAME_CHARS = new Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-[]");
const PAIR_NAME_START = new Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_");
const PAIR_PADDING_FOLLOWERS = new Set("= \t\n\x0b\x0c\r,;)]}\"'");
const PAIR_LIST_HEADERS = new Set(["cookie", "setcookie", "authorization", "proxyauthorization"]);
const NAME_CAP = 128, QUESTION_CAP = 8, GAP_CAP = 8;
const ESCAPED_QUOTE = '\\"', ENTITY_QUOTE = "&quot;";
const ENCODED_EQUALS = new Set(["3D", "3d"]);
const PATH_PAIR_SEPARATORS = new Set(";,&");
const ENCODED_SPACES = new Set(["20", "09", "0A", "0B", "0C", "0D"]);
const ENCODED_NBSP = "%C2%A0";
const JSON_SPACE_ESCAPES = new Set("tnrfb");
const HEX_DIGITS = new Set("0123456789abcdefABCDEF");
const JAVA_FINAL_TERMINATORS = new Set(["\r\n", "\n", "\r", "\u0085", " ", " "]);
const LINE_BREAKS = new Set("\n\r\u0085  \x0c\x0b");
const JSON_SCALAR_CHARS = new Set("0123456789+-.eEtrufalsn");
const JSON_NUMBER = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;
const PERCENT_ESCAPE = /%[0-9A-Fa-f]{2}/;
const CONTROL = /^\p{Cc}$/u;
const has = (set: ReadonlySet<string>, c: string | undefined) => c !== undefined && set.has(c);
/** Python's str.isspace for one character: the reference's whitespace set (JS \s differs). */
function pySpace(c: string | undefined): boolean {
  if (c === undefined || c.length !== 1) return false;
  const code = c.charCodeAt(0);
  return (code >= 9 && code <= 13) || (code >= 0x1c && code <= 0x20) || code === 0x85 || code === 0xa0 || code === 0x1680
    || (code >= 0x2000 && code <= 0x200a) || code === 0x2028 || code === 0x2029 || code === 0x202f || code === 0x205f || code === 0x3000;
}
/** Java's Character.isWhitespace. */
function javaSpace(c: string | undefined): boolean {
  if (c === undefined) return false;
  const code = c.charCodeAt(0);
  return (code >= 9 && code <= 13) || (code >= 0x1c && code <= 0x20) || code === 0x1680 || (code >= 0x2000 && code <= 0x2006)
    || (code >= 0x2008 && code <= 0x200a) || code === 0x2028 || code === 0x2029 || code === 0x205f || code === 0x3000;
}
const hexSpace = (digits: string) => pySpace(String.fromCharCode(parseInt(digits, 16)));
const allHex = (digits: string) => digits.length === 4 && [...digits].every(c => HEX_DIGITS.has(c));
/** Percent-decode once ('+' stays), UTF-8 with replacement, as urllib's unquote. */
const UTF8 = new TextDecoder();
/** urllib's unquote: each run of %XX escapes is decoded as UTF-8 with replacement; every other character stays as it is. */
function percentDecode(value: string, plus: boolean): string {
  if (plus) value = value.replace(/\+/g, " ");
  if (!value.includes("%")) return value;
  return value.replace(/(?:%[0-9A-Fa-f]{2})+/g, run => {
    const bytes = new Uint8Array(run.length / 3);
    for (let k = 0; k < bytes.length; k++) bytes[k] = parseInt(run.slice(3 * k + 1, 3 * k + 3), 16);
    return UTF8.decode(bytes);
  });
}
const unquote = (value: string) => percentDecode(value, false);
const unquotePlus = (value: string) => percentDecode(value, true);

function nextPair(text: string, start: number, spaced: boolean): boolean {
  const length = text.length;
  let i = start;
  if (spaced) {
    while (i < length && (text[i] === " " || text[i] === "\t") && i - start <= 8) i++;
    if (i - start > 8) return false;
  }
  const dashes = i;
  while (i < length && text[i] === "-" && i - dashes <= 2) i++;
  if (i - dashes > 2 || i >= length || !PAIR_NAME_START.has(text[i])) return false;
  const name = i;
  i++;
  while (i < length && PAIR_NAME_CHARS.has(text[i]) && i - name < 128) i++;
  if (i < length && PAIR_NAME_CHARS.has(text[i])) return false;
  if (spaced) {
    const gap = i;
    while (i < length && text[i] === " " && i - gap <= 8) i++;
    if (i - gap > 8) return false;
  }
  if (i >= length) return false;
  if (text[i] === ":") return true;
  if (text[i] !== "=") return false;
  const rest = i + 1;
  return !(rest >= length || PAIR_PADDING_FOLLOWERS.has(text[rest]) || (length - rest <= 2 && JAVA_FINAL_TERMINATORS.has(text.slice(rest))));
}
function pairValueEnd(text: string, start: number): number {
  const length = text.length;
  if (start >= length) return length;
  if (text.startsWith("-----BEGIN", start)) {
    const end = text.indexOf("-----END", start);
    if (end < 0) return length;
    const close = text.indexOf("-----", end + 8);
    return close < 0 ? length : close + 5;
  }
  const first = text[start];
  let i = start;
  if (first === '"' || first === "'") {
    i = start + 1;
    while (i < length && text[i] !== first) i += text[i] === "\\" ? 2 : 1;
    if (i >= length) return length;
    i++;
  }
  let depth = 0;
  for (; i < length; i++) {
    const c = text[i];
    if (c === "(" || c === "[" || c === "{") depth++;
    else if (c === ")" || c === "]" || c === "}") depth = Math.max(depth - 1, 0);
    else if (depth === 0) {
      if ((c === "," || c === ";" || c === "\n" || c === "\r") && nextPair(text, i + 1, true)) return i;
      if (c === " " && nextPair(text, i + 1, false)) return i;
    }
  }
  return length;
}
function encodedSpaceWidth(text: string, end: number, floor: number): number {
  if (end - 3 >= floor && text[end - 3] === "%" && ENCODED_SPACES.has(text.slice(end - 2, end).toUpperCase())) return 3;
  if (end - 6 >= floor && text.slice(end - 6, end).toUpperCase() === ENCODED_NBSP) return 6;
  let width = 0;
  if (end - 2 >= floor && text[end - 2] === "\\" && has(JSON_SPACE_ESCAPES, text[end - 1])) width = 2;
  else if (end - 6 >= floor && text.slice(end - 6, end - 4) === "\\u" && allHex(text.slice(end - 4, end)) && hexSpace(text.slice(end - 4, end))) width = 6;
  if (!width) return 0;
  let slashes = 1;
  while (end - width - slashes >= floor && text[end - width - slashes] === "\\") slashes++;
  return slashes % 2 ? width : 0;
}
function gapStart(text: string, end: number, floor: number): number {
  while (end > floor) {
    if (pySpace(text[end - 1])) { end--; continue; }
    const width = encodedSpaceWidth(text, end, floor);
    if (!width) break;
    end -= width;
  }
  return end;
}
const escapeBefore = (text: string, end: number, floor: number) => end - 3 >= floor && text[end - 3] === "%" && has(HEX_DIGITS, text[end - 2]) && has(HEX_DIGITS, text[end - 1]);
function escapedName(text: string, nameStart: number, nameEnd: number, floor: number): [string, boolean] | undefined {
  if (!((escapeBefore(text, nameEnd, floor) && !ENCODED_EQUALS.has(text.slice(nameEnd - 2, nameEnd)))
    || (nameStart > floor && escapeBefore(text, nameStart, floor) && text.slice(nameStart - 2, nameStart) === "25"))) return undefined;
  let start = nameEnd;
  while (start > floor && nameEnd - start < NAME_CAP) {
    if (escapeBefore(text, start, floor)) {
      if (ENCODED_EQUALS.has(text.slice(start - 2, start))) break;
      start -= 3;
    } else if (PAIR_NAME_CHARS.has(text[start - 1])) start--;
    else break;
  }
  const overflow = nameEnd - start >= NAME_CAP && start > floor && (PAIR_NAME_CHARS.has(text[start - 1]) || escapeBefore(text, start, floor));
  const decoded = unquote(text.slice(start, nameEnd));
  const undecided = overflow || PERCENT_ESCAPE.test(decoded) || !/[\p{L}\p{N}_]/u.test(decoded);
  return [decoded, undecided];
}
const DATA_LINE = /[\r\n]+([A-Za-z0-9+/_=\-]+)/y;
const BASE64_INDEX = new Map<string, number>([..."ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"].map((c, k) => [c, k] as [string, number]).concat([["-", 62], ["_", 63]]));
function padding(body: string, pad: number, base64: boolean, padded: boolean, count: number): boolean {
  if (!base64 || padded || pad > 2 || !body || body.startsWith("-") || (count + body.length + pad) % 4) return false;
  return BASE64_INDEX.get(body[body.length - 1])! % (pad === 1 ? 4 : 16) === 0;
}
/** The end of the data URI matched at text[start:end]: a continuation line belongs to it only if its '=' characters are
 * trailing base64 padding; a line followed by a separator is a pair name. */
function dataUriEnd(text: string, start: number, end: number): number {
  let head = start + 5;
  while (head < end && text[head] !== "\r" && text[head] !== "\n") head++;
  const [metadata, comma, payload] = partition(text.slice(start + 5, head), ",");
  const base64 = !!comma && metadata.toLowerCase().endsWith(";base64");
  const decoded = unquote(payload);
  let count = 0;
  for (const ch of decoded) if (BASE64_INDEX.has(ch)) count++;
  let padded = decoded.includes("="), position = head;
  while (position < end) {
    DATA_LINE.lastIndex = position;
    const line = DATA_LINE.exec(text);
    if (!line || line.index + line[0].length > end) break;
    const after = line.index + line[0].length;
    let gap = after;
    while (gap < text.length && (dashesSpace(text[gap]) || encodedSpaceAhead(text, gap))) gap += encodedSpaceAhead(text, gap) || 1;
    if (gap < text.length && (text[gap] === "=" || text[gap] === ":" || text.startsWith("%3D", gap) || text.startsWith("%3d", gap))) break;
    const body = line[1].replace(/=+$/, ""), pad = line[1].length - body.length;
    if (body.startsWith("--") || body.includes("=") || (pad && !padding(body, pad, base64, padded, count))) break;
    count += body.length; padded = padded || !!pad; position = after;
  }
  return position;
}
/** The matches of a DATA or TEXT scanner (global, case-insensitive) left to right, each data URI cut by dataUriEnd. */
function* scanText(pattern: RegExp, text: string, dataGroup: number): Generator<[number, number, boolean]> {
  let position = 0;
  while (position <= text.length) {
    pattern.lastIndex = position;
    const match = pattern.exec(text);
    if (!match) return;
    const data = dataGroup === 0 || match[dataGroup] !== undefined;
    const end = data ? dataUriEnd(text, match.index, match.index + match[0].length) : match.index + match[0].length;
    yield [match.index, end, data];
    position = end > match.index ? end : match.index + 1;
  }
}
const DATA_SCAN = new RegExp(DATA, "gi");
const TEXT_SCAN = new RegExp(TEXT.source, TEXT.flags);
function substituteData(value: string): string {
  let out = "", copied = 0;
  for (const [start, end] of scanText(DATA_SCAN, value, 0)) {
    const original = value.slice(start, end);
    out += value.slice(copied, start) + (dataURI(original) ?? original);
    copied = end;
  }
  return copied ? out + value.slice(copied) : value;
}
const dashesSpace = (c: string | undefined) => pySpace(c) && !LINE_BREAKS.has(c!);
const wordSpace = pySpace;
function encodedControl(text: string, start: number, width: number): boolean {
  if (text[start] === "%") return width === 3 && text.slice(start + 1, start + 3) !== "20";
  if (width === 2) return true;
  return CONTROL.test(String.fromCharCode(parseInt(text.slice(start + 2, start + 6), 16)));
}
function separatorAt(text: string, k: number): number {
  const c = text[k];
  if (c === "=" || (c === ":" && !text.startsWith("//", k + 1))) return 1;
  return c === "%" && ENCODED_EQUALS.has(text.slice(k + 1, k + 3)) ? 3 : 0;
}
function separatorValueEnd(text: string, k: number, size: number): number {
  let value = k + size;
  while (value < text.length && value - k - size < 8 && text[value] === " ") value++;
  let name = k;
  while (name > 0 && k - name < NAME_CAP && PAIR_NAME_CHARS.has(text[name - 1])) name--;
  if (PAIR_LIST_HEADERS.has(normalizePrivacyName(text.slice(name, k)))) {
    while (value < text.length && text[value] !== "\n" && text[value] !== "\r") value++;
    return value;
  }
  return pairValueEnd(text, value);
}
function consumedEnd(text: string, start: number, stop: number, decide: (k: number, size: number, floor: number) => number): number {
  while (stop > start) {
    let size: number;
    if ((text[stop - 1] === "=" || text[stop - 1] === ":") && separatorAt(text, stop - 1)) size = 1;
    else if (stop - 3 >= start && separatorAt(text, stop - 3) === 3) size = 3;
    else return stop;
    const end = decide(stop - size, size, start);
    if (end <= stop) return stop;
    stop = end;
  }
  return stop;
}
function encodedSpaceAhead(text: string, start: number): number {
  if (text.startsWith("%", start)) {
    if (ENCODED_SPACES.has(text.slice(start + 1, start + 3).toUpperCase())) return 3;
    return text.slice(start, start + 6).toUpperCase() === ENCODED_NBSP ? 6 : 0;
  }
  if (text.startsWith("\\", start)) {
    const next = text.slice(start + 1, start + 2);
    if (next && JSON_SPACE_ESCAPES.has(next)) return 2;
    const digits = text.slice(start + 2, start + 6);
    if (next === "u" && allHex(digits) && hexSpace(digits)) return 6;
  }
  return 0;
}
const dashesSeparator = (text: string, start: number) => start < text.length && (text[start] === "=" || text[start] === ":" || (text[start] === "%" && ENCODED_EQUALS.has(text.slice(start + 1, start + 3))));
function flagDashes(text: string, i: number): number {
  if (text.startsWith("--", i)) return 2;
  return text.startsWith("-", i) && has(PAIR_NAME_START, text[i + 1]) ? 1 : 0;
}
function readableGap(text: string, start: number, end: number): boolean {
  if (end - start > GAP_CAP) return false;
  for (let k = start; k < end; k++) if (text[k] !== " ") return false;
  return true;
}
function quoteBefore(text: string, end: number): string | undefined {
  if (end >= ENTITY_QUOTE.length && text.startsWith(ENTITY_QUOTE, end - ENTITY_QUOTE.length)) return ENTITY_QUOTE;
  if (end < 1 || (text[end - 1] !== '"' && text[end - 1] !== "'")) return undefined;
  if (text[end - 1] === '"') {
    let slashes = 0;
    while (end - 2 - slashes >= 0 && text[end - 2 - slashes] === "\\" && slashes < NAME_CAP) slashes++;
    if (slashes % 2) return ESCAPED_QUOTE;
  }
  return text[end - 1];
}
type QuotedName = [opening: number, quote: string, close: number, overflow: boolean];
/** Python's str.rfind(sub, start, end). */
function rfind(text: string, sub: string, start: number, end: number): number {
  // Read back no further than start: lastIndexOf has no lower bound and would scan to the beginning of the text on a miss.
  for (let k = Math.min(end, text.length) - sub.length; k >= start; k--) if (text.startsWith(sub, k)) return k;
  return -1;
}
/** Python's str.find(sub, start, end). */
function find(text: string, sub: string, start: number, end: number): number {
  // Read on no further than end: indexOf has no upper bound and would scan to the end of the text on a miss.
  for (let k = start, last = Math.min(end, text.length) - sub.length; k <= last; k++) if (text.startsWith(sub, k)) return k;
  return -1;
}
function jsonName(text: string, colon: number): QuotedName | undefined {
  const end = gapStart(text, colon, 0);
  const quote = quoteBefore(text, end);
  if (quote === undefined || (quote.length === 1 && end < 3)) return undefined;
  const close = end - quote.length;
  const floor = Math.max(0, close - 130);
  const opening = rfind(text, quote, floor, close);
  if (opening >= 0) return [opening, quote, close, !readableGap(text, end, colon)];
  return floor > 0 ? [-1, quote, close, true] : undefined;
}
function jsonStringEnd(text: string, start: number): number {
  const quote = text[start];
  for (let i = start + 1; i < text.length;) {
    if (text[i] === "\\") { i += 2; continue; }
    if (text[i] === quote) return i + 1;
    i++;
  }
  return text.length;
}
function jsonMemberBoundary(text: string, end: number): boolean {
  while (end < text.length && javaSpace(text[end])) end++;
  return end >= text.length || text[end] === "," || text[end] === "}" || text[end] === "]";
}
function escapedStringEnd(text: string, start: number): number {
  const length = text.length;
  let i = start + ESCAPED_QUOTE.length, innerEscape = false;
  while (i < length) {
    if (text[i] === "\\" && i + 1 < length) {
      const outer = text[i + 1];
      i += 2;
      if (innerEscape) innerEscape = false;
      else if (outer === "\\") innerEscape = true;
      else if (outer === '"') return i;
    } else { i++; innerEscape = false; }
  }
  return length;
}
function entityStringEnd(text: string, start: number): number {
  const length = text.length, size = ENTITY_QUOTE.length;
  let i = start + size;
  while (i < length) {
    if (text[i] === "\\") i += 1 + (text.startsWith(ENTITY_QUOTE, i + 1) ? size : 1);
    else if (text.startsWith(ENTITY_QUOTE, i)) return i + size;
    else i++;
  }
  return length;
}
function jsonValueEnd(text: string, start: number, listHeader = false, quote = '"'): number {
  const length = text.length;
  if (start >= length) return start;
  if (quote === ENTITY_QUOTE && text.startsWith(ENTITY_QUOTE, start)) return entityStringEnd(text, start);
  if (quote === ESCAPED_QUOTE && text.startsWith(ESCAPED_QUOTE, start)) return escapedStringEnd(text, start);
  const first = text[start];
  if (first === '"' || first === "'") return jsonStringEnd(text, start);
  let end = start;
  while (end < length && JSON_SCALAR_CHARS.has(text[end])) end++;
  if ((end >= length || ",}] \t\r\n".includes(text[end])) && jsonMemberBoundary(text, end)) {
    const scalar = text.slice(start, end);
    if (scalar === "true" || scalar === "false" || scalar === "null" || JSON_NUMBER.test(scalar)) return end;
  }
  if (listHeader) {
    while (start < length && text[start] !== "\n" && text[start] !== "\r") start++;
    return start;
  }
  return pairValueEnd(text, start);
}
/** The catalog's value shapes as read at a --name's value start: a leading word boundary is dropped (sticky). */
const wordShapes = catalog.credentials.value_shapes.map(shape => {
  const known = pyShape(shape);
  return known ? new RegExp(known.body, "uy")
    : new RegExp(shape.regex.startsWith("\\b") ? shape.regex.slice(2) : shape.regex, (shape.flags.includes("i") ? "i" : "") + "y");
});
/** A credential name for the pair rule, with the command-line and bracket spellings undone first. */
function pairName(name: string): boolean {
  if (!name) return false;
  let stripped = name.replace(/^-+/, "");
  if (stripped.startsWith("D") && stripped.length > 1 && name.startsWith("-D")) stripped = stripped.slice(1);
  stripped = stripped.replace(/[\[\]]/g, ".");
  return !!stripped && (credentialName(stripped) || credentialName(name));
}
type SeparatorPair = [pair: boolean, name: string, overflow: boolean];
function separatorPair(text: string, i: number, floor: number, credential: (name: string) => boolean): SeparatorPair {
  const nameEnd = gapStart(text, i, floor);
  const readable = readableGap(text, nameEnd, i);
  let nameStart = nameEnd;
  while (nameStart > floor && nameEnd - nameStart < NAME_CAP && PAIR_NAME_CHARS.has(text[nameStart - 1]) && !escapeBefore(text, nameStart, floor)) nameStart--;
  const overflow = nameEnd - nameStart >= NAME_CAP && nameStart > floor && PAIR_NAME_CHARS.has(text[nameStart - 1]) && !escapeBefore(text, nameStart, floor);
  if (overflow) pathBudget(text.slice(nameStart, nameEnd));
  const escaped = overflow ? undefined : escapedName(text, nameStart, nameEnd, floor);
  const name = escaped === undefined ? text.slice(nameStart, nameEnd) : escaped[0];
  const pair = (nameEnd - nameStart >= 2 && (overflow || !readable || credential(text.slice(nameStart, nameEnd))))
    || (escaped !== undefined && (escaped[1] || !readable || credential(escaped[0])));
  return [pair, name, overflow];
}
function flagAhead(text: string, start: number, credential: (name: string) => boolean): boolean {
  const dashes = flagDashes(text, start);
  if (dashes !== 1) return dashes === 2;
  let end = start + 1;
  while (end < text.length && PAIR_NAME_CHARS.has(text[end]) && end - start <= NAME_CAP) end++;
  if (end - start < 3 || end - start > NAME_CAP || end >= text.length || !pySpace(text[end])) return false;
  let after = end;
  while (after < text.length && pySpace(text[after])) after++;
  return after < text.length && credential(text.slice(start, end));
}
function lineStartsPair(text: string, start: number, credential: (name: string) => boolean): boolean {
  const length = text.length;
  let k = -1;
  for (const quote of [ENTITY_QUOTE, ESCAPED_QUOTE, '"', "'"]) {
    if (text.startsWith(quote, start)) {
      const close = find(text, quote, start + quote.length, Math.min(length, start + quote.length + 130));
      if (close < 0) return false;
      k = close + quote.length;
      break;
    }
  }
  if (k < 0) {
    if (flagAhead(text, start, credential)) return true;
    let name = start;
    while (name < length && text[name] === "-" && name - start < 2) name++;
    k = name;
    while (k < length && k - name < NAME_CAP) {
      if (PAIR_NAME_CHARS.has(text[k])) k++;
      else if (k + 3 <= length && escapeBefore(text, k + 3, k) && !ENCODED_EQUALS.has(text.slice(k + 1, k + 3)) && !encodedSpaceAhead(text, k)) k += 3;
      else break;
    }
    if (k === name) return false;
  }
  while (k < length && (dashesSpace(text[k]) || encodedSpaceAhead(text, k))) k += encodedSpaceAhead(text, k) || 1;
  return k < length && !!separatorAt(text, k);
}
interface Span { start: number; end: number; omission: boolean }
/** The credential pair rule: replace the value of every credential-named (or undecided) pair with the placeholder, in one
 * left-to-right pass linear in the text. skip holds the spans the catalog itself replaced in a whole URL value, by position. */
function credentialPairs(text: string, skipSpans: readonly Span[] = []): string {
  const length = text.length, out: string[] = [], known = new Map<string, boolean>();
  let copied = 0, i = 0, dashesColon = -1;
  // The '://' userinfo lookup reads the word after each '://' (no further than 1024 characters) for its last '@'. Both answers
  // come from state kept across '://' marks: the end of the current run of non-space characters, and every '@' position, so
  // the work for the whole text stays linear.
  let spaceFrom = -1, spaceAt = -1, atMarks: number[] | undefined;
  const wordLimit = (from: number): number => {
    if (spaceFrom < 0 || from < spaceFrom || from > spaceAt) {
      spaceFrom = from; spaceAt = from;
      while (spaceAt < length && !javaSpace(text[spaceAt])) spaceAt++;
    }
    return spaceAt;
  };
  const lastAt = (start: number, end: number): number => {
    if (!atMarks) { atMarks = []; for (let k = text.indexOf("@"); k >= 0; k = text.indexOf("@", k + 1)) atMarks.push(k); }
    let low = 0, high = atMarks.length;
    while (low < high) { const mid = (low + high) >> 1; if (atMarks[mid] < end) low = mid + 1; else high = mid; }
    return low > 0 && atMarks[low - 1] >= start ? atMarks[low - 1] : -1;
  };
  const skip = skipSpans.filter(span => !span.omission), starts = skip.map(span => span.start);
  const credential = (name: string) => {
    let value = known.get(name);
    if (value === undefined) { value = pairName(name); known.set(name, value); }
    return value;
  };
  const skipped = (position: number) => {
    let lo = 0, hi = starts.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (starts[mid] <= position) lo = mid + 1; else hi = mid; }
    const k = lo - 1;
    return k >= 0 && position < skip[k].end ? skip[k].end : -1;
  };
  const quotedPair = (quoted: QuotedName): [boolean, string] => {
    const [opening, quote, close, overflow] = quoted;
    const name = opening < 0 ? "" : text.slice(opening + quote.length, close);
    let plain = name.length;
    while (plain > 0 && PAIR_NAME_CHARS.has(name[plain - 1]) && !escapeBefore(name, plain, 0)) plain--;
    const escaped = opening >= 0 ? escapedName(name, plain, name.length, 0) : undefined;
    return [overflow || credential(name) || (escaped !== undefined && (escaped[1] || credential(escaped[0]))), name];
  };
  const equalsValueEnd = (k: number, size: number, name: string) => {
    let value = k + size;
    while (value < length && value - k - size < 8 && text[value] === " ") value++;
    if (PAIR_LIST_HEADERS.has(normalizePrivacyName(name))) {
      while (value < length && text[value] !== "\n" && text[value] !== "\r") value++;
      return value;
    }
    return pairValueEnd(text, value);
  };
  const decide = (k: number, size: number, floor: number, readEnd = true): number => {
    const quoted = jsonName(text, k);
    if (quoted !== undefined && quoted[0] >= floor) {
      const [pair, name] = quotedPair(quoted);
      if (size === 1 && text[k] === ":") {
        if (!pair) return -1;
        if (!readEnd) return k;
        let value = k + 1;
        while (value < length && javaSpace(text[value])) value++;
        return jsonValueEnd(text, value, PAIR_LIST_HEADERS.has(normalizePrivacyName(name)), quoted[1]);
      }
      if (pair) return readEnd ? equalsValueEnd(k, size, name) : k;
    }
    if (!separatorPair(text, k, floor, credential)[0]) return -1;
    return readEnd ? separatorValueEnd(text, k, size) : k;
  };
  const quotedEquals = (k: number) => {
    const quoted = k > 0 ? jsonName(text, k) : undefined;
    return quoted !== undefined && quotedPair(quoted)[0];
  };
  const wordEnd = (begin: number, value: number): number => {
    let valueEnd = value, word = value;
    if (text[value] === '"' || text[value] === "'") {
      valueEnd = value + 1;
      while (valueEnd < length && text[valueEnd] !== text[value]) valueEnd += text[valueEnd] === "\\" ? 2 : 1;
      valueEnd = word = Math.min(length, valueEnd + 1);
    }
    let shapeEnd = value;
    for (const shape of wordShapes) {
      shape.lastIndex = value;
      const match = shape.exec(text);
      if (match) shapeEnd = Math.max(shapeEnd, match.index + match[0].length);
    }
    let first = -1, last = -1;
    while (valueEnd < length && (valueEnd < shapeEnd || !wordSpace(text[valueEnd]))) {
      const size = valueEnd >= word ? separatorAt(text, valueEnd) : 0;
      if (size && decide(valueEnd, size, begin, false) >= 0) { if (first < 0) first = valueEnd; last = valueEnd; }
      valueEnd++;
    }
    let ahead = valueEnd;
    while (ahead < length && (dashesSpace(text[ahead]) || encodedSpaceAhead(text, ahead))) ahead += encodedSpaceAhead(text, ahead) || 1;
    if (ahead > valueEnd && ahead < length && separatorAt(text, ahead) && decide(ahead, separatorAt(text, ahead), begin, false) >= 0) {
      if (first < 0) first = ahead;
      last = ahead;
    }
    for (const k of [...new Set([first, last])].filter(k => k !== -1).sort((a, b) => a - b)) valueEnd = Math.max(valueEnd, decide(k, separatorAt(text, k), begin));
    return consumedEnd(text, begin, valueEnd, decide);
  };
  while (i < length) {
    const c = text[i];
    let start = -1, stop = -1, placeholder = replacement, userinfo = false;
    const quoted = c === ":" && i > 0 ? jsonName(text, i) : undefined;
    if (quoted !== undefined) {
      const [pair, name] = quotedPair(quoted);
      if (pair) {
        let value = i + 1;
        while (value < length && javaSpace(text[value])) value++;
        if (skip.length && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
        const header = PAIR_LIST_HEADERS.has(normalizePrivacyName(name));
        let end = jsonValueEnd(text, value, header, quoted[1]);
        if (!header) end = consumedEnd(text, value, end, decide);
        if (end > value) { start = value; stop = end; placeholder = quoted[1] + replacement + quoted[1]; }
      }
      if (start < 0) { i++; continue; }
    } else if (c === ":" && text.startsWith("//", i + 1) && i !== dashesColon) {
      const limit = Math.min(length, i + 3 + 512);
      let end = i + 3;
      while (end < limit && !"/?# \t\r\n".includes(text[end])) end++;
      let at = rfind(text, "@", i + 3, end);
      if (at < i + 3) {
        const word = Math.min(wordLimit(i + 3), i + 1024);
        const later = lastAt(i + 3, word);
        const colon = later > i + 3 ? find(text, ":", i + 3, later) : -1;
        if (later > i + 3 && colon > i + 3) at = later;
      }
      if (at < i + 3) { i += 3; continue; }
      start = i + 3; stop = at; userinfo = true;
    } else if ((c === "=" || (c === "%" && ENCODED_EQUALS.has(text.slice(i + 1, i + 3)))) && quotedEquals(i)) {
      const size = c === "=" ? 1 : 3;
      let value = i + size;
      while (value < length && value - i - size < 8 && text[value] === " ") value++;
      if (skip.length && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
      const name = quotedPair(jsonName(text, i)!)[1];
      start = value;
      stop = equalsValueEnd(i, size, name);
      if (!PAIR_LIST_HEADERS.has(normalizePrivacyName(name))) stop = consumedEnd(text, value, stop, decide);
    } else if (c === "=" || c === ":" || (c === "%" && ENCODED_EQUALS.has(text.slice(i + 1, i + 3)))) {
      const size = c === "%" ? 3 : 1;
      const [pair, name, overflow] = separatorPair(text, i, copied, credential);
      if (pair) {
        let value = i + size;
        while (value < length && value - i - size < 8 && text[value] === " ") value++;
        if (skip.length && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
        start = value;
        if (!overflow && PAIR_LIST_HEADERS.has(normalizePrivacyName(name))) {
          stop = value;
          while (stop < length && text[stop] !== "\n" && text[stop] !== "\r") stop++;
        } else stop = consumedEnd(text, value, pairValueEnd(text, value), decide);
      }
    } else if (c === "-" && (i === 0 || pySpace(text[i - 1]) || encodedSpaceWidth(text, i, 0)) && flagDashes(text, i)) {
      const dashes = flagDashes(text, i);
      let end = i + dashes;
      while (end < length && PAIR_NAME_CHARS.has(text[end]) && end - i < NAME_CAP + dashes) end++;
      const overflow = end - i >= NAME_CAP + dashes && end < length && PAIR_NAME_CHARS.has(text[end]);
      while (overflow && end < length && PAIR_NAME_CHARS.has(text[end])) end++;
      if (overflow) pathBudget(text.slice(i, end));
      if (end < length && end - i - dashes >= (dashes === 2 ? 1 : 2)
        && (dashesSpace(text[end]) || LINE_BREAKS.has(text[end]) || encodedSpaceAhead(text, end))
        && (overflow || credential(text.slice(i, end)))) {
        let value = end, control = -1, gapEnd = end, spaced = false;
        while (value < length) {
          if (dashesSpace(text[value])) { value++; spaced = spaced || control >= 0; continue; }
          const width = encodedSpaceAhead(text, value);
          if (!width) break;
          if (control < 0 && encodedControl(text, value, width)) control = value;
          value += width;
          gapEnd = value;
        }
        if (dashesSeparator(text, value)) {
          dashesColon = text[value] === ":" ? value : -1;
          i = value;
          continue;
        }
        const begin = control >= 0 ? control : value;
        let valueEnd = -1, read = begin;
        if (value < length && LINE_BREAKS.has(text[value])) {
          let line = value + (text.startsWith("\r\n", value) ? 2 : 1), encoded = false;
          while (line < length) {
            if (dashesSpace(text[line])) { line++; continue; }
            if (encoded && LINE_BREAKS.has(text[line])) { line += text.startsWith("\r\n", line) ? 2 : 1; encoded = false; continue; }
            const width = encodedSpaceAhead(text, line);
            if (!width) break;
            if (control < 0 && encodedControl(text, line, width)) control = line;
            line += width; encoded = true;
          }
          if (line >= length || LINE_BREAKS.has(text[line]) || dashesSeparator(text, line)) {
            if (line >= length || LINE_BREAKS.has(text[line])) i = end;
            else { dashesColon = text[line] === ":" ? line : -1; i = line; }
            continue;
          }
          if (lineStartsPair(text, line, credential)) {
            if (control >= 0) { start = control; stop = gapEnd; }
            else { i = line; continue; }
          } else {
            read = control >= 0 ? control : line;
            valueEnd = wordEnd(read, line);
          }
        } else if (control >= 0 && !spaced && value < length) valueEnd = wordEnd(begin, value);
        else if (value >= length || flagAhead(text, value, credential)) {
          if (control >= 0) { start = control; stop = gapEnd; }
        } else valueEnd = wordEnd(begin, value);
        if (valueEnd > read) { start = read; stop = valueEnd; }
      }
      if (start < 0) { i = end; continue; }
    }
    if (start >= 0 && skip.length) {
      const spanEnd = skipped(start);
      if (spanEnd >= 0 && !(userinfo && stop > spanEnd)) { i = Math.max(spanEnd, i + 1); continue; }
    }
    if (start >= copied && stop >= start) {
      out.push(text.slice(copied, start), placeholder);
      copied = stop;
      i = Math.max(stop, i + 1);
      continue;
    }
    i++;
  }
  if (!out.length) return text;
  out.push(text.slice(copied));
  return out.join("");
}
/** An output text built from kept and replaced pieces, recording by position every span the catalog replaced. */
class Rewrite {
  parts: string[] = []; spans: Span[] = []; length = 0;
  keep(text: string): void { this.parts.push(text); this.length += text.length; }
  put(text: string, omission = false): void { this.spans.push({ start: this.length, end: this.length + text.length, omission }); this.keep(text); }
  /** Write a part of a URL: each run (sorted, disjoint; an empty run inserts the placeholder) becomes the placeholder, and each
   * data URI in the original text its omission. Where a run and a data URI overlap, the run's placeholder is written and the
   * data URI's omission covers the rest of it. */
  region(text: string, runs: readonly (readonly [number, number])[] = []): void {
    const data: [number, number, string][] = [];
    for (const [start, end] of scanText(DATA_SCAN, text, 0)) {
      const clean = dataURI(text.slice(start, end));
      if (clean !== undefined) data.push([start, end, clean]);
    }
    const length = text.length;
    let position = 0, r = 0, d = 0, last = "";
    for (;;) {
      while (r < runs.length && runs[r][1] <= position && !(runs[r][0] === runs[r][1] && runs[r][1] === position)) r++;
      if (r < runs.length && runs[r][0] === runs[r][1] && runs[r][1] === position) { this.put(replacement); last = "run" + r; r++; continue; }
      while (d < data.length && data[d][1] <= position) d++;
      if (position >= length) break;
      if (r < runs.length && runs[r][0] <= position) {
        if (last !== "run" + r) { this.put(replacement); last = "run" + r; }
        position = runs[r][1];
      } else if (d < data.length && data[d][0] <= position) {
        const end = r < runs.length ? Math.min(data[d][1], runs[r][0]) : data[d][1];
        if (last !== "data" + d) { this.put(data[d][2], true); last = "data" + d; }
        position = end;
      } else {
        let end = length;
        if (r < runs.length) end = Math.min(end, runs[r][0]);
        if (d < data.length) end = Math.min(end, data[d][0]);
        this.keep(text.slice(position, end));
        position = end; last = "";
      }
    }
  }
  result(): [string, Span[]] { return [this.parts.join(""), this.spans]; }
}
/** Replace each run (sorted, disjoint) with the placeholder. A span a run overlaps is dropped; the others move. */
function replaceRuns(value: string, runs: readonly (readonly [number, number])[], spans: Span[]): [string, Span[]] {
  if (!runs.length) return [value, spans];
  const out: string[] = [], moved: Span[] = [];
  let copied = 0, shift = 0, k = 0;
  for (const [start, end] of runs) {
    while (k < spans.length && spans[k].end <= start) { moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission }); k++; }
    while (k < spans.length && spans[k].start < end) k++;
    out.push(value.slice(copied, start), replacement);
    copied = end;
    shift += replacement.length - (end - start);
  }
  for (; k < spans.length; k++) moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission });
  out.push(value.slice(copied));
  return [out.join(""), moved];
}
/** The jwt value shape's matches, as replaceJwt reads them (linear). */
function jwtRuns(value: string): [number, number][] {
  const runs: [number, number][] = [];
  if (!value.includes("eyJ")) return runs;
  const n = value.length;
  for (let i = 0; i < n;) {
    if (!isRunCode(value.charCodeAt(i)) && value.charCodeAt(i) !== 46) { i++; continue; }
    const starts: number[] = [], ends: number[] = [];
    let j = i;
    for (let segStart = i; ; j++) {
      const c = j < n ? value.charCodeAt(j) : -1;
      if (c === 46 || !(c !== -1 && isRunCode(c))) { starts.push(segStart); ends.push(j); if (c !== 46) break; segStart = j + 1; }
    }
    for (let k = 0; k + 2 < starts.length;) {
      const a = starts[k], b = ends[k];
      let found = -1;
      for (let p = a; p + 3 <= b; p++) if (value.charCodeAt(p) === 101 && value.charCodeAt(p + 1) === 121 && value.charCodeAt(p + 2) === 74 && (p === a ? !wordBefore(value, a) : value.charCodeAt(p - 1) === 45)) { found = p; break; }
      if (found !== -1 && b - found - 3 >= 8 && ends[k + 1] - starts[k + 1] >= 8 && ends[k + 2] - starts[k + 2] >= 8) { runs.push([found, ends[k + 2]]); k += 3; } else k++;
    }
    i = j;
  }
  return runs;
}
/** Known secrets, then value shapes, over a whole URL value: the catalog's spans move with each replacement. */
function credentialRuns(value: string, spans: Span[], secrets: readonly string[]): [string, Span[]] {
  for (const secret of prepareSecrets(secrets)) {
    if (typeof value === "string" && typeof secret === "string" && value.indexOf(secret) < 0) continue;
    const runs: [number, number][] = [];
    let offset = 0;
    for (const part of value.split(replacement)) {
      for (let found = part.indexOf(secret); found >= 0; found = part.indexOf(secret, found + secret.length)) runs.push([offset + found, offset + found + secret.length]);
      offset += part.length + replacement.length;
    }
    [value, spans] = replaceRuns(value, runs, spans);
  }
  for (const shape of valueShapes) {
    const runs: [number, number][] = typeof shape === "function" ? jwtRuns(value)
      : [...value.matchAll(shape as RegExp)].map(match => [match.index!, match.index! + match[0].length] as [number, number]);
    [value, spans] = replaceRuns(value, runs.filter(([start, end]) => end > start), spans);
  }
  return [value, spans];
}
/** Whether a name after a '?' is a credential name; a name past NAME_CAP or one holding more than QUESTION_CAP '?' marks is
 * undecided (true). */
function afterQuestion(text: string, marks: readonly number[], end: number): boolean {
  if (!marks.length) return false;
  if (end - marks[0] - 1 > NAME_CAP || marks.length > QUESTION_CAP) return true;
  return marks.some(q => queryName(unquotePlus(text.slice(q + 1, end))));
}
function questionName(rawName: string): boolean {
  if (queryName(unquotePlus(rawName))) return true;
  const marks: number[] = [];
  for (let at = rawName.indexOf("?"); at >= 0 && marks.length <= QUESTION_CAP; at = rawName.indexOf("?", at + 1)) marks.push(at);
  return afterQuestion(rawName, marks, rawName.length);
}
/** Where a credential pair after a second '?' cuts the value (after its '='), or -1, in one forward pass. */
function secondQuestionCut(rawValue: string): number {
  let i = rawValue.indexOf("?");
  if (i < 0) return -1;
  let marks: number[] = [];
  for (const length = rawValue.length; i < length; i++) {
    const c = rawValue[i];
    if (c === "?") { if (marks.length <= QUESTION_CAP) marks.push(i); }
    else if (c === "=") {
      if (marks.length && afterQuestion(rawValue, marks, i)) return i + 1;
      marks = [];
    }
  }
  return -1;
}
/** The part of a URL after its '#': split at '&' like a query; a credential-named pair's value goes to the next '&'. */
function tailPairs(segment: string, out: Rewrite): void {
  const runs: [number, number][] = [];
  let offset = 0;
  for (const piece of segment.split("&")) {
    const [rawName, equals, rawValue] = partition(piece, "=");
    const value = offset + rawName.length + equals.length, end = offset + piece.length;
    if (equals && questionName(rawName)) runs.push([value, end]);
    else if (equals) { const cut = secondQuestionCut(rawValue); if (cut >= 0) runs.push([value + cut, end]); }
    offset = end + 1;
  }
  out.region(segment, runs);
}
/** The end of a path pair's value in base[start:end] (one segment): the next ';', ',' or '&' that a non-empty name and then
 * '=' or %3D follow, or the segment end. */
function pathValueEnd(base: string, start: number, end: number): number {
  let k = start;
  while (k < end) {
    if (!PATH_PAIR_SEPARATORS.has(base[k])) { k++; continue; }
    let j = k + 1;
    while (j < end && !PATH_PAIR_SEPARATORS.has(base[j]) && base[j] !== "=" && !(base[j] === "%" && ENCODED_EQUALS.has(base.slice(j + 1, j + 3)))) j++;
    if (j < end && j > k + 1 && !PATH_PAIR_SEPARATORS.has(base[j])) return k;
    k = j;
  }
  return end;
}
/** The URL path pair rule: every '=' and %3D in each path segment is read; a credential name's value is replaced to the next
 * pair in the segment or the segment end. The scheme and authority of an absolute URL are not path segments. */
function pathPairs(base: string, out: Rewrite): void {
  let path = 0;
  if (base.slice(0, 7).toLowerCase() === "http://" || base.slice(0, 8).toLowerCase() === "https://") {
    path = base.indexOf("/", base.indexOf("//") + 2);
    if (path < 0) path = base.length;
  }
  const runs: [number, number][] = [];
  for (let position = path; position < base.length;) {
    let end = base.indexOf("/", position + 1);
    if (end < 0) end = base.length;
    const first = base.startsWith("/", position) ? position + 1 : position;
    let k = first, name = first, decided = false;
    while (k < end) {
      const c = base[k];
      if (PATH_PAIR_SEPARATORS.has(c)) { k++; name = k; continue; }
      const size = c === "=" ? 1 : c === "%" && ENCODED_EQUALS.has(base.slice(k + 1, k + 3)) ? 3 : 0;
      if (!size) { k++; continue; }
      let credential = k > name && credentialName(unquote(base.slice(name, k)));
      if (!decided && !credential && k > first) credential = credentialName(unquote(base.slice(first, k)));
      decided = true;
      if (credential) {
        const valueEnd = pathValueEnd(base, k + size, end);
        runs.push([k + size, valueEnd]);
        k = name = valueEnd;
      } else k = name = k + size;
    }
    position = end;
  }
  out.region(base, runs);
}
const USERINFO = new RegExp(String.raw`^(https?://)[^${SPACE}/@]+:[^${SPACE}/@]+@`, "i");
/** Scrub one already-framed URL: the text and the spans the catalog itself replaced in it, by position. */
function framedUrl(original: string, incomplete = false): [string, Span[]] {
  const out = new Rewrite();
  const [target, hashmark, fragment] = partition(original, "#");
  let [base, question, rawQuery] = partition(target, "?");
  const userinfo = USERINFO.exec(base);
  if (userinfo) { out.keep(userinfo[1]); out.put(replacement); out.keep("@"); base = base.slice(userinfo[0].length); }
  pathPairs(base, out);
  out.keep(question);
  const parsed = rawQuery.split("&").map(pair => { const [name, equals, value] = partition(pair, "="); return { name, equals, value, decodedName: unquotePlus(name), decodedValue: unquotePlus(value) }; });
  // A truncated query may end in a syntactically valid MIME prefix. Without complete context no query MIME can attest to a non-media value.
  const hints = hintIndex(parsed.filter(pair => pair.equals).map(pair => [pair.decodedName, pair.decodedValue]), "", noHints, incomplete ? 0 : Infinity, true);
  parsed.forEach((pair, index) => {
    if (index) out.keep("&");
    out.keep(pair.name + pair.equals);
    if (!pair.equals) return;
    if (queryName(pair.decodedName) || questionName(pair.name)) { out.put(replacement); return; }
    const clean = mediaReplacement(pair.decodedValue, pair.decodedName, hints, true, false, false, incomplete);
    if (clean !== undefined) { out.put(clean, true); return; }
    const cut = secondQuestionCut(pair.value);
    if (cut >= 0) { out.keep(pair.value.slice(0, cut)); out.put(replacement); }
    else if (dataText(pair.decodedValue, true) !== pair.decodedValue) out.region(pair.decodedValue);
    else out.keep(pair.value);
  });
  out.keep(hashmark);
  if (hashmark) tailPairs(fragment, out);
  return out.result();
}
/** A whole URL value at a framed site: the catalog pass records its spans; known secrets and value shapes move them; the pair
 * rule then reads the URL text and skips only the spans still recorded. */
function wholeUrlText(value: string, secrets: readonly string[], incomplete: boolean): string {
  const [url, spans] = framedUrl(value, incomplete);
  const [clean, moved] = credentialRuns(url, spans, secrets);
  return credentialPairs(clean, moved);
}
/** A map key goes through the export rules of a string value at a framed site that need no context. */
function mapKey(name: string, secrets: readonly string[], incomplete = false): string {
  return mediaReplacement(name, "", noHints, true) ?? text(name, secrets, true, incomplete, true);
}
const privateKeyIndex = catalog.credentials.value_shapes.findIndex(item => item.id === "private-key-pem");
const privateKeyShape = privateKeyIndex < 0 ? undefined : valueShapes[privateKeyIndex];
function privateKeyText(value: string): string {
  return typeof privateKeyShape === "function" || privateKeyShape === undefined || !value.includes("-----BEGIN ") ? value : value.replace(privateKeyShape, replacement);
}
/** A structured string (a JSON object or array held in a string): the reference reads the text of the scrubbed leaves again with the
 * URL catalog, known secrets and value shapes, but not the pair rule, so a URL pair is read to the end of its value in the
 * re-dumped text as well. */
function structuredText(scrubbed: string, parsed: unknown, secrets: readonly string[], framed: boolean, incomplete: boolean): string {
  return typeof parsed === "object" && parsed !== null ? text(scrubbed, secrets, framed, incomplete, false, false) : scrubbed;
}
/** A prose gap after the whole-text pass (URL catalog, known secrets, value shapes, pair rule): only bare media between its
 * URL and data URI tokens is left to read; the URLs are never read a second time. */
function bareGaps(value: string): string {
  let end = 0, out = "";
  for (const [start, stop] of scanText(typeof value === "string" ? TEXT_SCAN : new RegExp(TEXT.source, TEXT.flags), value, 2)) {
    out += bareMedia(value.slice(end, start)) + value.slice(start, stop);
    end = stop;
  }
  return out + bareMedia(value.slice(end));
}
/** A text that is one complete JSON document: each of its leaves and keys goes through the pair rule on its own. */
function wholeJSON(value: string): boolean {
  if (!/^\s*[\[{"]/.test(value)) return false;
  try { JSON.parse(value); return true; } catch { return false; }
}
// ---- End of the shared privacy spec v4 section. ----
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
// These maps are created only from JSON.parse-validated source text. They never
// cross a public API or turn a telemetry field into a numeric-policy exemption.
type NumberOrigins = WeakMap<object, Map<string, string>>;
interface JsonNumbers { input: NumberOrigins; output: NumberOrigins; incompleteURLs: boolean; payloadRole?: boolean }
function quotedEnd(value: string, start: number): number {
  for (let i = start + 1; i < value.length; i++) {
    if (value[i] === "\\") i++;
    else if (value[i] === '"') return i + 1;
  }
  return -1;
}
/** JSON.parse remains the grammar authority. Source spans retain number lexemes
 * and expose discarded members; equality of materialized trees alone cannot
 * certify original source. No comparison ever coerces a registered secret. */
function jsonNumbers(value: string, parsed: unknown, secrets: readonly string[], depth: number, budget: PrivacyBudget) {
  charge(budget, depth, value.length);
  const ranges = knownSecretRanges(value, secrets, depth, budget);
  const input: NumberOrigins = new WeakMap(), output: NumberOrigins = new WeakMap();
  const number = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
  const tokens: (SourceRange & { string: boolean; key?: boolean })[] = [];
  const edits = new Map<number, { end: number; text: string }>();
  const numericScalars: { raw: string; path: string; hints: PrivacyHints; level: number }[] = [];
  let at = 0, root: string | undefined, duplicates = false, unsafeDuplicates = false;
  let sourceHints: PrivacyHints = noHints;
  const space = () => { while (at < value.length && /\s/.test(value[at])) at++; };
  const quoted = (key = false): string => {
    const start = at, end = quotedEnd(value, at);
    if (end < 0) throw new Error("Privacy JSON source");
    tokens.push({ start, end, string: true, key }); at = end;
    return JSON.parse(value.slice(start, end));
  };
  // Only numbers and already-hidden sentinels may be discarded while retaining
  // raw duplicate syntax. Every such number gets a separate policy audit below.
  const reusableScalar = (): boolean => {
    if (value[at] === "-" || /[0-9]/.test(value[at])) return true;
    if (value[at] !== '"') return false;
    const end = quotedEnd(value, at), scalar = JSON.parse(value.slice(at, end));
    return (scalar === replacement || scalar === "__REDACTED__" || omission.test(scalar)) && knownSecretText(scalar, secrets) === scalar;
  };
  const visit = (node: unknown, level: number, parent?: Map<string, string>, key = "", path = "", hints: PrivacyHints = noHints): void => {
    charge(budget, level); space(); parent?.delete(key);
    const c = value[at];
    if (c === "{" || c === "[") {
      const array = c === "[", close = array ? "]" : "}", slots = new Map<string, string>(), members = new Map<string, boolean>();
      if (node && typeof node === "object") {
        input.set(node, slots);
        if (!array) {
          const items = dataEntries(node);
          hints = hintIndex(items, path, hints, Infinity, true);
          sourceHints = hintIndex(items, path, sourceHints, Infinity, true);
        }
      }
      at++; space(); let index = 0;
      while (value[at] !== close) {
        let name: string;
        if (array) name = String(index++);
        else { name = quoted(true); space(); at++; space(); } // colon
        const reusable = reusableScalar();
        if (members.has(name)) { duplicates = true; unsafeDuplicates ||= !members.get(name) || !reusable; }
        members.set(name, reusable);
        visit(own(node, name), level + 1, slots, name, path ? `${path}.${name}` : name, hints); space();
        if (value[at] !== ",") break;
        at++; space();
      }
      at++; return;
    }
    if (c === '"') { quoted(); return; }
    if (c === "-" || /[0-9]/.test(c)) {
      number.lastIndex = at;
      const match = number.exec(value);
      if (!match) throw new Error("Privacy JSON source");
      const raw = match[0], clean = knownSecretText(raw, secrets), end = at + raw.length;
      if (parent) parent.set(key, raw); else root = raw;
      tokens.push({ start: at, end, string: false });
      numericScalars.push({ raw, path, hints, level });
      if (clean !== raw) edits.set(at, { end, text: JSON.stringify(clean) });
      at = end; return;
    }
    at += c === "f" ? 5 : 4; // validated false, true or null
  };
  visit(parsed, depth);
  let tokenIndex = 0, crossingSecret = false;
  for (const range of ranges) {
    while (tokenIndex < tokens.length && tokens[tokenIndex].end <= range.start) tokenIndex++;
    const token = tokens[tokenIndex];
    if (!token || range.start < token.start || range.end > token.end) { crossingSecret = true; break; }
    if (token.string) {
      const decoded: string = JSON.parse(value.slice(token.start, token.end));
      // The decoded walker owns ordinary matches, including repeatedly encoded
      // JSON numbers. Plain substitution here would invalidate that inner JSON.
      // Only a raw spelling absent from the decoded value needs a source edit.
      if (!decoded.includes(value.slice(range.start, range.end))) {
        // Rewriting a source key before walking would erase its credential/media
        // ancestry. Redact this owner instead of granting the renamed key trust.
        if (token.key) { crossingSecret = true; break; }
        edits.set(token.start, { end: token.end, text: JSON.stringify(replacement) });
      }
    }
  }
  let end = 0, patched = "";
  for (const [start, edit] of [...edits].sort((a, b) => a[0] - b[0])) {
    patched += value.slice(end, start) + edit.text; end = edit.end;
  }
  return { input, output, root, patched: patched + value.slice(end), duplicates, unsafeDuplicates, numericScalars, crossingSecret, sourceHints };
}
/** Serialize only materialized JSON data, preserving the original spelling of
 * surviving numbers even when another field changes. JSON.stringify alone can
 * round an unrelated integer into a registered credential on the next pass.
 */
function renderJSON(value: any, origins: NumberOrigins, budget: PrivacyBudget, depth: number, secrets: readonly string[] = [], numericLexeme?: string): string {
  charge(budget, depth);
  if (typeof value === "number" && numericLexeme !== undefined) {
    const clean = knownSecretText(numericLexeme, secrets);
    return clean === numericLexeme ? numericLexeme : JSON.stringify(clean);
  }
  if (!value || typeof value !== "object") return JSON.stringify(value) ?? "null";
  const slots = origins.get(value);
  if (Array.isArray(value)) return "[" + value.map((item, i) => renderJSON(item, origins, budget, depth + 1, secrets, slots?.get(String(i)))).join(",") + "]";
  return "{" + Object.keys(value).map(key => JSON.stringify(key) + ":" + renderJSON(value[key], origins, budget, depth + 1, secrets, slots?.get(key))).join(",") + "}";
}
function scrubJSON(value: string, parsed: unknown, secrets: readonly string[], depth: number, budget: PrivacyBudget, inheritedAudio = false, inheritedMedia = false, incomplete = false, incompleteURLs = false, payloadRole = false): string {
  const source = jsonNumbers(value, parsed, secrets, depth, budget);
  // A registered match spanning syntax/scalars belongs to this entire JSON
  // owner. A JSON-encoded marker keeps the surrounding encoded layers valid.
  if (source.crossingSecret) return JSON.stringify(replacement);
  let reuse = !source.unsafeDuplicates;
  const origin = { ...source, incompleteURLs, payloadRole };
  if (reuse && source.duplicates) {
    // Non-numeric discarded members are forbidden above. The remaining source
    // strings/containers survive in the parsed tree, except fixed sentinels.
    // Check EVERY original numeric occurrence, including overwritten values,
    // with its full path/hints before certifying the source for reuse.
    for (const scalar of source.numericScalars) {
      const expected = knownSecretText(scalar.raw, secrets);
      const clean = walkValue(JSON.parse(scalar.raw), secrets, scalar.path, scalar.level, inheritedAudio, budget, scalar.hints, true, inheritedMedia, incomplete, true, undefined, false, origin, scalar.raw);
      if (typeof clean === "number" ? expected !== scalar.raw : clean !== expected) reuse = false;
    }
  }
  let numbers = origin;
  if (source.patched !== value) {
    value = source.patched; parsed = JSON.parse(value);
    // Source edits retain all unrelated numeric spellings; rebuild their private
    // ownership map against the new tree, never via JSON.stringify/Number aliases.
    numbers = { ...jsonNumbers(value, parsed, [], depth, budget), incompleteURLs, payloadRole };
  }
  // Source-masked MIME/type values must not erase the original media evidence.
  const clean = walkValue(parsed, secrets, "", depth, inheritedAudio, budget, source.sourceHints, true, inheritedMedia, incomplete, true, undefined, false, numbers, numbers.root);
  const rendered = renderJSON(clean, numbers.output, budget, depth, [], numbers.root);
  const numericOnly = renderJSON(parsed, numbers.input, budget, depth, secrets, numbers.root);
  return reuse && rendered === numericOnly ? value : rendered;
}
/** Walk only plain, already-materialized data. The Node S1 boundary rejects proxies before calling. */
export function scrubValue(value: any, secrets: readonly string[] = [], path = "", depth = 0, inheritedAudio = false, budget: PrivacyBudget = newBudget(), hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, incomplete = false, ambiguousBase64 = true): any {
  return pathBudgeted(() => walkValue(value, secrets, path, depth, inheritedAudio, budget, hints, framed, inheritedMedia, incomplete, ambiguousBase64));
}
/** A name (a path, a map key, a pair name in text) of more than max_depth components exports the whole value as the placeholder. */
function pathBudgeted<T>(scrub: () => T): T | string {
  try { return scrub(); } catch (error) { if (error instanceof PrivacyPathBudget) return replacement; throw error; }
}
function walkValue(value: any, secrets: readonly string[], path: string, depth: number, inheritedAudio: boolean, budget: PrivacyBudget, hints: PrivacyHints, framed: boolean, inheritedMedia: boolean, incomplete: boolean, ambiguousBase64: boolean, policy?: ResponsePolicy, exemptToken = false, numbers?: JsonNumbers, numericLexeme?: string): any {
  charge(budget, depth);
  // A path of more than limits.max_depth components exports the whole value as the placeholder.
  pathBudget(path);
  if (credentialName(path) && !exemptToken) return replacement;
  if (value && typeof value === "object") {
    if (budget.seen.has(value)) return "<omitted>";
    budget.seen.add(value);
    if (!Array.isArray(value)) {
      if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return typeof value;
      const items = dataEntries(value);
      if (items.length > budget.nodes) throw new Error("Privacy structure budget");
      budget.text -= items.reduce((n, [name]) => n + name.length, 0); if (budget.text < 0) throw new Error("Privacy text budget");
      hints = hintIndex(items, path, hints, Infinity, true);
      const token = policy?.tokenParents.has(value) ? own(value, "token") : undefined;
      const cleanToken = typeof token === "string"
        ? walkValue(token, secrets, "", depth + 1, false, budget, noHints, true, false, incomplete, ambiguousBase64) : undefined;
      const maskedBytes = typeof token === "string" && (cleanToken !== token || policy!.textPolicy(cleanToken) !== token);
      const numericOutput = new Map<string, string>();
      const entries = new Map<string, unknown>();
      for (const [name, item] of items) {
        // Token bytes are another encoding of the same value, not harmless metadata.
        const masked = name === "logprobs" && policy?.maskedChoices.has(value) || policy?.maskedText.get(value)?.has(name) || name === "bytes" && maskedBytes;
        const exempt = name === "token" && typeof item === "string" && !!policy?.tokenParents.has(value);
        const lexeme = numbers?.input.get(value)?.get(name);
        const clean = masked ? replacement : walkValue(item, secrets, path ? `${path}.${name}` : name, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, exempt, numbers, lexeme);
        // A key goes through the export text rules of a framed string value.
        const cleanName = mapKey(name, secrets, numbers?.incompleteURLs);
        // Two keys that are one after scrubbing: one key is kept, in the first one's place, and its value is the placeholder.
        if (entries.has(cleanName)) { entries.set(cleanName, replacement); numericOutput.delete(cleanName); continue; }
        if (typeof clean === "number" && lexeme !== undefined) numericOutput.set(cleanName, lexeme);
        entries.set(cleanName, clean);
      }
      const result = Object.fromEntries(entries);
      numbers?.output.set(result, numericOutput);
      return result;
    }
    const length = Object.getOwnPropertyDescriptor(value, "length")!.value;
    if (length > budget.nodes) throw new Error("Privacy structure budget");
    const items = Array.from({ length }, (_, i) => { const d = Object.getOwnPropertyDescriptor(value, String(i)); return d && "value" in d ? d.value : undefined; });
    const clean = replaceMedia(items, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, true, numbers?.payloadRole);
    if (clean !== undefined) return clean;
    const numericOutput = new Map<string, string>();
    const result = items.map((item, i) => {
      const lexeme = numbers?.input.get(value)?.get(String(i));
      const clean = walkValue(item, secrets, `${path}.${i}`, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, false, numbers, lexeme);
      if (typeof clean === "number" && lexeme !== undefined) numericOutput.set(String(i), lexeme);
      return clean;
    });
    numbers?.output.set(result, numericOutput);
    return result;
  }
  if (typeof value === "string") {
    budget.text -= value.length; if (budget.text < 0) throw new Error("Privacy text budget");
    if (wholeSecret(value, secrets)) return replacement;
    const clean = replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64, numbers?.payloadRole); if (clean !== undefined) return clean;
    // JSON tool arguments are independently framed. Share budgets across nested encodings.
    if (/^\s*[\[{"]/.test(value)) {
      let parsed: unknown; let valid = false;
      try { parsed = JSON.parse(value); valid = true; } catch { /* Prose is handled below. */ }
      if (valid) {
        const ctx = context(path, hints, inheritedMedia, inheritedAudio);
        // Source-number origins and budgets remain private to this JSON owner.
        // Never reprocess complete JSON as prose or match a rounded numeric alias.
        const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;
        return structuredText(scrubJSON(value, parsed, secrets, depth + 1, budget, ctx.audio, ctx.media, incomplete, numbers?.incompleteURLs, payloadRole), parsed, secrets, framed, incomplete);
      }
    }
    const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;
    return scrubEmbeddedText(value, secrets, depth + 1, budget, framed, incomplete, numbers?.incompleteURLs, payloadRole);
  }
  const clean = replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64, numbers?.payloadRole);
  if (clean !== undefined) {
    // Restored numeric media classification must not preempt the existing
    // source-aware secret repair, including original JSON numeric lexemes.
    if (typeof value === "number") {
      const raw = numericLexeme ?? String(value), masked = knownSecretText(raw, secrets);
      if (masked !== raw) return masked;
    }
    return clean;
  }
  if (typeof value === "number") {
    const raw = numericLexeme ?? String(value), clean = knownSecretText(raw, secrets);
    if (clean !== raw) return clean;
    // Valid JSON may contain overflowing or rounded numbers. Its source owner
    // re-emits the original token; native numbers have only canonical spelling.
    return numericLexeme !== undefined || Number.isFinite(value) ? value : String(value);
  }
  return value == null || typeof value === "boolean" ? value : typeof value;
}
/**
 * Next `name["']?\s*[:=]\s*` at or after `from`, name = [A-Za-z_][A-Za-z0-9_.-]* starting at a
 * word boundary. A single regex retried the whole key run from every '-' or '.' inside it
 * (quadratic on "sk-sk-sk-..."); every start in one run has the same end, so each run is tried once.
 */
const KEY_RUN = /(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+/g;
const ASSIGNMENT_TAIL = /["']?\s*[:=]\s*/y;
function nextAssignment(value: string, from: number): { index: number; name: string; lastIndex: number } | undefined {
  KEY_RUN.lastIndex = from;
  for (let run: RegExpExecArray | null; (run = KEY_RUN.exec(value));) {
    const text = run[0], runEnd = run.index + text.length;
    ASSIGNMENT_TAIL.lastIndex = runEnd;
    const tail = ASSIGNMENT_TAIL.exec(value);
    if (!tail) continue;
    // First \b start of [A-Za-z_]: run start, or right after a '.' / '-' (non-word) separator.
    for (let i = 0; i < text.length; i++) {
      if (/[A-Za-z_]/.test(text[i]) && (i === 0 || text[i - 1] === "." || text[i - 1] === "-"))
        return { index: run.index + i, name: text.slice(i), lastIndex: runEnd + tail[0].length };
    }
  }
  return undefined;
}
/** Text assignments use the same catalog predicate as structured fields. */
function scrubAssignments(value: string, owners: readonly TextOwner[] = []): string {
  // URL tokens own their query and fragment delimiters. The URL sanitizer below
  // already masks catalog credentials (including encoded names); applying the
  // prose assignment grammar inside them consumes safe &pairs and adds quotes.
  // Standalone #fragments follow the same delimiter rule. Only this prose pass
  // skips them: known secrets, value shapes and embedded media still get scrubbed.
  // Do not exempt an assignment whose VALUE is a URL: its key remains private.
  const urls = [...value.matchAll(new RegExp(ASSIGNMENT_URL_TOKEN, "gi"))].map(match => [match.index!, match.index! + match[0].length] as const);
  // Complete JSON/string owners apply the structural policy themselves. A prose
  // assignment beginning BEFORE an owner still owns its whole private value.
  urls.push(...owners.map(owner => [owner.start, owner.end] as const));
  urls.sort((a, b) => a[0] - b[0]);
  let result = "", offset = 0, urlIndex = 0;
  // Diagnostic paragraphs repeat names (e.g. 'Note:') many thousands of times.
  // Bound a per-string name-only memo; no credential values or cross-call state.
  const names = new Map<string, boolean>();
  for (let match: ReturnType<typeof nextAssignment>, next = 0; (match = nextAssignment(value, next));) {
    next = match.lastIndex;
    while (urlIndex < urls.length && urls[urlIndex][1] <= match.index) urlIndex++;
    if (urlIndex < urls.length && urls[urlIndex][0] <= match.index) continue;
    const name = match.name;
    let credential = names.get(name);
    if (credential === undefined) {
      credential = isCredentialName(name);
      if (names.size < 256 && name.length <= 256) names.set(name, credential);
    }
    if (!credential) continue;
    const start = match.lastIndex;
    let end = start;
    const quote = value[start];
    if (quote === '"' || quote === "'") {
      end++;
      let escaped = false;
      for (; end < value.length; end++) {
        if (escaped) escaped = false;
        else if (value[end] === "\\") escaped = true;
        else if (value[end] === quote) { end++; break; }
      }
    } else if (/(?:authorization|cookie)$/.test(normalizePrivacyName(name)) || /^[\[{]/.test(value.slice(start, start + 1))) {
      // Signed Authorization and Cookie headers have multiple fields; a scalar
      // matcher would leak later signatures, nonces or cookies on the same line.
      while (end < value.length && value[end] !== "\r" && value[end] !== "\n") end++;
    } else {
      const scheme = /^(?:Bearer|Basic)\s+/i.exec(value.slice(start));
      if (scheme) end += scheme[0].length;
      while (end < value.length && !/[\s,;}"']/.test(value[end])) end++;
    }
    if (end === start) continue;
    result += value.slice(offset, start) + JSON.stringify(replacement);
    offset = end;
    next = end;
  }
  return result + value.slice(offset);
}

interface TextOwner {
  start: number; end: number; kind: "json" | "wrapper" | "tail" | "field";
  parsed?: unknown; key?: string; valueStart?: number; keyEnd?: number;
}
/** Find owners on ORIGINAL input, before replacements move delimiters. Invalid
 * bracketed prose is handled recursively, while incomplete JSON stays closed.
 */
function textOwners(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {
  charge(budget, depth, value.length);
  const owners: TextOwner[] = [];
  const urls: RegExpMatchArray[] = []; let urlIndex = 0;
  TEXT.lastIndex = 0;
  for (const match of value.matchAll(typeof value === "string" ? TEXT : new RegExp(TEXT.source, TEXT.flags))) { charge(budget, depth); urls.push(match); }
  for (let i = 0; i < value.length; i++) {
    while (urlIndex < urls.length && urls[urlIndex].index! + urls[urlIndex][0].length <= i) urlIndex++;
    if (urlIndex < urls.length && urls[urlIndex].index! <= i) { i = urls[urlIndex].index! + urls[urlIndex][0].length - 1; continue; }
    const c = value[i];
    if (c === '"') {
      const end = quotedEnd(value, i); if (end < 0) continue;
      charge(budget, depth);
      let parsed: unknown;
      try { parsed = JSON.parse(value.slice(i, end)); } catch { i = end - 1; continue; }
      // A key/value fragment outside a complete object still needs the named
      // media/credential policy; a property name alone grants no value exemption.
      const colon = /^\s*:\s*/.exec(value.slice(end));
      if (colon) {
        const valueStart = end + colon[0].length;
        const stop = value[valueStart] === '"' ? quotedEnd(value, valueStart) : -1;
        if (stop >= 0) {
          let field: unknown; let valid = false;
          try { field = JSON.parse(value.slice(valueStart, stop)); valid = true; } catch { /* prose */ }
          if (valid) { owners.push({ start: i, end: stop, kind: "field", key: parsed as string, parsed: field, valueStart, keyEnd: end }); i = stop - 1; continue; }
        }
        i = end - 1; continue;
      }
      owners.push({ start: i, end, kind: "json", parsed }); i = end - 1; continue;
    }
    if (c !== "{" && c !== "[") continue;
    charge(budget, depth);
    let nesting = 1, end = i + 1;
    for (; end < value.length; end++) {
      const next = value[end];
      if (next === '"') { const stop = quotedEnd(value, end); if (stop < 0) { end = value.length; break; } end = stop - 1; }
      else if (next === "{" || next === "[") { if (++nesting + depth > catalog.limits.max_depth) throw new Error("Privacy structure budget"); }
      else if ((next === "}" || next === "]") && --nesting === 0) { end++; break; }
    }
    if (nesting !== 0) { owners.push({ start: i, end: value.length, kind: "tail" }); break; }
    let parsed: unknown; let valid = false;
    try { parsed = JSON.parse(value.slice(i, end)); valid = true; } catch { /* Prose wrapper around real JSON. */ }
    owners.push({ start: i, end, kind: valid ? "json" : "wrapper", parsed }); i = end - 1;
  }
  return owners;
}
/** Reconcile original credential ranges with owners before any replacement
 * moves delimiters. JSON-contained matches stay with that owner; crossing
 * matches consume the whole intersected owner plus overlapping prose bytes. */
function redactCrossOwnerSecrets(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget): string {
  if (!ranges.length) return value;
  // Keep this second scan: native RegExp legacy statics are observable from
  // budget accessors, even though the scan itself has no charge calls.
  TEXT.lastIndex = 0;
  const urls = [...value.matchAll(typeof value === "string" ? TEXT : new RegExp(TEXT.source, TEXT.flags))].map(match => ({ start: match.index!, end: match.index! + match[0].length }));
  const edits: SourceRange[] = [];
  let ownerIndex = 0, urlIndex = 0;
  for (const range of ranges) {
    charge(budget, depth);
    while (ownerIndex < owners.length && owners[ownerIndex].end <= range.start) ownerIndex++;
    const enclosing = owners[ownerIndex];
    if (enclosing && enclosing.start <= range.start && enclosing.end >= range.end
      && (enclosing.kind === "json" || enclosing.kind === "field" && range.start >= enclosing.valueStart!
        || enclosing.kind === "wrapper" && enclosing.start < range.start && range.end < enclosing.end
        || enclosing.kind === "tail" && enclosing.start < range.start)) continue;
    let { start, end } = range;
    for (let i = ownerIndex; i < owners.length && owners[i].start < end; i++) {
      charge(budget, depth);
      start = Math.min(start, owners[i].start); end = Math.max(end, owners[i].end);
    }
    while (urlIndex < urls.length && urls[urlIndex].end <= start) urlIndex++;
    for (let i = urlIndex; i < urls.length && urls[i].start < end; i++) {
      charge(budget, depth);
      // A URL-contained credential keeps ordinary query structure. If the
      // match crosses its boundary, consume the URL rather than strand syntax.
      if (start < urls[i].start || end > urls[i].end) { start = Math.min(start, urls[i].start); end = Math.max(end, urls[i].end); }
    }
    const prior = edits.at(-1);
    if (prior && start < prior.end) prior.end = Math.max(prior.end, end);
    else edits.push({ start, end });
  }
  let end = 0, out = "";
  for (const edit of edits) { out += value.slice(end, edit.start) + replacement; end = edit.end; }
  return out + value.slice(end);
}
/** S1's additional malformed/embedded JSON policy; recognition still delegates to S6. */
function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false, nested = false): string {
  if (depth > catalog.limits.max_depth) throw new Error("Privacy structure budget");
  if (wholeSecret(value, secrets)) return replacement;
  const ranges = knownSecretRanges(value, secrets, depth, budget);
  // A whole data URI or URL owns its delimiters, including brackets and quotes.
  if (framed) { const uri = mediaReplacement(value); if (uri !== undefined) return uri; if (isURLValue(value)) return text(value, secrets, true, incomplete || incompleteURLs); }
  // Owners are found on the text they frame; a pass that rewrites the text finds them again (once, below).
  let owners: TextOwner[] | undefined;
  if (ranges.length) {
    owners = textOwners(value, depth, budget);
    const matched = redactCrossOwnerSecrets(value, ranges, owners, depth, budget);
    // A nested owner's text was read by the whole-text pass with its known secrets in place, so a value shape beside one was cut
    // off by a word character; the reference replaces known secrets first, so read the shapes again on the replaced text.
    if (matched !== value) { value = nested ? credentialText(matched, []) : matched; owners = undefined; }
  }
  let whole = false;
  if (!nested && !(whole = wholeJSON(value))) {
    // The credential pair rule reads the whole text once, after the URL catalog, known secrets and value shapes, before any
    // owner below rewrites a name or a value it must read. Known secrets outside every owner were replaced just above; the
    // owners replace their own (a JSON owner keeps its encoding), so this pass reads none.
    const paired = text(value, [], false, incomplete || incompleteURLs, true, false);
    if (paired !== value) { value = paired; owners = undefined; }
  }
  owners ??= textOwners(value, depth, budget);
  let end = 0, out = "";
  for (const owner of owners) {
    const raw = value.slice(owner.start, owner.end);
    out += bareGaps(value.slice(end, owner.start));
    if (owner.kind === "json") {
      out += wholeSecret(raw, secrets) ? JSON.stringify(replacement) : scrubJSON(raw, owner.parsed, secrets, depth + 1, budget, false, false, incomplete, incompleteURLs, payloadRole);
    } else if (owner.kind === "field") {
      const key = owner.key!, cleanKey = text(key, secrets, false, incompleteURLs);
      const urlContext: JsonNumbers | undefined = incompleteURLs || payloadRole ? { input: new WeakMap(), output: new WeakMap(), incompleteURLs, payloadRole } : undefined;
      const encoded = scrubJSON(value.slice(owner.valueStart, owner.end), owner.parsed, secrets, depth + 1, budget, false, false, incomplete, incompleteURLs, payloadRole);
      const clean = walkValue(JSON.parse(encoded), secrets, key, depth + 1, false, budget, noHints, true, false, incomplete, true, undefined, false, urlContext);
      out += (cleanKey === key ? value.slice(owner.start, owner.keyEnd) : JSON.stringify(cleanKey))
        + value.slice(owner.keyEnd, owner.valueStart)
        + (clean === owner.parsed ? value.slice(owner.valueStart, owner.end) : JSON.stringify(clean));
    } else if (owner.kind === "wrapper") {
      out += raw[0] + scrubEmbeddedText(raw.slice(1, -1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole, true) + raw.at(-1);
    } else {
      // A truncated JSON object cannot safely retain structural media context.
      const fields: [string, unknown][] = [];
      for (const match of raw.matchAll(/("(?:\\.|[^"\\])*"\s*:\s*)("(?:\\.|[^"\\])*")/g)) {
        try { fields.push([JSON.parse(match[1].replace(/\s*:\s*$/, "")), JSON.parse(match[2])]); } catch { /* malformed */ }
      }
      const hints = indexPrivacyHints(fields);
      const mediaFragment = [...hints.values()].some(h => h[0] || h[1]) || [...raw.matchAll(/"([^"\\]+)"\s*:/g)].some(m => incompleteMediaPath(m[1]));
      const propertyOnly = /^(\{\s*)("(?:\\.|[^"\\])*")(\s*:\s*)$/.exec(raw);
      const jsonTail = !propertyOnly && /^(?:\{\s*"|\[\s*(?:[\[{"\d-]|true\b|false\b|null\b))/.test(raw);
      if (incomplete || !propertyOnly && raw[0] === "{" && mediaFragment || jsonTail) out += "<omitted: incomplete audio>";
      else if (propertyOnly) {
        let key: unknown;
        try { key = JSON.parse(propertyOnly[2]); } catch { out += text(raw, secrets, false, incomplete); end = owner.end; continue; }
        const clean = scrubValue(key, secrets, "", depth + 1, false, budget, noHints, true, false, incomplete);
        out += clean === key ? raw : propertyOnly[1] + JSON.stringify(clean) + propertyOnly[3];
      } else out += raw[0] + scrubEmbeddedText(raw.slice(1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole, true);
    }
    end = owner.end;
  }
  // Only unowned gaps use the prose policy. Repaired JSON/quoted layers must
  // never return through URL or bare-media scanning as one blob.
  out += bareGaps(value.slice(end));
  return whole && /^\s*[\[{]/.test(value) ? structuredText(out, {}, secrets, framed, incomplete || incompleteURLs) : out;
}
/** Free-text entry point: unlike scalar values, data: prefixes do not own trailing prose. */
export function scrubText(value: string, secrets: readonly string[] = [], depth = 0): string {
  const budget = newBudget(); charge(budget, depth, value.length);
  return pathBudgeted(() => scrubEmbeddedText(value, secrets, depth, budget, false));
}
export function scrubAttribute(value: string, secrets: readonly string[] = [], incompleteURLs = false): string {
  return pathBudgeted(() => attributeText(value, secrets, incompleteURLs));
}
function attributeText(value: string, secrets: readonly string[], incompleteURLs: boolean): string {
  if (value.length > catalog.limits.max_text_chars) throw new Error("Privacy text budget");
  if (wholeSecret(value, secrets)) return replacement;
  // Preserve S1's JSON-scalar handling as well as S6's object/array walker. SDKs
  // can serialize a primitive media value as a quoted JSON output.value.
  let parsed: unknown, valid = false;
  // JSON allows only SP/TAB/CR/LF before these initial characters. Everything
  // else would throw into the same walker below. Possible JSON still uses the
  // native parser, including scalar roots and malformed-but-plausible prefixes.
  // Non-string runtime callers retain JSON.parse's original coercion behavior.
  if (typeof value !== "string" || /^[ \t\r\n]*[{\["0-9tfn-]/.test(value)) {
    try { parsed = JSON.parse(value); valid = true; } catch { /* Prose is handled below. */ }
  }
  if (!valid) {
    // A mapped copy may have lost URL MIME context, not all scalar context.
    // Carry that fact through owners even in diagnostics with JSON islands;
    // a whole-input prose prepass could corrupt their quoted/nested framing.
    const urlContext: JsonNumbers | undefined = incompleteURLs ? { input: new WeakMap(), output: new WeakMap(), incompleteURLs } : undefined;
    return walkValue(value, secrets, "", 0, false, newBudget(), noHints, true, false, false, true, undefined, false, urlContext);
  }
  // The public numeric-root result is plain text, not a quoted JSON marker.
  // Keep the original spelling/whitespace and never match String(parsed).
  if (typeof parsed === "number") return knownSecretText(value, secrets);
  return structuredText(scrubJSON(value, parsed, secrets, 0, newBudget(), false, false, false, incompleteURLs), parsed, secrets, true, false);
}

/** S5 harvesting over already-parsed data, sharing exactly the export predicate.
 * No body reads, user conversions or per-trace credential groups. Exhaustion throws.
 */
export function discoverSecrets(sources: Iterable<unknown>, entries = dataEntries, inspect?: (characters: number) => void): string[] {
  return discover(sources, entries, undefined, newBudget(), inspect);
}
function discover(sources: Iterable<unknown>, entries = dataEntries, tokenParents?: WeakSet<object>, budget: PrivacyBudget = newBudget(), inspect?: (characters: number) => void): string[] {
  const found = new Set<string>(), seen = new Set<object>();
  const add = (value: string) => { if (value.length < 8 || value === replacement) return; if (found.size >= 1024 && !found.has(value)) throw new Error("Privacy credential budget"); found.add(value); };
  const urls = (value: string) => {
    const visit = (url: string) => {
      inspect?.(url.length);
      for (const pair of queryParts(url).pairs) if (pair.equals && isCredentialName(pair.decodedName, true)) { add(pair.value); add(pair.decodedValue); }
    };
    if (isURLValue(value, inspect)) visit(value);
    else {
      inspect?.(value.length);
      DISCOVERY_URLS.lastIndex = 0;
      for (const match of value.matchAll(DISCOVERY_URLS)) {
        const closing = match.index! > 0 && value[match.index! - 1] === "'" && match[0].endsWith("'");
        visit(closing ? match[0].slice(0, -1) : match[0]);
      }
    }
  };
  const jsonContainer = (value: string): boolean => {
    // Probe only characters actually examined, including arbitrarily long
    // whitespace prefixes. Reuse the result instead of probing the input twice.
    for (let i = 0; i < value.length; i++) {
      inspect?.(1);
      const char = value[i];
      if (!/\s/.test(char)) return char === "[" || char === "{";
    }
    return false;
  };
  const visit = (value: unknown, path = "", depth = 0, query = false, exemptToken = false) => {
    const json = typeof value === "string" && jsonContainer(value);
    if (typeof value === "string" && json && value.length > MAX_DISCOVERY_BODY_BYTES) throw new Error("Privacy discovery body budget");
    charge(budget, depth, typeof value === "string" ? value.length : 0);
    const credential = isCredentialName(path, query) && !exemptToken;
    if (typeof value === "string") {
      if (credential) {
        add(value);
        if (leaf(path).endsWith("authorization")) { inspect?.(value.length); add(value.replace(/^\S+\s+/, "")); }
      } else {
        urls(value);
        if (json) {
          inspect?.(value.length);
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
        visit(item, path ? `${path}.${name}` : name, depth + 1, query || /^(?:query|defaultQuery|searchParams)$/.test(path), name === "token" && typeof item === "string" && !!tokenParents?.has(value));
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

/** Explicit provider-root context. Never inferred from an attribute name or JSON shape. */
export type LogprobFormat = "openai.chat";
interface ResponsePolicy {
  tokenParents: WeakSet<object>;
  maskedChoices: WeakSet<object>;
  maskedText: WeakMap<object, Set<string>>;
  textPolicy: (value: string) => unknown;
}
const own = (value: unknown, name: string): any => {
  if (!value || typeof value !== "object") return undefined;
  const descriptor = Object.getOwnPropertyDescriptor(value, name);
  return descriptor && "value" in descriptor ? descriptor.value : undefined;
};
function responseRoots(value: unknown, budget: PrivacyBudget): unknown[] {
  if (!Array.isArray(value)) return [value];
  const length = own(value, "length");
  if (length > budget.nodes) throw new Error("Privacy response budget");
  return Array.from({ length }, (_, i) => own(value, String(i)));
}
function responseTokens(value: unknown, format: LogprobFormat, budget: PrivacyBudget): WeakSet<object> {
  const parents = new WeakSet<object>();
  const rules = catalog.logprob_tokens.formats.filter(rule => rule.format === format);
  const visit = (node: unknown, path: readonly string[], depth: number): void => {
    charge(budget, depth);
    if (!node || typeof node !== "object" || !path.length) return;
    const [segment, ...rest] = path;
    if (segment === "*") {
      if (!Array.isArray(node)) return;
      const length = own(node, "length");
      if (length > budget.nodes) throw new Error("Privacy response budget");
      for (let i = 0; i < length; i++) visit(own(node, String(i)), rest, depth + 1);
    } else if (path.length === 1) {
      if (segment === "token" && typeof own(node, segment) === "string") parents.add(node);
    } else visit(own(node, segment), rest, depth + 1);
  };
  for (const root of responseRoots(value, budget)) for (const rule of rules) for (const path of rule.paths) visit(root, path, 0);
  return parents;
}
/** Parsed provider data only: no arbitrary nested JSON may acquire this context.
 * Streaming callers supply the complete buffered chunk array, once per response.
 * All passes share a budget; a budget failure belongs to the caller's fail-closed boundary.
 */
export function scrubProviderResponse(value: unknown, secrets: readonly string[] = [], format: LogprobFormat = "openai.chat", textPolicy: (value: string) => unknown = value => value, incomplete = false): any {
  return pathBudgeted(() => providerResponse(value, secrets, format, textPolicy, incomplete));
}
function providerResponse(value: unknown, secrets: readonly string[], format: LogprobFormat, textPolicy: (value: string) => unknown, incomplete: boolean): any {
  const budget = newBudget();
  const policy: ResponsePolicy = { tokenParents: incomplete ? new WeakSet<object>() : responseTokens(value, format, budget), maskedChoices: new WeakSet(), maskedText: new WeakMap(), textPolicy };
  const rule = catalog.logprob_tokens.formats.find(rule => rule.format === format);
  if (!rule) return scrubValue(value, secrets);
  const streaming = Array.isArray(value);
  type Field = { parent: object; name: string; value: unknown };
  type Choice = { choice: object; fields: Field[] };
  const slots = new Map<number, { choices: Choice[]; text: Map<string, string>; masked: boolean }>();
  const changed = (textValue: string): boolean => {
    // A prior admission may already have masked this choice's text. The image cap
    // (trace-config/imageCap.ts) emits exactly two markers: REDACTED_VALUE
    // "__REDACTED__" and its whole-value/ambiguous-token WHOLE_MARKER
    // "[image-redacted]". This leaf stays dependency-free, so both are literals.
    if (textValue.includes(replacement) || textValue.includes("__REDACTED__") || textValue.includes("[image-redacted]")) return true;
    const clean = walkValue(textValue, secrets, "", 0, false, budget, noHints, true, false, incomplete, true);
    return clean !== textValue || textPolicy(clean) !== textValue;
  };
  for (const root of responseRoots(value, budget)) {
    const choices = own(root, "choices");
    if (!Array.isArray(choices)) continue;
    const length = own(choices, "length");
    if (length > budget.nodes) throw new Error("Privacy response budget");
    for (let i = 0; i < length; i++) {
      charge(budget, 1);
      const choice = own(choices, String(i));
      if (!choice || typeof choice !== "object") continue;
      const rawIndex = own(choice, "index"), validIndex = Number.isSafeInteger(rawIndex) && rawIndex >= 0;
      // Missing stream indexes cannot be joined to another delta safely.
      if (streaming && !validIndex) { policy.maskedChoices.add(choice); continue; }
      const index = streaming ? rawIndex : i;
      let slot = slots.get(index);
      if (!slot) { slot = { choices: [], text: new Map(), masked: false }; slots.set(index, slot); }
      const fields: Field[] = [];
      for (const path of rule.text_paths) {
        let parent: unknown = choice;
        for (const part of path.slice(0, -1)) parent = own(parent, part);
        if (!parent || typeof parent !== "object") continue;
        const name = path[path.length - 1], field = own(parent, name);
        if (field == null) continue;
        fields.push({ parent, name, value: field });
        if (typeof field !== "string") { slot.masked = true; continue; }
        if (changed(field)) slot.masked = true;
        if (streaming) {
          const key = path.join("."), joined = (slot.text.get(key) ?? "") + field;
          if (joined.length > catalog.limits.max_text_chars) throw new Error("Privacy response text budget");
          slot.text.set(key, joined);
        }
      }
      slot.choices.push({ choice, fields });
    }
  }
  for (const slot of slots.values()) {
    // Detect secrets/shapes crossing delta boundaries, before returning any chunk.
    for (const joined of slot.text.values()) if (changed(joined)) slot.masked = true;
    if (!slot.masked) continue;
    for (const { choice, fields } of slot.choices) {
      policy.maskedChoices.add(choice);
      if (streaming) for (const field of fields) {
        const names = policy.maskedText.get(field.parent) ?? new Set<string>();
        names.add(field.name); policy.maskedText.set(field.parent, names);
      }
    }
  }
  return walkValue(value, secrets, "", 0, false, budget, noHints, true, false, incomplete, true, policy);
}
export function discoverProviderResponseSecrets(value: unknown, format: LogprobFormat = "openai.chat"): string[] {
  const budget = newBudget();
  return discover([value], dataEntries, responseTokens(value, format, budget), budget);
}

// Typed FI context session IDs are correlation metadata, not transport sessions.
// Only FITracer's context handoff records them. A flattened key alone never
// grants this exception; exact bytes and native SpanContext identity must match.
// This shared leaf keeps the binding identical across CJS/ESM entry points.
const sessionIdentities = new WeakMap<object, string>();
/** @internal FITracer startActiveSpan handoff only; never infer from attributes. */
export function rememberSessionIdentity(context: object, value: string): void {
  if (context && typeof context === "object" && value.length <= catalog.limits.max_text_chars) sessionIdentities.set(context, value);
}
/** @internal Name-only exception; secrets, credential shapes and media still apply. */
export function isSessionIdentity(context: object | undefined, value: unknown): boolean {
  // Only the typed handoff (or its sanitized detached copy) grants a name-only
  // exception. Equal trace/span IDs on a different context object do not match.
  return context !== undefined && typeof context === "object" && context !== null
    && typeof value === "string" && sessionIdentities.get(context) === value;
}

export interface ProviderResponseBinding { value: string; format: LogprobFormat }
// This leaf is shared by the existing CJS/ESM bridge. Bindings are private,
// exact-byte and context-identity bound; no public telemetry marker grants trust.
const providerResponses = new WeakMap<object, Map<string, ProviderResponseBinding>>();
/** @internal Called only at a provider parser's output handoff, before FISpan admission. */
export function rememberProviderResponse(context: object, attributes: Record<string, unknown>, format: LogprobFormat): void {
  if (!context || typeof context !== "object" || !catalog.logprob_tokens.formats.some(rule => rule.format === format)) return;
  const bindings = providerResponses.get(context) ?? new Map<string, ProviderResponseBinding>();
  for (const key of ["output.value", "raw.output"]) {
    const value = own(attributes, key);
    if (typeof value === "string" && value.length <= catalog.limits.max_text_chars) bindings.set(key, { value, format });
  }
  providerResponses.set(context, bindings);
}
/** @internal No structural, provider-name or attribute-name inference here. */
export function providerResponseBinding(context: object | undefined, key: string, value: unknown): ProviderResponseBinding | undefined {
  const binding = context && providerResponses.get(context)?.get(key);
  return binding && binding.value === value ? binding : undefined;
}
