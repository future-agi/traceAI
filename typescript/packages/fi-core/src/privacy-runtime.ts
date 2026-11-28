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
/** Python's order of two strings: by code point (UTF-16 order differs where one side has a surrogate pair). */
function compareCodePoints(a: string, b: string): number {
  for (let i = 0, j = 0; i < a.length && j < b.length;) {
    const x = a.codePointAt(i)!, y = b.codePointAt(j)!;
    if (x !== y) return x < y ? -1 : 1;
    i += x > 0xffff ? 2 : 1; j += y > 0xffff ? 2 : 1;
  }
  return 0;
}
export const codePointLength = (value: string) => { let n = 0; for (let i = 0; i < value.length; i++) { n++; if (value.charCodeAt(i) >= 0xd800 && value.charCodeAt(i) <= 0xdbff && i + 1 < value.length && value.charCodeAt(i + 1) >= 0xdc00 && value.charCodeAt(i + 1) <= 0xdfff) i++; } return n; };
/** The reference's minimum for a known secret: eight code points (a UTF-16 length of sixteen always holds them). */
export const longEnoughSecret = (value: string) => { const length = value.length; return length >= 16 || (length >= 8 && (typeof value !== "string" || codePointLength(value) >= 8)); };
/** Known secrets in the reference's order: longest first (in code points), equal lengths by code point. Read once per call. */
function longestFirst(secrets: string[]): string[] {
  const lengths = new Map<string, number>();
  for (const secret of secrets) if (typeof secret === "string") lengths.set(secret, codePointLength(secret));
  return secrets.sort((a, b) => typeof a === "string" && typeof b === "string" ? lengths.get(b)! - lengths.get(a)! || compareCodePoints(a, b) : b.length - a.length);
}
let preparedSecrets = new WeakMap<object, PreparedSecrets>();
let preparedSecretAdmissions = 0;
function prepareSecrets(secrets: readonly string[]): readonly string[] {
  const input = [...new Set(secrets.filter(s => longEnoughSecret(s) && s !== replacement))];
  const keyed = secrets !== null && (typeof secrets === "object" || typeof secrets === "function");
  let characters = 0;
  const bounded = input.length <= 64 && input.every(secret => typeof secret === "string"
    && secret.length <= 256 && (characters += secret.length) <= 4096);
  if (!keyed || !bounded) {
    if (keyed) preparedSecrets.delete(secrets);
    return longestFirst(input);
  }
  const cached = preparedSecrets.get(secrets);
  if (cached && cached.input.length === input.length && input.every((secret, i) => secret === cached.input[i])) return cached.ordered;
  // Detach slices before retaining them; the weak key never roots the caller's
  // array. Reset generations, rather than keeping strong keys for FIFO eviction.
  const detached = input.map(secret => secret.split("").join(""));
  const ordered = longestFirst(detached.slice());
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
interface SourceRange { start: number; end: number; /** What replaces the range, when not the placeholder. */ text?: string }
/** Match ORIGINAL bytes before partitioning or rewriting. Longest-first removal
 * splits the remaining source gaps, exactly like knownSecretText; inserted
 * sentinels and existing sentinel segments cannot become a new match.
 * `inserted` (sorted, disjoint) are the spans of the text that the scrub wrote itself, each holding at most one placeholder: such a
 * placeholder bounds a gap like any other, but neither it nor the gap it ends is charged: a scan is charged for the placeholders the
 * text brought, not for those the scrub wrote. A secret's next occurrence is looked for once past each match, not once per gap, so a
 * scan reads the text once per secret however many gaps it has. */
function knownSecretRanges(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, inserted: readonly SourceRange[] = []): SourceRange[] {
  const matches: SourceRange[] = [];
  let gaps: (SourceRange & { free?: boolean })[] = [], at = 0, k = 0;
  for (let next = value.indexOf(replacement); next >= 0; next = value.indexOf(replacement, at)) {
    while (k < inserted.length && inserted[k].end <= next) k++;
    const free = k < inserted.length && inserted[k].start <= next && next + replacement.length <= inserted[k].end;
    if (!free) charge(budget, depth);
    if (next > at) gaps.push(free ? { start: at, end: next, free } : { start: at, end: next });
    at = next + replacement.length;
  }
  if (at < value.length) gaps.push({ start: at, end: value.length });
  // How many gaps each secret is charged for: every gap but those an inserted placeholder ends.
  let charged = gaps.filter(gap => !gap.free).length;
  for (const secret of prepareSecrets(secrets)) {
    charge(budget, depth);
    let remaining: typeof gaps = [], read = 0, kept = 0, matched = false;
    // The first occurrence at or after where the last look started: the gaps are in order, so one that ends past a gap is the first
    // occurrence of each later gap it is not before. A secret that is not a string is looked for as the scan always looked for it.
    let found = -1, cached = false;
    for (let g = 0; g < gaps.length; g++) {
      if (cached && found < 0) {
        // No occurrence is left: the other gaps stay as they are, and are charged as if each were read.
        kept += charged - read;
        for (; read < charged; read++) charge(budget, depth);
        remaining = matched ? remaining.concat(gaps.slice(g)) : gaps;
        break;
      }
      const gap = gaps[g];
      if (!gap.free) { charge(budget, depth); read++; }
      let start = gap.start;
      if (!cached || found >= 0 && found < start) found = value.indexOf(secret, start);
      for (; found >= 0 && found + secret.length <= gap.end; found = value.indexOf(secret, start)) {
        charge(budget, depth);
        matched = true;
        if (found > start) { remaining.push({ start, end: found }); kept++; }
        matches.push({ start: found, end: found + secret.length });
        start = found + secret.length;
      }
      cached = typeof secret === "string";
      if (start < gap.end) { remaining.push(gap.free ? { start, end: gap.end, free: true } : { start, end: gap.end }); if (!gap.free) kept++; }
    }
    gaps = remaining; charged = kept;
  }
  return matches.sort((a, b) => a.start - b.start);
}
const wholeSecret = (value: string, secrets: readonly string[]) => longEnoughSecret(value) && value !== replacement && value !== "__REDACTED__" && secrets.includes(value);
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
/** A run of overlapping occurrences of one known secret in the original text, and whether a catalog edit cuts one of them: overlaps
 * it and runs on past its end (what is left after the edit would no longer be found whole in the rewritten text). An inserted
 * placeholder cuts an occurrence it falls inside; the userinfo edit and a data URI's omission cut none (see Edit). */
interface OccurrenceRun { start: number; end: number; cut: boolean; /** A span the value shapes or the pair rule hide (plainRuns). */ force?: boolean }
/** The runs of every occurrence of each known secret, overlapping occurrences included, sorted by start. Each secret is read in one
 * pass over the text (Knuth-Morris-Pratt), and each occurrence is checked against the edits (sorted, disjoint) by a cursor that only
 * moves forward, so a self-overlapping secret or a long one over many edits costs no more than any other. */
function occurrenceRuns(value: string, secrets: readonly string[], edits: readonly Edit[]): OccurrenceRun[] {
  const runs: OccurrenceRun[] = [], count = edits.length;
  // The first edit at or after each index that can cut an occurrence.
  const cutting = new Int32Array(count + 1);
  cutting[count] = count;
  for (let k = count - 1; k >= 0; k--) cutting[k] = edits[k].whole ? cutting[k + 1] : k;
  for (const secret of prepareSecrets(secrets)) {
    if (typeof secret !== "string" || value.indexOf(secret) < 0) continue;
    const size = secret.length, border = new Int32Array(size);
    for (let i = 1, k = 0; i < size; i++) {
      while (k > 0 && secret.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];
      if (secret.charCodeAt(i) === secret.charCodeAt(k)) k++;
      border[i] = k;
    }
    let run: OccurrenceRun | undefined, j = 0;
    for (let i = 0, k = 0; i < value.length; i++) {
      while (k > 0 && value.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];
      if (value.charCodeAt(i) === secret.charCodeAt(k)) k++;
      if (k < size) continue;
      k = border[k - 1];
      const start = i + 1 - size, end = i + 1;
      // The first edit that ends after the occurrence starts.
      while (j < count && edits[j].to <= start) j++;
      let cut = false;
      // Only an occurrence that runs on past the end of an edit it overlaps is cut: one that ends inside it is hidden there.
      if (j < count && edits[j].from < end && edits[j].to < end) {
        if (!edits[j].whole) cut = true;
        else { const next = cutting[j + 1]; cut = next < count && edits[next].from < end && edits[next].to < end; }
      }
      if (run && start < run.end) { run.end = end; run.cut ||= cut; }
      else { if (run) runs.push(run); run = { start, end, cut }; }
    }
    if (run) runs.push(run);
  }
  return runs.sort((a, b) => a.start - b.start);
}
/** Where the original position p (not inside an edit) stands in the rewrite; `after` places it after a placeholder inserted at p. */
function rewritePosition(edits: readonly Edit[], p: number, after: boolean): number {
  let low = 0, high = edits.length;
  while (low < high) {
    const mid = (low + high) >> 1, edit = edits[mid];
    if (edit.to < p || edit.to === p && (after || edit.from < p)) low = mid + 1; else high = mid;
  }
  return low ? edits[low - 1].end + (p - edits[low - 1].to) : p;
}
/** The catalog's rewrite, the known secrets and what the value shapes and the pair rule hide as one union of spans on the ORIGINAL
 * text, applied in one rewrite. A registered secret occurrence that a catalog edit cuts is joined with every edit and occurrence that
 * overlaps it (and what overlaps those); from the first edit in that union to its end, the original becomes one placeholder, which the
 * catalog owns. So no part of an occurrence that runs on past a value the catalog replaced is left after it, while what stands before
 * that value (the name and delimiter the catalog and the pair rule read, as the reference keeps them) stays. A union that holds a span
 * the value shapes or the pair rule hide (`force`, see plainRuns), or two overlapping occurrences of which one is cut, starts where its
 * first interval starts: every overlapping occurrence is hidden whole with it, so a shorter one replaced first cannot leave part of a
 * longer one. Every other edit stays as the catalog wrote it, and every other occurrence is left whole for the known-secret pass.
 * `found` holds the runs of occurrences (occurrenceRuns) and the forced spans, by start; edits are sorted and disjoint. Returns the
 * rewritten text and its catalog spans. */
function spanUnion(output: string, edits: readonly Edit[], spans: readonly Span[], found: readonly OccurrenceRun[], size: number): [string, Span[]] {
  if (!found.length || !edits.length && !found.some(run => run.force)) return [output, spans as Span[]];
  const unions: [start: number, end: number, inserted: boolean, forced: boolean][] = [];
  // A union runs on to the end of the URL token of its last edit (`limit`; the end of the text for a whole URL value): the pair rule
  // would read a credential name inside a cut occurrence to the end of its value, which in a URL token can run to the token's end,
  // so taking the rest of the token keeps hidden all that the reference hides there.
  let to = -1, first = -1, limit = -1, inserted = false, cut = false, force = false, low = -1, seen = -1, overlapped = false, e = 0, f = 0;
  const close = () => {
    if (!cut && !force) return;
    const whole = force || overlapped, from = whole ? low : first, insert = whole ? false : inserted;
    const last = unions[unions.length - 1];
    if (last && last[1] >= from) { last[1] = Math.max(last[1], to); last[3] ||= force; }
    else unions.push([from, to, insert, force]);
  };
  while (e < edits.length || f < found.length) {
    // The next interval by start: an edit, or an occurrence. An inserted placeholder joins only the union it falls inside.
    const edit = f >= found.length || e < edits.length && edits[e].from <= found[f].start;
    const start = edit ? edits[e].from : found[f].start, end = edit ? edits[e].to : found[f].end;
    const run = edit ? undefined : found[f], cuts = !!run && run.cut, bound = edit ? edits[e].limit ?? size : -1;
    edit ? e++ : f++;
    if (start === end) { if (start < to) { if (first < 0) { first = start; inserted = true; } limit = Math.max(limit, bound); if (cut && limit > to) to = limit; } continue; }
    if (start < to) {
      if (end > to) to = end; if (edit && first < 0) first = start; limit = Math.max(limit, bound); cut ||= cuts;
      // Two occurrences overlap: the second starts before the first ends.
      if (run?.force) force = true; else if (run) { overlapped ||= start < seen; seen = Math.max(seen, end); }
      if (cut && limit > to) to = limit;
      continue;
    }
    close();
    to = end; first = edit ? start : -1; limit = bound; inserted = false; cut = cuts; low = start;
    force = !!run?.force; seen = run && !run.force ? end : -1; overlapped = false;
    if (cut && limit > to) to = limit;
  }
  close();
  if (!unions.length) return [output, spans as Span[]];
  const moved: Span[] = [], parts: string[] = [];
  let copied = 0, shift = 0, k = 0;
  for (const [start, end, insert, forced] of unions) {
    const at = rewritePosition(edits, start, !insert), stop = rewritePosition(edits, end, false);
    while (k < spans.length && spans[k].end <= at) { moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission }); k++; }
    while (k < spans.length && spans[k].start < stop) k++;
    parts.push(output.slice(copied, at), replacement);
    moved.push(forced ? { start: at + shift, end: at + shift + replacement.length, omission: false, forced } : { start: at + shift, end: at + shift + replacement.length, omission: false });
    shift += replacement.length - (stop - at);
    copied = stop;
  }
  for (; k < spans.length; k++) moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission });
  parts.push(output.slice(copied));
  return [parts.join(""), moved];
}
/** The first stage of the export text rules: the URL and data URI catalog over prose. Each URL token is rewritten by the catalog and
 * each data URI is omitted before any known secret or value shape is read, so a secret or shape never consumes a name, a delimiter
 * or a userinfo '@' that the catalog and the pair rule need. A private key that a known secret overlaps is left to the known-secret
 * pass, as the reference reads it only as a value shape after it. A bare image or audio token is omitted last (see mediaTokens).
 * Returns the text and the spans the catalog wrote in it (see spanUnion), by position. */
function catalogText(value: string, incomplete: boolean, secrets: readonly string[], pairs = true): [string, Span[]] {
  // A private key runs across URL-like tokens and lines, and a token read on its own (a leading '/' or '?') may be rewritten
  // into a form that no longer holds the key's header: read it once over the whole prose first.
  if (!secretIn(value, secrets)) value = privateKeyText(value);
  let end = 0, result = "";
  const spans: Span[] = [], edits: Edit[] = [];
  // Each data URI is cut where its continuation lines stop being base64; the rest is read as text.
  for (const [start, stop, data] of scanText(typeof value === "string" ? TEXT_SCAN : new RegExp(TEXT.source, TEXT.flags), value, 2)) {
    const original = value.slice(start, stop);
    result += value.slice(end, start);
    if (!data) {
      const closing = start > 0 && value[start - 1] === "'" && original.endsWith("'") ? "'" : "";
      const [rewritten, written, changed] = framedUrl(closing ? original.slice(0, -1) : original, incomplete);
      const at = result.length;
      for (const span of written) spans.push({ start: span.start + at, end: span.end + at, omission: span.omission });
      const limit = start + original.length - closing.length;
      for (const edit of changed) edits.push({ ...edit, from: edit.from + start, to: edit.to + start, start: edit.start + at, end: edit.end + at, limit });
      result += rewritten + closing;
    } else {
      const clean = dataText(original);
      if (clean !== original) {
        spans.push({ start: result.length, end: result.length + clean.length, omission: true });
        // A data URI's omission cuts no occurrence, as the reference reads a secret after it: what a secret runs on into stays.
        edits.push({ from: start, to: stop, start: result.length, end: result.length + clean.length, whole: true, limit: stop });
      }
      result += clean;
    }
    end = stop;
  }
  // The occurrences are read only where a registered secret occurs; the shapes and the pair rule then read the catalog's text with
  // none replaced, and what they hide where an occurrence overlaps their context joins the union.
  const output = result + value.slice(end);
  if (!secretIn(value, secrets)) return [output, spans];
  const found = occurrenceRuns(value, secrets, edits);
  return spanUnion(output, edits, spans, byStart(found, forcedRuns(value, output, spans, edits, found, pairs, false)), value.length);
}
/** Whether any known secret occurs in the text. */
function secretIn(value: string, secrets: readonly string[]): boolean {
  return secrets.length > 0 && prepareSecrets(secrets).some(secret => typeof secret === "string" && value.indexOf(secret) >= 0);
}
/** A complete bare JPEG/MP3 token (it can begin with '/') is omitted after the pair rule has read the names in the text: a name written
 * right against the token is part of it, and must still be read as a name. Check the WHOLE framed token, never a slash-prefixed
 * substring of an authority/path. */
function mediaTokens(value: string): string {
  let end = 0, result = "";
  for (const [start, stop, data] of scanText(typeof value === "string" ? TEXT_SCAN : new RegExp(TEXT.source, TEXT.flags), value, 2)) {
    if (data) continue;
    const original = value.slice(start, stop);
    const closing = start > 0 && value[start - 1] === "'" && original.endsWith("'") ? "'" : "";
    const media = mediaReplacement(closing ? original.slice(0, -1) : original);
    if (media === undefined) continue;
    result += value.slice(end, start) + media + closing;
    end = stop;
  }
  return end === 0 ? value : result + value.slice(end);
}
function text(value: string, secrets: readonly string[], framed: boolean, incomplete = false, pairs = false, bare = true): string {
  return typeof value === "string" && secretIn(value, secrets) ? onlyAdds(value, undefined, () => orderedText(value, secrets, framed, incomplete, pairs, bare)) : orderedText(value, secrets, framed, incomplete, pairs, bare);
}
function orderedText(value: string, secrets: readonly string[], framed: boolean, incomplete = false, pairs = false, bare = true): string {
  // This function only receives prose, names or URL-owned values. The order is the reference's: the URL and data URI catalog,
  // then known secrets (one at a time, longest first), then value shapes once over the assembled text, then the pair rule.
  if (framed && isURLValue(value)) return wholeUrlText(value, secrets, incomplete);
  // Known values, then shapes, once over the assembled text: a run beside a URL token is read whole. The catalog's spans move with them.
  let [result, owned] = credentialRuns(...catalogText(value, incomplete, secrets, pairs), secrets);
  // The pair rule runs after known secrets and value shapes, and before any media is omitted.
  if (pairs) result = credentialPairs(result, [], undefined, owned);
  // Bare media is read in the prose between the tokens, never in reconstructed URL syntax.
  if (bare) result = bareGaps(result);
  return mediaTokens(result);
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
/** The characters a name may be written with as an encoded pair (version 5): the name characters, '%' and whitespace. Any other encoded
 * pair (a delimiter, a separator, a quote, a non-ASCII byte) ends a name. A flag name holds no encoded whitespace. */
const NAME_ENCODED = new Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-[]% \t\n\x0b\x0c\r");
const FLAG_ENCODED = new Set([...NAME_ENCODED].filter(c => !" \t\n\x0b\x0c\r".includes(c)));
/** The character the encoded pair whose hex digits start at `at` stands for. */
const encodedChar = (text: string, at: number) => String.fromCharCode(parseInt(text.slice(at, at + 2), 16));
/** Whether an encoded pair (a '%' and two hex digits) starts at `at`. */
const escapeAt = (text: string, at: number) => text[at] === "%" && has(HEX_DIGITS, text[at + 1]) && has(HEX_DIGITS, text[at + 2]);
/** A name read once (version 5): [name, undecided]. Its encoded pairs are replaced by what they stand for, once; the name is undecided
 * when an encoded pair is left after that, or when the name held one and has no letter, digit or underscore. A name with no '%' is itself. */
function readName(raw: string): [string, boolean] {
  if (!raw.includes("%")) return [raw, false];
  const name = unquote(raw);
  if (PERCENT_ESCAPE.test(name)) return [name, true];
  return [name, PERCENT_ESCAPE.test(raw) && !/[\p{L}\p{N}_]/u.test(name)];
}
/** The name that ends at nameEnd, read back over name characters, encoded pairs of the allowed characters and a '%' that starts no
 * encoded pair, at most NAME_CAP characters (version 5): [start, held, overflow]. held: the read passed over a '%'; overflow: the window is
 * full and more of the name lies before it. */
function wideName(text: string, nameEnd: number, floor: number, allowed: ReadonlySet<string> = NAME_ENCODED): [number, boolean, boolean] {
  let start = nameEnd, held = false;
  while (start > floor && nameEnd - start < NAME_CAP) {
    if (escapeBefore(text, start, floor)) {
      if (!allowed.has(encodedChar(text, start - 2))) break;
      start -= 3; held = true;
    } else if (PAIR_NAME_CHARS.has(text[start - 1])) start--;
    else if (text[start - 1] === "%") { start--; held = true; }
    else break;
  }
  const overflow = nameEnd - start >= NAME_CAP && start > floor && (PAIR_NAME_CHARS.has(text[start - 1]) || text[start - 1] === "%" || escapeBefore(text, start, floor));
  return [start, held, overflow];
}
/** A flag name's end moved on over name characters, encoded pairs of flag name characters and a '%' that starts no encoded pair, up to limit. */
function flagNameEnd(text: string, end: number, limit: number): number {
  while (end < text.length && end < limit) {
    if (PAIR_NAME_CHARS.has(text[end])) end++;
    else if (escapeAt(text, end)) { if (!FLAG_ENCODED.has(encodedChar(text, end + 1))) break; end += 3; }
    else if (text[end] === "%") end++;
    else break;
  }
  return end;
}
/** Whether a flag name read by flagNameEnd goes on at end. */
function flagNameContinues(text: string, end: number): boolean {
  if (end >= text.length) return false;
  return PAIR_NAME_CHARS.has(text[end]) || text[end] === "%" && (!escapeAt(text, end) || FLAG_ENCODED.has(encodedChar(text, end + 1)));
}
/** Whether a name is one of the list headers (the value runs to the end of the line), read as it is written or read once. */
const listHeader = (name: string) => PAIR_LIST_HEADERS.has(normalizePrivacyName(name)) || PAIR_LIST_HEADERS.has(normalizePrivacyName(readName(name)[0]));
/** Whether a name, as the pair rule reads it, is an exclusion name of the catalog: the name or one of its components. */
function pairExcluded(name: string): boolean {
  pathBudget(name);
  return javaExcluded(normalizePrivacyName(name)) || name.split(/[.\[\]]/).some(part => !!part && !/^\p{Nd}+$/u.test(part) && javaExcluded(normalizePrivacyName(part)));
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
  const [name, held] = wideName(text, k, 0);
  if (held ? listHeader(text.slice(name, k)) : PAIR_LIST_HEADERS.has(normalizePrivacyName(text.slice(name, k)))) {
    while (value < text.length && text[value] !== "\n" && text[value] !== "\r") value++;
    return value;
  }
  return pairValueEnd(text, value);
}
function consumedEnd(text: string, start: number, stop: number, valueEndFor: (k: number, size: number, floor: number) => number): number {
  while (stop > start) {
    let size: number;
    if ((text[stop - 1] === "=" || text[stop - 1] === ":") && separatorAt(text, stop - 1)) size = 1;
    else if (stop - 3 >= start && separatorAt(text, stop - 3) === 3) size = 3;
    else return stop;
    const end = valueEndFor(stop - size, size, start);
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
/** The dashes of a flag that starts at i: 2 for "--", 1 for a single dash followed by a name start (a letter or an underscore, written plain
 * or as an encoded pair, version 5), after any '%' that starts no encoded pair, else 0. */
function flagDashes(text: string, i: number): number {
  if (text.startsWith("--", i)) return 2;
  if (!text.startsWith("-", i)) return 0;
  let k = i + 1;
  const limit = i + 1 + NAME_CAP;
  while (k < limit && text[k] === "%" && !escapeAt(text, k)) k++;
  // A run of marks as long as a name window is read as a flag: its name past the window is undecided.
  if (k >= limit) return 1;
  return has(PAIR_NAME_START, text[k]) || escapeAt(text, k) && PAIR_NAME_START.has(encodedChar(text, k + 1)) ? 1 : 0;
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
function separatorPair(text: string, i: number, floor: number, credential: (name: string) => boolean, owned: CatalogOwned = noneOwned): SeparatorPair {
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
  if (!pair) {
    // Version 5: the name read back over its encoded pairs is read once; this only ever adds a pair.
    const [start, held, wideOverflow] = wideName(text, nameEnd, floor);
    if ((held || wideOverflow) && nameEnd - start >= 2) {
      const raw = text.slice(start, nameEnd);
      if (wideOverflow) { pathBudget(raw); return [true, raw, true]; }
      // A URL query, fragment or path value the URL catalog already replaced is not read again.
      if ((!readable || nameDecision(raw, credential, pairExcluded)) && !owned(i + (text[i] === "%" ? 3 : 1), raw)) return [true, readName(raw)[0], false];
    }
  }
  return [pair, name, overflow];
}
/** Whether the value that starts at a position is a placeholder the URL catalog wrote (a span it recorded, never a placeholder that was
 * in the input), followed by '&', '#', ';' or the end of the text, for a name the URL catalog reads: a value already replaced. */
type CatalogOwned = (value: number, raw: string) => boolean;
const noneOwned: CatalogOwned = () => false;
function catalogOwned(text: string, spans: readonly Span[]): CatalogOwned {
  const owned = spans.filter(span => !span.omission);
  if (!owned.length) return noneOwned;
  return (value, raw) => {
    let low = 0, high = owned.length;
    while (low < high) { const mid = (low + high) >> 1; if (owned[mid].start < value) low = mid + 1; else high = mid; }
    if (low >= owned.length || owned[low].start !== value) return false;
    const end = owned[low].end;
    return (end >= text.length || "&#;".includes(text[end])) && urlName(raw.replace(/^[?&#;]+/, ""));
  };
}
function flagAhead(text: string, start: number, credential: (name: string) => boolean): boolean {
  const dashes = flagDashes(text, start);
  if (dashes !== 1) return dashes === 2;
  let end = start + 1;
  while (end < text.length && PAIR_NAME_CHARS.has(text[end]) && end - start <= NAME_CAP) end++;
  end = flagNameEnd(text, end, start + NAME_CAP + 1);
  if (end - start < 3 || end - start > NAME_CAP || end >= text.length || !pySpace(text[end])) return false;
  let after = end;
  while (after < text.length && pySpace(text[after])) after++;
  return after < text.length && nameDecision(text.slice(start, end), credential);
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
interface Span { start: number; end: number; omission: boolean; /** A union placeholder that holds a forced span (spanUnion). */ forced?: boolean }
/** A catalog edit: the original characters [from, to) were written as the text [start, end) of the rewrite. `whole` marks the userinfo
 * edit and a data URI's omission: what a registered secret runs on into past them is read as the reference reads it. */
interface Edit { from: number; to: number; start: number; end: number; whole?: boolean; limit?: number }
/** The credential pair rule: replace the value of every credential-named (or undecided) pair with the placeholder, in one
 * left-to-right pass linear in the text. skip holds the spans the catalog itself replaced in a whole URL value, by position; ownedSpans
 * the spans the catalog wrote in the text, by position (see catalogOwned). */
function credentialPairs(text: string, skipSpans: readonly Span[] = [], edits?: [number, number][], ownedSpans: readonly Span[] = []): string {
  const length = text.length, out: string[] = [], known = new Map<string, boolean>(), owned = catalogOwned(text, ownedSpans);
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
    // Version 5: a quoted name is also read once.
    return [overflow || credential(name) || (escaped !== undefined && (escaped[1] || credential(escaped[0]))) || opening >= 0 && nameDecision(name, credential), name];
  };
  const equalsValueEnd = (k: number, size: number, name: string) => {
    let value = k + size;
    while (value < length && value - k - size < 8 && text[value] === " ") value++;
    if (listHeader(name)) {
      while (value < length && text[value] !== "\n" && text[value] !== "\r") value++;
      return value;
    }
    return pairValueEnd(text, value);
  };
  const valueEndFor = (k: number, size: number, floor: number, readEnd = true): number => {
    const quoted = jsonName(text, k);
    if (quoted !== undefined && quoted[0] >= floor) {
      const [pair, name] = quotedPair(quoted);
      if (size === 1 && text[k] === ":") {
        if (!pair) return -1;
        if (!readEnd) return k;
        let value = k + 1;
        while (value < length && javaSpace(text[value])) value++;
        return jsonValueEnd(text, value, listHeader(name), quoted[1]);
      }
      if (pair) return readEnd ? equalsValueEnd(k, size, name) : k;
    }
    if (!separatorPair(text, k, floor, credential, owned)[0]) return -1;
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
      if (size && valueEndFor(valueEnd, size, begin, false) >= 0) { if (first < 0) first = valueEnd; last = valueEnd; }
      valueEnd++;
    }
    let ahead = valueEnd;
    while (ahead < length && (dashesSpace(text[ahead]) || encodedSpaceAhead(text, ahead))) ahead += encodedSpaceAhead(text, ahead) || 1;
    if (ahead > valueEnd && ahead < length && separatorAt(text, ahead) && valueEndFor(ahead, separatorAt(text, ahead), begin, false) >= 0) {
      if (first < 0) first = ahead;
      last = ahead;
    }
    for (const k of [...new Set([first, last])].filter(k => k !== -1).sort((a, b) => a - b)) valueEnd = Math.max(valueEnd, valueEndFor(k, separatorAt(text, k), begin));
    return consumedEnd(text, begin, valueEnd, valueEndFor);
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
        const header = listHeader(name);
        let end = jsonValueEnd(text, value, header, quoted[1]);
        if (!header) end = consumedEnd(text, value, end, valueEndFor);
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
      if (!listHeader(name)) stop = consumedEnd(text, value, stop, valueEndFor);
    } else if (c === "=" || c === ":" || (c === "%" && ENCODED_EQUALS.has(text.slice(i + 1, i + 3)))) {
      const size = c === "%" ? 3 : 1;
      const [pair, name, overflow] = separatorPair(text, i, copied, credential, owned);
      if (pair) {
        let value = i + size;
        while (value < length && value - i - size < 8 && text[value] === " ") value++;
        if (skip.length && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
        start = value;
        if (!overflow && listHeader(name)) {
          stop = value;
          while (stop < length && text[stop] !== "\n" && text[stop] !== "\r") stop++;
        } else stop = consumedEnd(text, value, pairValueEnd(text, value), valueEndFor);
      }
    } else if (c === "-" && (i === 0 || pySpace(text[i - 1]) || encodedSpaceWidth(text, i, 0)) && flagDashes(text, i)) {
      const dashes = flagDashes(text, i);
      // Version 5: the name is read over name characters, encoded pairs of name characters and a '%' that starts none, and read once.
      const window = i + NAME_CAP + dashes;
      let end = flagNameEnd(text, i + dashes, window);
      const overflow = end >= window && flagNameContinues(text, end);
      if (overflow) end = flagNameEnd(text, end, length);
      if (overflow) pathBudget(text.slice(i, end));
      if (end < length && end - i - dashes >= (dashes === 2 ? 1 : 2)
        && (dashesSpace(text[end]) || LINE_BREAKS.has(text[end]) || encodedSpaceAhead(text, end))
        && (overflow || nameDecision(text.slice(i, end), credential))) {
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
      edits?.push([start, stop]);
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
  parts: string[] = []; spans: Span[] = []; edits: Edit[] = []; length = 0; source = 0;
  /** Copy the next characters of the original as they are. */
  keep(text: string): void { this.parts.push(text); this.length += text.length; this.source += text.length; }
  /** Write text in place of the next `consumed` characters of the original (none: the text is inserted). */
  put(text: string, omission = false, consumed = 0): void {
    this.spans.push({ start: this.length, end: this.length + text.length, omission });
    this.edits.push({ from: this.source, to: this.source + consumed, start: this.length, end: this.length + text.length });
    this.parts.push(text); this.length += text.length; this.source += consumed;
  }
  /** The last text written also stands for the next `consumed` characters of the original. */
  extend(consumed: number): void { this.edits[this.edits.length - 1].to += consumed; this.source += consumed; }
  /** Write a part of a URL: each run (sorted, disjoint; an empty run inserts the placeholder) becomes the placeholder, and each
   * data URI in the original text its omission. Where a run and a data URI overlap, the run's placeholder is written and the
   * data URI's omission covers the rest of it. `raw`, when given, is how many characters of the original the text (a decoded form of
   * them) stands for: what is written is then one edit over all of them. */
  region(text: string, runs: readonly (readonly [number, number])[] = [], raw?: number): void {
    const source = this.source, edits = this.edits.length, written = this.length;
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
        if (last !== "run" + r) { this.put(replacement, false, runs[r][1] - position); last = "run" + r; } else this.extend(runs[r][1] - position);
        position = runs[r][1];
      } else if (d < data.length && data[d][0] <= position) {
        const end = r < runs.length ? Math.min(data[d][1], runs[r][0]) : data[d][1];
        if (last !== "data" + d) { this.put(data[d][2], true, end - position); last = "data" + d; } else this.extend(end - position);
        position = end;
      } else {
        let end = length;
        if (r < runs.length) end = Math.min(end, runs[r][0]);
        if (d < data.length) end = Math.min(end, data[d][0]);
        this.keep(text.slice(position, end));
        position = end; last = "";
      }
    }
    if (raw !== undefined) {
      this.edits.length = edits;
      this.edits.push({ from: source, to: source + raw, start: written, end: this.length });
      this.source = source + raw;
    }
  }
  result(): [string, Span[], Edit[]] { return [this.parts.join(""), this.spans, this.edits]; }
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
/** Query and fragment names are read as written and once decoded, also reading '+' as a space. */
function urlName(raw: string): boolean {
  return (raw.includes("+") && queryName(unquotePlus(raw))) || nameDecision(raw, queryName, pairExcluded);
}
/** Whether a name after a '?' is a credential name; a name past NAME_CAP or one holding more than QUESTION_CAP '?' marks is
 * undecided (true). */
function afterQuestion(text: string, marks: readonly number[], end: number): boolean {
  if (!marks.length) return false;
  if (end - marks[0] - 1 > NAME_CAP || marks.length > QUESTION_CAP) return true;
  return marks.some(q => urlName(text.slice(q + 1, end)));
}
function questionName(rawName: string): boolean {
  if (urlName(rawName)) return true;
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
      let credential = k > name && nameDecision(base.slice(name, k), credentialName, pairExcluded);
      if (!decided && !credential && k > first) credential = nameDecision(base.slice(first, k), credentialName, pairExcluded);
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
function framedUrl(original: string, incomplete = false): [string, Span[], Edit[]] {
  const out = new Rewrite();
  const [target, hashmark, fragment] = partition(original, "#");
  let [base, question, rawQuery] = partition(target, "?");
  const userinfo = USERINFO.exec(base);
  if (userinfo) {
    out.keep(userinfo[1]); out.put(replacement, false, userinfo[0].length - userinfo[1].length - 1); out.edits[out.edits.length - 1].whole = true;
    out.keep("@"); base = base.slice(userinfo[0].length);
  }
  pathPairs(base, out);
  out.keep(question);
  const parsed = rawQuery.split("&").map(pair => { const [name, equals, value] = partition(pair, "="); return { name, equals, value, decodedName: unquotePlus(name), decodedValue: unquotePlus(value) }; });
  // A truncated query may end in a syntactically valid MIME prefix. Without complete context no query MIME can attest to a non-media value.
  const hints = hintIndex(parsed.filter(pair => pair.equals).map(pair => [pair.decodedName, pair.decodedValue]), "", noHints, incomplete ? 0 : Infinity, true);
  parsed.forEach((pair, index) => {
    if (index) out.keep("&");
    out.keep(pair.name + pair.equals);
    if (!pair.equals) return;
    if (queryName(pair.decodedName) || questionName(pair.name)) { out.put(replacement, false, pair.value.length); return; }
    const clean = mediaReplacement(pair.decodedValue, pair.decodedName, hints, true, false, false, incomplete);
    if (clean !== undefined) { out.put(clean, true, pair.value.length); return; }
    const cut = secondQuestionCut(pair.value);
    if (cut >= 0) { out.keep(pair.value.slice(0, cut)); out.put(replacement, false, pair.value.length - cut); }
    else if (dataText(pair.decodedValue, true) !== pair.decodedValue) out.region(pair.decodedValue, [], pair.value.length);
    else out.keep(pair.value);
  });
  out.keep(hashmark);
  if (hashmark) tailPairs(fragment, out);
  return out.result();
}
/** Where a position of a text stands in the text before one rewrite: `runs` (sorted, disjoint) of the earlier text were each replaced
 * by the placeholder, which starts at `outs` in the later one. A range's start (end) inside a placeholder stands for its run's start (end). */
function beforeRuns(runs: readonly (readonly [number, number])[], outs: readonly number[], p: number, end: boolean): number {
  let low = 0, high = runs.length;
  while (low < high) { const mid = (low + high) >> 1; if (end ? outs[mid] < p : outs[mid] <= p) low = mid + 1; else high = mid; }
  if (!low) return p;
  const k = low - 1, after = outs[k] + replacement.length;
  if (end ? p <= after : p < after) return end ? runs[k][1] : runs[k][0];
  return runs[k][1] + (p - after);
}
/** Where a position of the catalog's text stands in the original: a range's start (end) inside what an edit wrote stands for the
 * edit's start (end). Edits are sorted and disjoint. */
function beforeEdits(edits: readonly Edit[], p: number, end: boolean): number {
  let low = 0, high = edits.length;
  while (low < high) { const mid = (low + high) >> 1; if (end ? edits[mid].start < p : edits[mid].start <= p) low = mid + 1; else high = mid; }
  if (!low) return p;
  const edit = edits[low - 1];
  if (end ? p <= edit.end : p < edit.end) return end ? edit.to : edit.from;
  return edit.to + (p - edit.end);
}
/** What the value shapes and the pair rule hide when they read the catalog's text with no registered value replaced, as forced spans of
 * the ORIGINAL text (for spanUnion). A registered value only adds redaction: replacing one first must not take out the context (a key
 * prefix, a credential name, a separator, a userinfo '@') that hides a credential. A span is returned only where a registered occurrence
 * can take that context out: one a catalog edit cuts that overlaps the span or its context; one that starts before a pair's value and
 * ends inside its name, its separator or its value; one that holds the head of a shape's run but not its end; a run of occurrences that
 * starts inside a shape's run (it holds the run's tail, or a part between its ends). Elsewhere the reads are the same either way.
 * `output`, `spans` and `edits` are the catalog's (catalogText or framedUrl); `found` holds the occurrences by start; `whole` marks a whole
 * URL value, whose pair rule skips the spans the catalog replaced. Each pass reads the text once (the shapes, then the pair rule). */
function plainRuns(output: string, spans: readonly Span[], edits: readonly Edit[], found: readonly OccurrenceRun[], pairs: boolean, whole: boolean): OccurrenceRun[] {
  if (!found.length) return [];
  // Whether a cut occurrence overlaps [from, to): one that starts before `to` and reaches past `from`.
  const cuts = found.filter(run => run.cut), starts = cuts.map(run => run.start);
  let reach = -1;
  const reaches = cuts.map(run => (reach = Math.max(reach, run.end)));
  const cutIn = (from: number, to: number) => {
    let low = 0, high = starts.length;
    while (low < high) { const mid = (low + high) >> 1; if (starts[mid] < to) low = mid + 1; else high = mid; }
    return low > 0 && reaches[low - 1] > from;
  };
  // The occurrences by end: whether one ends in (from, to], and the least start of those that end in (from, to).
  const byEnd = [...found].sort((a, b) => a.end - b.end), ends = byEnd.map(run => run.end);
  const firstEnd = (p: number) => { let low = 0, high = ends.length; while (low < high) { const mid = (low + high) >> 1; if (ends[mid] <= p) low = mid + 1; else high = mid; } return low; };
  // One that starts before a pair's value and ends after the start of its context but before the value's end: it takes out the name,
  // the separator or the head of the value, and leaves the rest of the value to be read without them.
  const crosses = (name: number, from: number, to: number) => {
    for (let k = firstEnd(name); k < ends.length && ends[k] < to; k++) if (byEnd[k].start < from) return true;
    return false;
  };
  const headOf = (start: number, end: number) => {
    for (let k = firstEnd(start); k < ends.length && ends[k] < end; k++) if (byEnd[k].start <= start) return true;
    return false;
  };
  // A run of occurrences (overlapping occurrences of one secret, joined) that starts inside a shape's run: it holds the run's tail, or a
  // part between its ends. Replaced first, or taken into a union that starts where it starts, it leaves the run's head, which the shape
  // no longer reads whole. An occurrence that overlaps an earlier one of the same secret starts its run where that one starts, so it is
  // decided by where the run starts. `found` is sorted by start.
  const begins = found.map(run => run.start);
  const partOf = (start: number, end: number) => {
    let low = 0, high = begins.length;
    while (low < high) { const mid = (low + high) >> 1; if (begins[mid] <= start) low = mid + 1; else high = mid; }
    return low < begins.length && begins[low] < end;
  };
  let text = output, moved = spans as Span[];
  const layers: (readonly [number, number])[][] = [], outs: number[][] = [];
  for (const shape of valueShapes) {
    const runs = (typeof shape === "function" ? jwtRuns(text)
      : [...text.matchAll(shape as RegExp)].map(match => [match.index!, match.index! + match[0].length] as [number, number])).filter(([start, end]) => end > start);
    const at: number[] = [];
    let shift = 0;
    for (const [start, end] of runs) { at.push(start + shift); shift += replacement.length - (end - start); }
    layers.push(runs); outs.push(at);
    [text, moved] = replaceRuns(text, runs, moved);
  }
  const back = (p: number, end: boolean, layer: number) => {
    for (let k = layer - 1; k >= 0; k--) p = beforeRuns(layers[k], outs[k], p, end);
    return beforeEdits(edits, p, end);
  };
  const result: OccurrenceRun[] = [];
  const add = (start: number, end: number) => { if (end > start) result.push({ start, end, cut: false, force: true }); };
  layers.forEach((runs, k) => {
    for (const [from, to] of runs) { const start = back(from, false, k), end = back(to, true, k); if (cutIn(start, end) || headOf(start, end) || partOf(start, end)) add(start, end); }
  });
  if (pairs) {
    const ranges: [number, number][] = [];
    credentialPairs(text, whole ? moved : [], ranges, moved);
    for (const [start, end] of ranges) {
      // The pair's context: the spaces before its value, then its name and separator (the word before them).
      let context = start;
      while (context > 0 && start - context < 8 && pySpace(text[context - 1])) context--;
      while (context > 0 && start - context < NAME_CAP + 16 && !pySpace(text[context - 1])) context--;
      const from = back(start, false, layers.length), to = back(end, true, layers.length), name = back(context, false, layers.length);
      if (cutIn(name, to) || crosses(name, from, to)) add(from, to);
    }
  }
  return result.sort((a, b) => a.start - b.start);
}
function forcedRuns(value: string, output: string, spans: readonly Span[], edits: readonly Edit[], found: readonly OccurrenceRun[], pairs: boolean, whole: boolean): OccurrenceRun[] {
  if (!forcing) return [];
  const forced = plainRuns(output, spans, edits, found, pairs, whole);
  forcedCount += forced.length;
  return forced;
}
/** Occurrences and forced spans, each sorted by start, as one list by start. */
const byStart = (found: readonly OccurrenceRun[], forced: readonly OccurrenceRun[]) => forced.length ? [...found, ...forced].sort((a, b) => a.start - b.start) : found;
/** A whole URL value at a framed site: the catalog pass records its spans, joined with the registered secrets it cuts (spanUnion);
 * known secrets and value shapes move them; the pair rule then reads the URL text and skips only the spans still recorded. */
function wholeUrlText(value: string, secrets: readonly string[], incomplete: boolean): string {
  const [url, spans, edits] = framedUrl(value, incomplete);
  const found = secretIn(value, secrets) ? occurrenceRuns(value, secrets, edits) : [];
  const [clean, moved] = credentialRuns(...spanUnion(url, edits, spans, byStart(found, forcedRuns(value, url, spans, edits, found, true, true)), value.length), secrets);
  return credentialPairs(clean, moved, undefined, moved);
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
/** Known secrets, then value shapes, then the pair rule over a catalogued text, the catalog's spans moved with the first two. */
function catalogPairs(value: string, secrets: readonly string[], owned: Span[] | undefined): string {
  if (!owned) return credentialPairs(credentialText(value, secrets));
  const [clean, moved] = credentialRuns(value, owned, secrets);
  return credentialPairs(clean, [], undefined, moved);
}
/** Spans moved through edits (sorted, disjoint; each written as its text, or the placeholder): a span an edit overlaps is dropped. */
function movedSpans(spans: readonly Span[], edits: readonly SourceRange[]): Span[] {
  const moved: Span[] = [];
  let shift = 0, k = 0;
  for (const edit of edits) {
    while (k < spans.length && spans[k].end <= edit.start) { moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission }); k++; }
    while (k < spans.length && spans[k].start < edit.end) k++;
    shift += (edit.text ?? replacement).length - (edit.end - edit.start);
  }
  for (; k < spans.length; k++) moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission });
  return moved;
}
// ---- End of the shared privacy spec v4 section. ----
export interface PrivacyBudget { nodes: number; text: number; seen: WeakSet<object> }
const newBudget = (): PrivacyBudget => ({ nodes: catalog.limits.max_nodes, text: catalog.limits.max_text_chars, seen: new WeakSet() });
/** A budget for the passes that read a text again (its source spans, its owners, the known secrets in it, the two renderings of a tree):
 * they bound their own work and leave the node and text charge to the one walk that the reference charges. Its limit is a multiple of
 * the node limit, so a text the reference accepts is never refused by a scan of it. */
const workBudget = (): PrivacyBudget => ({ nodes: catalog.limits.max_nodes * 4, text: Infinity, seen: new WeakSet() });
/** While a scan of a text runs (see scanned), the nodes it charges are tallied here and the node limit less the one node of the value
 * that holds the text, not what is left of the budget, bounds it. */
let scanTally: { nodes: number; last: number } | undefined;
function charge(budget: PrivacyBudget, depth: number, length = 0): void {
  const left = --budget.nodes;
  if (scanTally) scanTally.last = left;
  if (scanTally ? ++scanTally.nodes >= catalog.limits.max_nodes || depth > catalog.limits.max_depth : left < 0 || depth > catalog.limits.max_depth) throw new Error("Privacy structure budget");
  if ((budget.text -= length) < 0) throw new Error("Privacy text budget");
}
/** A scan of a text (its known secrets, its owners, the secrets that cross them) bounds its own work by the node limit and leaves the
 * node charge of the value to the one walk that the reference charges: what the scan charged is given back when it completes, so a
 * string is one node, as the reference counts it. A scan that throws keeps what it charged. */
function scanned<T>(budget: PrivacyBudget, run: () => T): T {
  const outer = scanTally, tally = { nodes: 0, last: 0 };
  scanTally = tally;
  let result: T;
  try { result = run(); } finally { scanTally = outer; }
  // Written from the last count the scan saw, so the budget is not read an extra time.
  if (tally.nodes) budget.nodes = tally.last + tally.nodes;
  return result;
}
/** Charges a budget of its own outside the scan that runs, so the scan's tally does not count it. */
function chargeAside(budget: PrivacyBudget, depth: number): void {
  const tally = scanTally;
  scanTally = undefined;
  try { charge(budget, depth); } finally { scanTally = tally; }
}
/** Whether a parsed JSON tree whose root is read at `depth` (each child one level deeper) reaches past the depth limit. */
function pastDepth(node: unknown, depth: number): boolean {
  if (depth > catalog.limits.max_depth) return true;
  return node !== null && typeof node === "object" && Object.values(node).some(child => pastDepth(child, depth + 1));
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
    // A name of more than max_depth components exports the placeholder before the depth budget is read.
    pathBudget(path); charge(budget, level); space(); parent?.delete(key);
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
  let tokenIndex = 0, crossingSecret = false, decodedToken: (SourceRange & { string: boolean; key?: boolean }) | undefined, decoded = "";
  const spelled = new Map<string, boolean>();
  for (const range of ranges) {
    while (tokenIndex < tokens.length && tokens[tokenIndex].end <= range.start) tokenIndex++;
    const token = tokens[tokenIndex];
    if (!token || range.start < token.start || range.end > token.end) { crossingSecret = true; break; }
    if (token.string) {
      // A token is read once, however many secrets it holds: the ranges are in order, so the ones of a token follow each other.
      if (decodedToken !== token) { decodedToken = token; decoded = JSON.parse(value.slice(token.start, token.end)); spelled.clear(); }
      // The decoded walker owns ordinary matches, including repeatedly encoded
      // JSON numbers. Plain substitution here would invalidate that inner JSON.
      // Only a raw spelling absent from the decoded value needs a source edit.
      const spelling = value.slice(range.start, range.end);
      let found = spelled.get(spelling);
      if (found === undefined) spelled.set(spelling, found = decoded.includes(spelling));
      // A match that takes a quote of the string is not where the decoded string holds the same spelling, if it holds it at all: the
      // decoded walker may replace that other place in another order, and leave this one to the reference's read of the text.
      if (!found || range.start === token.start || range.end === token.end) {
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
  // The reference charges a complete JSON text once for its own length and once per node of the tree it parses. The passes below that
  // read the same text again (its source spans, the audit of repeated numbers, the two renderings) read it against a node budget
  // of their own and no text budget: they bound their own work and leave the charge to the one walk of the parsed tree.
  const extra = workBudget;
  const source = jsonNumbers(value, parsed, secrets, depth, extra());
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
      const clean = walkValue(JSON.parse(scalar.raw), secrets, scalar.path, scalar.level, inheritedAudio, extra(), scalar.hints, true, inheritedMedia, incomplete, true, undefined, false, origin, scalar.raw);
      if (typeof clean === "number" ? expected !== scalar.raw : clean !== expected) reuse = false;
    }
  }
  let numbers = origin;
  if (source.patched !== value) {
    value = source.patched; parsed = JSON.parse(value);
    // Source edits retain all unrelated numeric spellings; rebuild their private
    // ownership map against the new tree, never via JSON.stringify/Number aliases.
    numbers = { ...jsonNumbers(value, parsed, [], depth, extra()), incompleteURLs, payloadRole };
  }
  // Source-masked MIME/type values must not erase the original media evidence.
  const clean = walkValue(parsed, secrets, "", depth, inheritedAudio, budget, source.sourceHints, true, inheritedMedia, incomplete, true, undefined, false, numbers, numbers.root);
  const rendered = renderJSON(clean, numbers.output, extra(), depth, [], numbers.root);
  const numericOnly = renderJSON(parsed, numbers.input, extra(), depth, secrets, numbers.root);
  return reuse && rendered === numericOnly ? value : rendered;
}
/** Walk only plain, already-materialized data. The Node S1 boundary rejects proxies before calling. */
// ---- Shared privacy spec v6 round 3: a text that begins as a JSON object or array and is not one JSON value. ----
/** Where a text begins as the reference reads it (Python's str.lstrip, then byte order marks, then str.lstrip again). */
function jsonTextStart(value: string): number {
  let i = 0;
  while (i < value.length && pySpace(value[i])) i++;
  while (value[i] === "\ufeff") i++;
  while (i < value.length && pySpace(value[i])) i++;
  return i;
}
const JSON_WHITESPACE = new Set(" \t\n\r");
const JSON_CONSTANTS = ["NaN", "Infinity", "-Infinity"] as const;
const JSON_NUMBER_AT = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
/**
 * One iterative pass of the JSON reader of the reference over text from `start`: the end of the JSON value read there (or -1 when the
 * text stops being JSON before a value is complete), the positions of NaN, Infinity and -Infinity read as values, and whether an
 * object read to its end repeats a member name (names compared as JSON reads them; an object that never closes is not counted).
 */
function readJSONPrefix(text: string, start: number): { end: number; repeated: boolean; constants: [number, number][] } {
  const stack: ({ names: Set<string>; repeated: boolean } | undefined)[] = [], constants: [number, number][] = [];
  let i = start, repeated = false;
  const space = () => { while (i < text.length && JSON_WHITESPACE.has(text[i])) i++; };
  const string = (): string | undefined => {
    // A string: no control character below U+0020, each escape one of the JSON forms.
    const from = i++;
    while (i < text.length) {
      const c = text.charCodeAt(i);
      if (c === 34) { i++; try { return JSON.parse(text.slice(from, i)); } catch { return undefined; } }
      if (c < 0x20) return undefined;
      if (c === 92) {
        const n = text[i + 1];
        if (n === "u") { if (!/^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6))) return undefined; i += 6; }
        else if (n !== undefined && '"\\/bfnrt'.includes(n)) i += 2;
        else return undefined;
      } else i++;
    }
    return undefined;
  };
  const fail = () => ({ end: -1, repeated, constants });
  // expect: 0 a value, 1 a member name or '}', 2 ',' or the close, 3 a member name (after ',')
  let expect = 0;
  for (;;) {
    if (expect === 0) {
      if (stack.length) space();
      const c = text[i];
      if (c === "{") { stack.push({ names: new Set(), repeated: false }); i++; space(); if (text[i] === "}") { i++; stack.pop(); expect = 2; } else expect = 3; }
      else if (c === "[") { stack.push(undefined); i++; space(); if (text[i] === "]") { i++; stack.pop(); expect = 2; } else expect = 0; continue; }
      else if (c === '"') { if (string() === undefined) return fail(); expect = 2; }
      else {
        const constant = JSON_CONSTANTS.find(name => text.startsWith(name, i));
        if (constant) { constants.push([i, i + constant.length]); i += constant.length; expect = 2; }
        else if (text.startsWith("true", i)) { i += 4; expect = 2; }
        else if (text.startsWith("false", i)) { i += 5; expect = 2; }
        else if (text.startsWith("null", i)) { i += 4; expect = 2; }
        else { JSON_NUMBER_AT.lastIndex = i; const m = JSON_NUMBER_AT.exec(text); if (!m) return fail(); i += m[0].length; expect = 2; }
      }
      if (expect === 3) continue;
    }
    if (expect === 3) {
      if (text[i] !== '"') return fail();
      const name = string();
      if (name === undefined) return fail();
      const object = stack[stack.length - 1]!;
      if (object.names.has(name)) object.repeated = true; else object.names.add(name);
      space();
      if (text[i] !== ":") return fail();
      i++; space(); expect = 0; continue;
    }
    // expect === 2: after a value
    if (!stack.length) return { end: i, repeated, constants };
    space();
    const top = stack[stack.length - 1], c = text[i];
    if (c === ",") { i++; space(); expect = top ? 3 : 0; continue; }
    if (top && c === "}") { if (top.repeated) repeated = true; stack.pop(); i++; expect = 2; continue; }
    if (!top && c === "]") { stack.pop(); i++; expect = 2; continue; }
    return fail();
  }
}
/** The text with each JSON string form (\uXXXX and the short forms) replaced by what it spells. */
const JSON_SPELLING = /\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g;
const JSON_SIMPLE: Record<string, string> = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
function unspell(text: string): string {
  return text.includes("\\") ? text.replace(JSON_SPELLING, (_, hex: string | undefined, simple: string | undefined) => hex ? String.fromCharCode(parseInt(hex, 16)) : JSON_SIMPLE[simple!]) : text;
}
/** The quoted names that stand before a colon, in one pass (a string runs to its closing quote or to the end of its line). */
function memberNames(text: string): string[] {
  const names: string[] = [], length = text.length;
  for (let i = 0; ;) {
    i = text.indexOf('"', i);
    if (i < 0) return names;
    const start = ++i;
    while (i < length) {
      const c = text[i];
      if (c === "\\") i += 2;
      else if (c === '"' || c === "\n") break;
      else i++;
    }
    if (i < length && text[i] === '"') {
      const end = i++;
      while (i < length && JSON_WHITESPACE.has(text[i])) i++;
      if (i < length && text[i] === ":") names.push(text.slice(start, end));
    }
  }
}
/** The one reading every name site of the reference uses (version 5): the name as written, else read once. Where `excluded` is given and
 * the reading is an exclusion name, the reading alone decides. */
function nameDecision(raw: string, credential: (name: string) => boolean, excluded?: (name: string) => boolean): boolean {
  if (!raw.includes("%")) return credential(raw);
  const [read, undecided] = readName(raw);
  if (!(excluded !== undefined && excluded(read)) && credential(raw)) return true;
  return undecided || credential(read) || (read.includes("%") && credential(read.replace(/%/g, "")));
}
const countMarkers = (text: string) => text.split(replacement).length - 1;
/**
 * Whether a text that begins as a JSON object or array and is not one JSON value is the placeholder (version 6, round 3): a text
 * that holds NaN, Infinity or -Infinity and is one JSON value otherwise, when that reading repeats a member name or its export would
 * change it; any other text, when it repeats a member name in an object read to its end before it stops being JSON, or holds a
 * credential name as a quoted member name (read as JSON reads names) or as a pair the pair rule reads only once its JSON string
 * forms are spelled out.
 */
/** The decisions of the NaN and infinity reading, per list of secrets: a nested JSON string that holds one is walked once, however
 * many layers above it read it again. A text the export would change is the placeholder wherever it is read again; a text it leaves
 * unchanged is read so again at the same depth or nearer the root, where the depth limit is further away. Kept to a bound of
 * characters, then started again. */
const constantsDecisions = new WeakMap<readonly string[], { chars: number; decided: Map<string, { changed: boolean; depth: number }> }>();
const CONSTANTS_MEMO_CHARS = 1 << 22;
function unreadableJSON(value: string, start: number, secrets: readonly string[], depth: number, budget: PrivacyBudget, ctx: { media: boolean; audio: boolean }, incomplete: boolean): boolean {
  let prefix: ReturnType<typeof readJSONPrefix>;
  try { prefix = readJSONPrefix(value, start); } catch { return true; }
  if (prefix.repeated) return true;
  const leading = value.slice(0, start);
  const constants = prefix.end >= 0 && prefix.constants.length > 0 && [...leading].every(c => JSON_WHITESPACE.has(c)) && [...value.slice(prefix.end)].every(c => JSON_WHITESPACE.has(c));
  // A quoted member name that is a credential name, read as JSON reads names and as a name with percent forms is read once. In a
  // text that is one JSON value apart from its constants, a name of more than max_depth components exports the whole value as the
  // placeholder, as the walk of that value does; in any other such text, this text.
  try {
    const seen = new Set<string>();
    for (const name of memberNames(value)) {
      if (seen.has(name)) continue;
      seen.add(name);
      if (nameDecision(unspell(name), credentialName)) return true;
    }
  } catch (error) { if (constants && error instanceof PrivacyPathBudget) throw error; return true; }
  if (constants) {
    // One JSON value once NaN and the infinities are read as numbers: the placeholder when its export would change it.
    let memo = constantsDecisions.get(secrets);
    if (!memo || memo.chars > CONSTANTS_MEMO_CHARS) { memo = { chars: 0, decided: new Map() }; constantsDecisions.set(secrets, memo); }
    const key = `${+ctx.media}${+ctx.audio}${+incomplete}\u0000${value}`;
    const known = memo.decided.get(key);
    if (known && (known.changed || depth <= known.depth)) return known.changed;
    let text = "", at = 0;
    for (const [from, to] of prefix.constants) { text += value.slice(at, from) + "0"; at = to; }
    text += value.slice(at);
    let changed: boolean;
    try {
      const parsed: unknown = JSON.parse(text);
      const copy: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: new WeakSet() };
      const clean = walkValue(parsed, secrets, "", depth + 1, ctx.audio, copy, noHints, true, ctx.media, incomplete, true);
      changed = JSON.stringify(clean) !== JSON.stringify(parsed);
    } catch (error) {
      // A name of more than max_depth components exports the whole value as the placeholder; any other failure, this text.
      if (error instanceof PrivacyPathBudget) throw error;
      changed = true;
    }
    memo.decided.set(key, { changed, depth });
    memo.chars += key.length;
    return changed;
  }
  const plain = unspell(value);
  if (plain === value) return false;
  return countMarkers(credentialPairs(plain)) - countMarkers(plain) > countMarkers(credentialPairs(value)) - countMarkers(value);
}
export function scrubValue(value: any, secrets: readonly string[] = [], path = "", depth = 0, inheritedAudio = false, budget: PrivacyBudget = newBudget(), hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, incomplete = false, ambiguousBase64 = true): any {
  return pathBudgeted(() => walkValue(value, secrets, path, depth, inheritedAudio, budget, hints, framed, inheritedMedia, incomplete, ambiguousBase64));
}
/** A name (a path, a map key, a pair name in text) of more than max_depth components exports the whole value as the placeholder. */
function pathBudgeted<T>(scrub: () => T): T | string {
  try { return scrub(); } catch (error) { if (error instanceof PrivacyPathBudget) return replacement; throw error; }
}
function walkValue(value: any, secrets: readonly string[], path: string, depth: number, inheritedAudio: boolean, budget: PrivacyBudget, hints: PrivacyHints, framed: boolean, inheritedMedia: boolean, incomplete: boolean, ambiguousBase64: boolean, policy?: ResponsePolicy, exemptToken = false, numbers?: JsonNumbers, numericLexeme?: string): any {
  // The name path budget first: a path of more than limits.max_depth components exports the whole value as the placeholder,
  // whatever the depth or node budget says.
  pathBudget(path);
  charge(budget, depth);
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
    // A text that begins as a JSON object or array and is not one JSON value: the placeholder when it repeats a member name or holds
    // a credential name (version 6, round 3); any other such text takes the text path.
    const opening = jsonTextStart(value);
    if (value[opening] === "{" || value[opening] === "[") {
      let one = false;
      try { JSON.parse(value); one = true; } catch { /* not one JSON value */ }
      if (!one && unreadableJSON(value, opening, secrets, depth, budget, context(path, hints, inheritedMedia, inheritedAudio), incomplete)) return replacement;
    }
    // JSON tool arguments are independently framed. Share budgets across nested encodings.
    if (/^\s*[\[{"]/.test(value)) {
      let parsed: unknown; let valid = false;
      try { parsed = JSON.parse(value); valid = true; } catch { /* Prose is handled below. */ }
      // The reference reads only an object or an array as JSON: a string literal, number or keyword is read as raw text at the depth of
      // the value that holds it, with its quotes and backslashes as they stand.
      if (valid && (parsed === null || typeof parsed !== "object")) valid = false;
      if (valid) {
        const ctx = context(path, hints, inheritedMedia, inheritedAudio);
        // Source-number origins and budgets remain private to this JSON owner.
        // Never reprocess complete JSON as prose or match a rounded numeric alias.
        const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;
        return structuredText(scrubJSON(value, parsed, secrets, depth + 1, budget, ctx.audio, ctx.media, incomplete, numbers?.incompleteURLs, payloadRole), parsed, secrets, framed, incomplete);
      }
    }
    const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;
    // Text is read at the depth of the value that holds it, as the reference reads a string leaf: only a container or a parsed JSON text
    // goes one level deeper.
    return scrubEmbeddedText(value, secrets, depth, budget, framed, incomplete, numbers?.incompleteURLs, payloadRole);
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

/** End (exclusive) of the quoted string that opens at `start`, read as /"(?:\\.|[^"\\])*"/ reads it ('.' stops at a line terminator),
 * or a negative number when it does not close: -1 at the end of the text, or -(at + 2) for a backslash at `at` that no escape
 * can follow. */
function stringEnd(text: string, start: number): number {
  for (let i = start + 1; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c === 92) {
      const next = text.charCodeAt(i + 1);
      if (i + 1 >= text.length) return -1;
      if (next === 10 || next === 13 || next === 0x2028 || next === 0x2029) return -(i + 2);
      i++;
    } else if (c === 34) return i + 1;
  }
  return -1;
}
/** The key and value strings of every `"key" : "value"` pair in the text, as the non-overlapping matches of
 * /("(?:\\.|[^"\\])*"\s*:\s*)("(?:\\.|[^"\\])*")/g, in time linear in the text. A quote inside a string that was read is not tried as the
 * start of another (the scan from it reads the same text in the same escape state, so it ends where that scan ended), and a
 * string with no end ends the search. */
function stringPairs(text: string): [string, string][] {
  const pairs: [string, string][] = [];
  for (let start = text.indexOf('"'); start >= 0;) {
    const keyEnd = stringEnd(text, start);
    if (keyEnd === -1) break;
    if (keyEnd < 0) { start = text.indexOf('"', -keyEnd); continue; }
    let at = keyEnd;
    while (at < text.length && JS_SPACE.test(text[at])) at++;
    if (text[at] === ":") {
      at++;
      while (at < text.length && JS_SPACE.test(text[at])) at++;
      if (text[at] === '"') {
        const valueEnd = stringEnd(text, at);
        if (valueEnd > 0) { pairs.push([text.slice(start, keyEnd), text.slice(at, valueEnd)]); start = text.indexOf('"', valueEnd); continue; }
        if (valueEnd === -1) break;
        start = text.indexOf('"', -valueEnd); continue;
      }
    }
    start = text.indexOf('"', keyEnd - 1);
  }
  return pairs;
}
const JS_SPACE = /\s/;
interface TextOwner {
  start: number; end: number; kind: "json" | "wrapper" | "tail" | "field";
  parsed?: unknown; key?: string; valueStart?: number; keyEnd?: number;
}
/** Find owners on ORIGINAL input, before replacements move delimiters. Invalid
 * bracketed prose is handled recursively, while incomplete JSON stays closed.
 */
function textOwners(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {
  // The text was charged where it was read as a value (walkValue, scrubText), once, as the reference does.
  charge(budget, depth);
  const owners: TextOwner[] = [];
  const urls: RegExpMatchArray[] = []; let urlIndex = 0;
  TEXT.lastIndex = 0;
  for (const match of value.matchAll(typeof value === "string" ? TEXT : new RegExp(TEXT.source, TEXT.flags))) { charge(budget, depth); urls.push(match); }
  // A quote with no closing quote after it leaves every later quote unclosed too: a scan from a later quote reads the same text in
  // the same escape state as the scan that failed (each later quote was passed as an escaped one), so it is not repeated.
  let unclosed = false;
  const closing = (from: number): number => {
    if (unclosed) return -1;
    const stop = quotedEnd(value, from);
    if (stop < 0) unclosed = true;
    return stop;
  };
  for (let i = 0; i < value.length; i++) {
    while (urlIndex < urls.length && urls[urlIndex].index! + urls[urlIndex][0].length <= i) urlIndex++;
    if (urlIndex < urls.length && urls[urlIndex].index! <= i) { i = urls[urlIndex].index! + urls[urlIndex][0].length - 1; continue; }
    const c = value[i];
    if (c === '"') {
      const end = closing(i); if (end < 0) continue;
      charge(budget, depth);
      let parsed: unknown;
      try { parsed = JSON.parse(value.slice(i, end)); } catch { i = end - 1; continue; }
      // A key/value fragment outside a complete object still needs the named
      // media/credential policy; a property name alone grants no value exemption.
      const colon = /^\s*:\s*/.exec(value.slice(end));
      if (colon) {
        const valueStart = end + colon[0].length;
        const stop = value[valueStart] === '"' ? closing(valueStart) : -1;
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
      if (next === '"') { const stop = closing(end); if (stop < 0) { end = value.length; break; } end = stop - 1; }
      else if (next === "{" || next === "[") nesting++;
      else if ((next === "}" || next === "]") && --nesting === 0) { end++; break; }
    }
    if (nesting !== 0) { owners.push({ start: i, end: value.length, kind: "tail" }); break; }
    let parsed: unknown; let valid = false;
    try { parsed = JSON.parse(value.slice(i, end)); valid = true; } catch { /* Prose wrapper around real JSON. */ }
    owners.push({ start: i, end, kind: valid ? "json" : "wrapper", parsed }); i = end - 1;
  }
  return owners;
}
/** The one owner of a text that is a complete JSON document. A document is read by its own parse, whatever its nesting: the name path
 * budget and then the depth budget decide, in the order the reference reads them, instead of a scan of the brackets in the text. */
function wholeOwner(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {
  // The text was charged where it was read as a value (walkValue, scrubText), once, as the reference does.
  charge(budget, depth);
  const start = value.search(/\S/);
  let end = value.length;
  while (/\s/.test(value[end - 1])) end--;
  charge(budget, depth);
  return [{ start, end, kind: "json", parsed: JSON.parse(value.slice(start, end)) }];
}
/** Whether the range starts or ends inside a backslash sequence of a JSON string: an odd run of backslashes ends the range or stands just before it. */
function splitsSequence(value: string, range: SourceRange): boolean {
  let before = 0, after = 0;
  while (range.start - 1 - before >= 0 && value[range.start - 1 - before] === "\\") before++;
  while (range.end - 1 - after >= range.start && value[range.end - 1 - after] === "\\") after++;
  return before % 2 === 1 || after % 2 === 1;
}
/** Reconcile original credential ranges with owners before any replacement
 * moves delimiters. JSON-contained matches stay with that owner; crossing
 * matches consume the whole intersected owner plus overlapping prose bytes. */
function crossOwnerEdits(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget, reads?: { budget: PrivacyBudget; secrets: ReadonlySet<string> }, readBefore?: (position: number) => boolean, secretRun?: (run: string) => boolean): SourceRange[] {
  if (!ranges.length) return [];
  // Keep this second scan: native RegExp legacy statics are observable from
  // budget accessors, even though the scan itself has no charge calls.
  TEXT.lastIndex = 0;
  const urls = [...value.matchAll(typeof value === "string" ? TEXT : new RegExp(TEXT.source, TEXT.flags))].map(match => ({ start: match.index!, end: match.index! + match[0].length }));
  const edits: SourceRange[] = [];
  let ownerIndex = 0, urlIndex = 0;
  for (const range of ranges) {
    // A range the pair rule reads, which is not one known secret, is charged with the owners and URLs it crosses to a budget of its own,
    // outside the scan: the reference charges nothing for what it reads in a text, so a text with many pairs is not refused for them.
    const pay = reads && !reads.secrets.has(range.start + ":" + range.end) ? () => chargeAside(reads.budget, depth) : () => charge(budget, depth);
    pay();
    while (ownerIndex < owners.length && owners[ownerIndex].end <= range.start) ownerIndex++;
    const enclosing = owners[ownerIndex];
    // A secret inside a JSON string is replaced by the owner, in the string's own encoding. One that holds a quote or a control
    // character, or a bracket, or starts or ends inside a backslash sequence, is not a piece of one string, so it takes the owner out whole, as the owner redacts itself, before any later read
    // of the text changes the owner.
    const enclosed = !!enclosing && enclosing.start <= range.start && enclosing.end >= range.end;
    if (enclosed && enclosing.kind === "json" && (/["\u0000-\u001f[\]{}]/.test(value.slice(range.start, range.end)) || splitsSequence(value, range))) {
      const before = edits.at(-1);
      const startRun = () => enclosing.start === range.start && !!secretRun?.(value.slice(Math.max(0, enclosing.start - 3), enclosing.start + 1));
      const endRun = () => enclosing.end === range.end && !!secretRun?.(value.slice(enclosing.end - 1, enclosing.end + 3));
      // A later secret merged into the owner's edit takes the owner's first or last character on the same reading as below.
      if (before && enclosing.start < before.end) {
        if (enclosing.end > before.end || startRun() || endRun()) delete before.text;
        before.end = Math.max(before.end, enclosing.end);
      }
      // A JSON string that is the secret byte for byte is replaced as the reference replaces it, by the bare placeholder: the quotes
      // go with it, so the pair rule reads a name around it as one name.
      // A secret that takes the owner's first or last character (a quote or a bracket) where that character and the three outside it are
      // a run of a known secret takes it with the owner: the placeholder is bare, so the run is not left beside the placeholder. The
      // reference replaces the longer secret first, and the shorter one that holds the run is gone with it.
      else edits.push(typeof enclosing.parsed === "string" && enclosing.start === range.start && enclosing.end === range.end
        || startRun() || endRun()
        ? { start: enclosing.start, end: enclosing.end } : { start: enclosing.start, end: enclosing.end, text: JSON.stringify(replacement) });
      continue;
    }
    if (enclosed && (enclosing.kind === "json" || enclosing.kind === "field" && range.start >= enclosing.valueStart! && !/["\u0000-\u001f]/.test(value.slice(range.start, range.end)) && !splitsSequence(value, range)
        || enclosing.kind === "wrapper" && enclosing.start < range.start && range.end < enclosing.end
        || enclosing.kind === "tail" && enclosing.start < range.start)) {
      // Brackets keep a secret that starts past their opener only while no edit comes before them: an edit before them can pair the
      // quotes and brackets differently, so the text read again finds other owners, and a tail's opening run taken out as one
      // placeholder would cut the secret. The secret is then replaced where it stands, as the reference replaces it. A pair or value
      // shape the whole-text read redacts before the brackets is such an edit too: the brackets are read again once it is made.
      if (enclosing.kind !== "wrapper" && enclosing.kind !== "tail" || !edits.length && !readBefore?.(enclosing.start)) continue;
      const prior = edits.at(-1);
      if (!prior || range.start >= prior.end) edits.push({ start: range.start, end: range.end });
      continue;
    }
    let { start, end } = range;
    for (let i = ownerIndex; i < owners.length && owners[i].start < end; i++) {
      pay();
      start = Math.min(start, owners[i].start); end = Math.max(end, owners[i].end);
    }
    while (urlIndex < urls.length && urls[urlIndex].end <= start) urlIndex++;
    for (let i = urlIndex; i < urls.length && urls[i].start < end; i++) {
      pay();
      // A URL-contained credential keeps ordinary query structure. If the
      // match crosses its boundary, consume the URL rather than strand syntax.
      if (start < urls[i].start || end > urls[i].end) { start = Math.min(start, urls[i].start); end = Math.max(end, urls[i].end); }
    }
    const prior = edits.at(-1);
    if (prior && start < prior.end) { if (end > prior.end) delete prior.text; prior.end = Math.max(prior.end, end); }
    else edits.push({ start, end });
  }
  return edits;
}
function redactCrossOwnerSecrets(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget): string {
  return replaceEdits(value, crossOwnerEdits(value, ranges, owners, depth, budget));
}
function replaceEdits(value: string, edits: readonly SourceRange[]): string {
  let end = 0, out = "";
  for (const edit of edits) { out += value.slice(end, edit.start) + (edit.text ?? replacement); end = edit.end; }
  return out + value.slice(end);
}
/** A stage that replaced `runs` (sorted, disjoint, in the coordinates of the text it read): each run's position in the text it wrote. */
interface StageRuns { runs: SourceRange[]; from: number[] }
function stageRuns(runs: SourceRange[]): StageRuns {
  const from: number[] = [];
  let shift = 0;
  for (const run of runs) { from.push(run.start + shift); shift += replacement.length - (run.end - run.start); }
  return { runs, from };
}
/** The position in the text a stage read of a position in the text it wrote: a position inside a placeholder moves to the start (or,
 * for the end of a range, the end) of the run the placeholder stands for. Binary search over the runs. */
function beforeStage(position: number, stage: StageRuns, isEnd: boolean): number {
  const { runs, from } = stage, L = replacement.length;
  // The first run whose placeholder ends at or after the position (an end) or after it (a start).
  let low = 0, high = runs.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (isEnd ? from[mid] + L >= position : from[mid] + L > position) high = mid; else low = mid + 1;
  }
  const shift = low === 0 ? 0 : from[low - 1] + L - runs[low - 1].end;
  if (low < runs.length && (isEnd ? position > from[low] : position >= from[low])) return isEnd ? runs[low].end : runs[low].start;
  return position - shift;
}
/** The ranges of the text, in its own coordinates, that the reference's value shapes and pair rule redact once known secrets (`known`)
 * are replaced: the reference reads the pair rule over the text with the secrets in place of their bytes, so a name beside a secret is
 * read there. A secret that crosses a JSON owner takes the whole owner out of the text, and with it the name the pair rule needs; these
 * ranges are taken out as well. With `empty`, a pair whose value is empty (a separator just before a line break) is kept, as an empty
 * range where the pair rule writes its placeholder: a secret that runs from the separator over the line break cuts that pair. */
function referenceRedactions(value: string, known: readonly SourceRange[], empty = false): SourceRange[] {
  const stages: StageRuns[] = [];
  let current = value;
  const stage = (runs: SourceRange[]) => { stages.push(stageRuns(runs)); current = replaceEdits(current, runs); };
  stage(known.map(range => ({ ...range })));
  for (const shape of valueShapes) {
    const runs: SourceRange[] = (typeof shape === "function" ? jwtRuns(current) : [...current.matchAll(shape as RegExp)].map(match => [match.index!, match.index! + match[0].length] as [number, number]))
      .filter(([start, end]) => end > start).map(([start, end]) => ({ start, end }));
    if (runs.length) stage(runs);
  }
  const pairs: [number, number][] = [];
  credentialPairs(current, [], pairs);
  stages.push(stageRuns(pairs.map(([start, end]) => ({ start, end }))));
  const found: SourceRange[] = [];
  for (let s = 1; s < stages.length; s++) {
    for (const run of stages[s].runs) {
      let start = run.start, end = run.end;
      for (let k = s - 1; k >= 0; k--) { start = beforeStage(start, stages[k], false); end = beforeStage(end, stages[k], true); }
      if (end > start || empty && s === stages.length - 1) found.push({ start, end });
    }
  }
  return found;
}
/** The first of the sorted, disjoint ranges that ends after the position (binary search). */
function firstEnding<T extends SourceRange>(sorted: readonly T[], position: number): T | undefined {
  let low = 0, high = sorted.length;
  while (low < high) { const mid = (low + high) >> 1; if (sorted[mid].end > position) high = mid; else low = mid + 1; }
  return sorted[low];
}
/** Ranges sorted by start, the overlapping ones joined. */
function joinRanges(ranges: readonly SourceRange[]): SourceRange[] {
  const out: SourceRange[] = [];
  for (const range of [...ranges].sort((a, b) => a.start - b.start || a.end - b.end)) {
    const last = out.at(-1);
    if (last && range.start < last.end) last.end = Math.max(last.end, range.end);
    else out.push({ ...range });
  }
  return out;
}
/** The edits with the ranges that no owner reads added: an edit that already covers a range keeps its own text. */
function withPlainRanges(edits: SourceRange[], inner: SourceRange[]): SourceRange[] {
  if (!inner.length) return edits;
  const out: SourceRange[] = [];
  for (const edit of [...edits, ...inner].sort((a, b) => a.start - b.start || a.end - b.end)) {
    const last = out.at(-1);
    if (last && edit.start < last.end) { if (edit.end > last.end) { delete last.text; last.end = edit.end; } }
    else out.push({ ...edit });
  }
  return out;
}
/** A text that is one complete JSON object or array. The reference reads a string literal, number or keyword as raw text, so those are not read leaf by leaf. */
function wholeContainer(value: string): boolean {
  return /^\s*[\[{]/.test(value) && wholeJSON(value);
}
/** Whether a range the pair rule and value shapes redact on the text with its known secrets in place is left out of what the reference
 * redacts once the secrets are replaced, together with the secrets themselves (sorted, joined ranges; one forward pass). */
function secretCutsName(value: string, ranges: readonly SourceRange[]): boolean {
  const secrets = joinRanges(ranges), cover = joinRanges([...ranges, ...referenceRedactions(value, ranges)]);
  let k = 0, j = 0;
  for (const range of joinRanges(referenceRedactions(value, []))) {
    while (k < cover.length && cover[k].end <= range.start) k++;
    while (j < secrets.length && secrets[j].end <= range.start) j++;
    // A secret that takes out a letter or digit of the range takes out part of the value itself, not only of its name.
    let inValue = false;
    for (let i = j; i < secrets.length && secrets[i].start < range.end && !inValue; i++) inValue = /[\p{L}\p{N}]/u.test(value.slice(Math.max(range.start, secrets[i].start), Math.min(range.end, secrets[i].end)));
    if (inValue) continue;
    // What the range keeps readable once the secrets and the reference's own read are taken out: a letter or digit there is a value left.
    let at = range.start;
    for (let i = k; i < cover.length && cover[i].start < range.end; i++) {
      if (cover[i].start > at && /[\p{L}\p{N}]/u.test(value.slice(at, cover[i].start))) return true;
      at = Math.max(at, cover[i].end);
    }
    if (at < range.end && /[\p{L}\p{N}]/u.test(value.slice(at, range.end))) return true;
  }
  return false;
}
/** The runs of one to four characters of each known secret, of any length, as the secrets were given. Read once per secrets array and
 * kept while the array holds the same secrets, so the texts of one value or span do not read the secrets again. */
let secretRunSets = new WeakMap<object, { input: readonly unknown[]; runs: ReadonlySet<string> }>();
let secretRunAdmissions = 0;
function secretRuns(secrets: readonly string[]): ReadonlySet<string> {
  const input = Array.from(secrets as readonly unknown[]);
  const keyed = secrets !== null && (typeof secrets === "object" || typeof secrets === "function");
  const cached = keyed ? secretRunSets.get(secrets) : undefined;
  if (cached && cached.input.length === input.length && input.every((secret, i) => secret === cached.input[i])) return cached.runs;
  const runs = new Set<string>();
  for (const secret of input) {
    if (typeof secret !== "string") continue;
    for (let i = 0; i < secret.length; i++) for (let k = 1; k <= 4 && i + k <= secret.length; k++) runs.add(secret.slice(i, i + k));
  }
  if (keyed) {
    if (!cached) {
      if (secretRunAdmissions >= 64) { secretRunSets = new WeakMap(); secretRunAdmissions = 0; }
      secretRunAdmissions++;
    }
    // Detached copies, as prepareSecrets keeps them: the cache does not hold slices of the caller's strings.
    secretRunSets.set(secrets, { input: input.map(secret => typeof secret === "string" ? secret.split("").join("") : secret), runs });
  }
  return runs;
}
const isHighSurrogate = (c: number) => c >= 0xd800 && c <= 0xdbff;
const isLowSurrogate = (c: number) => c >= 0xdc00 && c <= 0xdfff;
/** The quoted runs of a text that /"(?:[^"\\\n]|\\.)*"/g matches, in one pass: a quote whose run reaches a line break or the end of the text
 * without a closing quote is not one, and neither is any quote inside that run (each sees the same escapes), so the scan goes on after it. */
function stringLiterals(text: string): [number, number][] {
  const found: [number, number][] = [];
  for (let at = text.indexOf('"'); at >= 0;) {
    let k = at + 1, closed = false;
    while (k < text.length) {
      const c = text.charCodeAt(k);
      if (c === 34) { closed = true; break; }
      if (c === 10) break;
      if (c === 92) { if (k + 1 >= text.length || text.charCodeAt(k + 1) === 10) break; k += 2; } else k++;
    }
    if (closed) { found.push([at, k + 1]); at = text.indexOf('"', k + 1); } else at = k < text.length ? text.indexOf('"', k + 1) : -1;
  }
  return found;
}
/** The runs of four code points of each known secret, as the secrets were given (a reader reads code points, not code units). Read once per
 * secrets array and kept while it holds the same secrets. */
let codePointRunSets = new WeakMap<object, { input: readonly unknown[]; runs: ReadonlySet<string> }>();
let codePointRunAdmissions = 0;
function codePointRuns(secrets: readonly string[]): ReadonlySet<string> {
  const input = Array.from(secrets as readonly unknown[]);
  const keyed = secrets !== null && (typeof secrets === "object" || typeof secrets === "function");
  const cached = keyed ? codePointRunSets.get(secrets) : undefined;
  if (cached && cached.input.length === input.length && input.every((secret, i) => secret === cached.input[i])) return cached.runs;
  const runs = new Set<string>();
  for (const secret of input) {
    if (typeof secret !== "string") continue;
    const points = Array.from(secret);
    for (let i = 0; i + 4 <= points.length; i++) runs.add(points.slice(i, i + 4).join(""));
  }
  if (keyed) {
    if (!cached) {
      if (codePointRunAdmissions >= 64) { codePointRunSets = new WeakMap(); codePointRunAdmissions = 0; }
      codePointRunAdmissions++;
    }
    codePointRunSets.set(secrets, { input: input.map(secret => typeof secret === "string" ? secret.split("").join("") : secret), runs });
  }
  return runs;
}
/** The most times each run of four code points of a known secret is shown in one reading of a JSON string literal of the output: the literal
 * itself, the string it encodes, and each JSON text, JSON string literal, escape and percent-encoding read inside it, again and again to
 * the depth limit. Placeholders break runs. Undefined where the readings pass a bound of their own, a multiple of the literal's length. */
function shownRuns(literal: string, runs: ReadonlySet<string>): Map<string, number> | undefined {
  const shown = new Map<string, number>(), seen = new Set<string>(), starts = new Set<number>();
  for (const run of runs) starts.add(run.charCodeAt(0));
  let work = 16 * literal.length + 4096, layer = [literal];
  for (let depth = 0; layer.length; depth++) {
    if (depth > catalog.limits.max_depth) return undefined;
    const next: string[] = [];
    const add = (view: string) => { if (!seen.has(view)) { if ((work -= view.length + 1) < 0) throw shownRuns; next.push(view); } };
    try {
      for (const view of layer) {
        if (seen.has(view)) continue;
        seen.add(view);
        const counts = new Map<string, number>();
        for (const part of view.split(replacement)) for (let i = 0; i + 4 <= part.length; i++) {
          // Runs of four code points: a character outside the basic plane is one code point of two code units.
          const low = part.charCodeAt(i);
          if (!starts.has(low) || low >= 0xdc00 && low <= 0xdfff && i > 0 && isHighSurrogate(part.charCodeAt(i - 1))) continue;
          let end = i;
          for (let k = 0; k < 4 && end < part.length; k++) end += isHighSurrogate(part.charCodeAt(end)) && end + 1 < part.length && isLowSurrogate(part.charCodeAt(end + 1)) ? 2 : 1;
          const run = part.slice(i, end);
          if (runs.has(run)) counts.set(run, (counts.get(run) ?? 0) + 1);
        }
        for (const [run, count] of counts) if (count > (shown.get(run) ?? 0)) shown.set(run, count);
        if (/^\s*[[{"]/.test(view)) {
          let parsed: unknown;
          try { parsed = JSON.parse(view); } catch { parsed = undefined; }
          const walk = (node: unknown, level: number): void => {
            if (level > catalog.limits.max_depth) throw shownRuns;
            if (typeof node === "string") add(node);
            else if (node !== null && typeof node === "object") for (const [key, child] of Object.entries(node)) { add(key); walk(child, level + 1); }
          };
          walk(parsed, 0);
        }
        for (const [start, end] of stringLiterals(view)) {
          let literal: unknown;
          try { literal = JSON.parse(view.slice(start, end)); } catch { continue; }
          if (typeof literal === "string") add(literal);
        }
        if (view.includes("\\")) add(view.replace(/\\(u[0-9a-fA-F]{4}|["\\/bfnrt])/g, (_, escape: string) => escape[0] === "u" ? String.fromCharCode(parseInt(escape.slice(1), 16)) : ({ b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" } as Record<string, string>)[escape] ?? escape));
        if (/%[0-9A-Fa-f]{2}/.test(view)) add(unquotePlus(view));
      }
    } catch (error) { if (error === shownRuns) return undefined; throw error; }
    layer = next;
  }
  return shown;
}
/** Whether no reading of `clean` shows a run of a known secret more often than a reading of the fallback read's output does (both within
 * their bounds). The fallback is read only where `clean` shows a run. */
function lessShown(clean: string, secrets: readonly string[], fallback: () => string): boolean {
  const runs = codePointRuns(secrets), shown = shownRuns(clean, runs);
  if (shown === undefined) return false;
  if (!shown.size) return true;
  const hidden = shownRuns(fallback(), runs);
  return hidden !== undefined && [...shown].every(([run, count]) => count <= (hidden.get(run) ?? 0));
}
/** S1's additional malformed/embedded JSON policy; recognition still delegates to S6. */
function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false, nested = false): string {
  if (typeof value !== "string" || !secretIn(value, secrets)) return embeddedText(value, secrets, depth, budget, framed, incomplete, incompleteURLs, payloadRole, nested);
  return onlyAdds(value, budget, read => embeddedText(value, secrets, depth, read!, framed, incomplete, incompleteURLs, payloadRole, nested));
}
/** Whether the catalog joins the forced spans (plainRuns) into its union, and how many it joined while `onlyAdds` reads a text. */
let forcing = true, forcedCount = 0, deciding = false;
/** Whether every character `a` shows is one `b` shows too: what is left of `a` without its placeholders is a subsequence of what is
 * left of `b` (both are the same text with parts replaced). Linear. */
function hidesAtLeast(a: string, b: string): boolean {
  const x = a.split(replacement).join(""), y = b.split(replacement).join("");
  let j = 0;
  for (let i = 0; i < x.length; i++, j++) { while (j < y.length && y[j] !== x[i]) j++; if (j >= y.length) return false; }
  return true;
}
/** The two moduli (each under 2^26, so every product stays exact in a double) and the bases of the rolling hash `windowsWithin` uses. The
 * bases are drawn when the module loads, so a text cannot be built in advance to make its windows collide with a piece. */
const PIECE_MOD = [67108859, 67108837] as const;
const PIECE_BASE = PIECE_MOD.map(mod => 65537 + Math.floor(Math.random() * (mod - 2 * 65537)));
/** `x` modulo `m`, for 0 <= x < 2^53 (no floating remainder). */
const reduce = (x: number, m: number) => { const r = x - Math.floor(x / m) * m; return r < 0 ? r + m : r >= m ? r - m : r; };
/** Whether every 4 characters together in a kept part of `a` (a part between its placeholders, PLACEHOLDERS) are 4 characters together
 * in a kept part of `b`: `a` shows no run of 4 that `b` does not show. Each window of `b` is put in a table by its keyed rolling hash (the
 * bases are drawn when the module loads), and each window of `a` is looked up there. Linear in both texts, with one 8-byte slot per
 * window of `b` (and as many again free). A hash that two different windows share counts as shown; the bases are not known in advance. */
function windowsWithin(a: string, b: string): boolean {
  const [m1, m2] = PIECE_MOD, [b1, b2] = PIECE_BASE;
  let w1 = 1, w2 = 1;
  for (let i = 1; i < 4; i++) { w1 = reduce(w1 * b1, m1); w2 = reduce(w2 * b2, m2); }
  // Calls `each` with the key (1 or more) of each window of each kept part of `text`, until it returns false.
  const windows = (text: string, each: (key: number) => boolean) => {
    for (const part of text.split(PLACEHOLDERS[0])) {
      let h1 = 0, h2 = 0;
      for (let i = 0; i < part.length; i++) {
        if (i >= 4) { const d = part.charCodeAt(i - 4); h1 = reduce(h1 + m1 * 65536 - d * w1, m1); h2 = reduce(h2 + m2 * 65536 - d * w2, m2); }
        const c = part.charCodeAt(i);
        h1 = reduce(h1 * b1 + c, m1); h2 = reduce(h2 * b2 + c, m2);
        if (i >= 3 && !each(h1 * m2 + h2 + 1)) return false;
      }
    }
    return true;
  };
  let count = 0;
  windows(b, () => { count++; return true; });
  let size = 16;
  while (size < 2 * count) size *= 2;
  const table = new Float64Array(size), mask = size - 1;
  const slot = (key: number) => { let i = (key - Math.floor(key / size) * size) & mask; while (table[i] !== 0 && table[i] !== key) i = (i + 1) & mask; return i; };
  windows(b, key => { table[slot(key)] = key; return true; });
  return windows(a, key => table[slot(key)] === key);
}
/** The first index at or after `from` where `pattern` (not empty) occurs in `text`, or -1. Knuth-Morris-Pratt: linear in the text
 * read and the pattern. */
function indexFrom(text: string, pattern: string, from: number): number {
  const border = new Int32Array(pattern.length);
  for (let i = 1, k = 0; i < pattern.length; i++) {
    while (k > 0 && pattern.charCodeAt(i) !== pattern.charCodeAt(k)) k = border[k - 1];
    if (pattern.charCodeAt(i) === pattern.charCodeAt(k)) k++;
    border[i] = k;
  }
  for (let i = from, k = 0; i < text.length; i++) {
    while (k > 0 && text.charCodeAt(i) !== pattern.charCodeAt(k)) k = border[k - 1];
    if (text.charCodeAt(i) === pattern.charCodeAt(k)) k++;
    if (k === pattern.length) return i + 1 - k;
  }
  return -1;
}
/** The last index at or before `end - pattern.length` where `pattern` (not empty) occurs in `text`, or -1: indexFrom read backwards. */
function indexBefore(text: string, pattern: string, end: number): number {
  const size = pattern.length, border = new Int32Array(size);
  // The pattern read from its last character to its first.
  for (let i = 1, k = 0; i < size; i++) {
    while (k > 0 && pattern.charCodeAt(size - 1 - i) !== pattern.charCodeAt(size - 1 - k)) k = border[k - 1];
    if (pattern.charCodeAt(size - 1 - i) === pattern.charCodeAt(size - 1 - k)) k++;
    border[i] = k;
  }
  for (let i = end - 1, k = 0; i >= 0; i--) {
    while (k > 0 && text.charCodeAt(i) !== pattern.charCodeAt(size - 1 - k)) k = border[k - 1];
    if (text.charCodeAt(i) === pattern.charCodeAt(size - 1 - k)) k++;
    if (k === size) return i;
  }
  return -1;
}
/** The placeholders a read writes: the credential placeholder and the omissions of media, data URIs and incomplete audio; and the same
 * with the quotes (or JSON-escaped quotes) a read writes around a placeholder that replaces a value to the end of the text. */
const PLACEHOLDER_SOURCE = replacement.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "|<omitted(?:: [^<>\\n]*)?>";
const PLACEHOLDERS = [new RegExp(PLACEHOLDER_SOURCE), new RegExp(`(?:\\\\?")?(?:${PLACEHOLDER_SOURCE})(?:\\\\?")?`)];
/** The same, and a placeholder written with the single quotes (or JSON-escaped double quotes) of the pair value it replaces: the forms
 * the read of the raw bytes with no known secret writes (`'<redacted>'` for `\\t\\"value\\"`). */
const SPELLED_PLACEHOLDERS = [...PLACEHOLDERS, new RegExp(`(?:\\\\?["'])?(?:${PLACEHOLDER_SOURCE})(?:\\\\?["'])?`)];
/** Which characters of `original` a read hides, where its `output` is `original` with parts (each of one or more characters) replaced
 * by placeholders (PLACEHOLDERS; where that does not match, a placeholder with the quotes around it): 1 for a hidden character.
 * Undefined where the output is not of that form (a read that wrote anything else). Each kept part is placed at its first place after
 * the previous one and, reading backwards, at its last place before the next one; every way of reading the output places it between
 * the two. A character counts as shown only where the part covers it at both places, so it is shown however the output is read; `exact`
 * where each part has one place (the mask is then what the read hid). Linear. */
function hiddenBy(original: string, output: string, forms: readonly RegExp[] = PLACEHOLDERS): { hidden: Uint8Array; exact: boolean } | undefined {
  const n = original.length;
  for (const placeholders of forms) {
    const parts = output.split(placeholders);
    if (parts.length === 1) { if (output === original) return { hidden: new Uint8Array(n), exact: true }; continue; }
    const first = placedParts(original, parts), last = first && lastPlacedParts(original, parts);
    if (!first || !last) continue;
    const hidden = new Uint8Array(n).fill(1);
    let exact = true;
    for (let k = 0; k < parts.length; k++) {
      const size = parts[k].length;
      if (!size) continue;
      if (last[k] !== first[k]) exact = false;
      hidden.fill(0, last[k], first[k] + size);
    }
    return { hidden, exact };
  }
  return undefined;
}
/** Where each kept part of a read's output stands in `original`, each at its last place before the next one (at least one hidden
 * character between two), the first at the start and the last at the end; undefined where there is no such place. */
function lastPlacedParts(original: string, parts: readonly string[]): number[] | undefined {
  const last = parts.length - 1, at: number[] = new Array(parts.length);
  if (!original.endsWith(parts[last])) return undefined;
  let start = at[last] = original.length - parts[last].length;
  for (let k = last - 1; k > 0; k--) {
    const place = parts[k] ? indexBefore(original, parts[k], start - 1) : start - 1;
    if (place < 0) return undefined;
    at[k] = start = place;
  }
  if (start < parts[0].length + 1 || !original.startsWith(parts[0])) return undefined;
  at[0] = 0;
  return at;
}
/** Where each kept part of a read's output stands in `original`, each at its first place after the previous one (at least one hidden
 * character between two), the first at the start and the last at the end; undefined where there is no such place. */
function placedParts(original: string, parts: readonly string[]): number[] | undefined {
  const last = parts.length - 1, at: number[] = [0];
  if (!original.startsWith(parts[0])) return undefined;
  let end = parts[0].length;
  for (let k = 1; k < last; k++) {
    const next = parts[k] ? indexFrom(original, parts[k], end + 1) : end + 1;
    if (next < 0 || next > original.length) return undefined;
    at.push(next); end = next + parts[k].length;
  }
  const start = original.length - parts[last].length;
  if (start < end + 1 || !original.endsWith(parts[last])) return undefined;
  at.push(start);
  return at;
}
/** Two reads of `original` that each only replace parts of it with placeholders (hiddenBy), decided by position: `first` where it hides
 * every character `second` hides and its own hidden characters are known exactly (each kept part has one place), `second` where the same
 * holds the other way, and otherwise `original` written once with the union of what either hides replaced (touching parts joined), a
 * character counted as shown by a read only where every way of reading its output shows it. So the result shows no character either read
 * hides. Undefined where a read wrote anything else. Linear. */
function positionalUnion(original: string, first: string, second: string): string | undefined {
  const bySecond = hiddenBy(original, second), byFirst = bySecond && hiddenBy(original, first);
  if (!bySecond || !byFirst) return undefined;
  const a = bySecond.hidden, b = byFirst.hidden;
  let firstMore = true, secondMore = true;
  for (let i = 0; i < original.length; i++) { if (a[i] > b[i]) firstMore = false; else if (b[i] > a[i]) secondMore = false; }
  if (firstMore && byFirst.exact) return first;
  if (secondMore && bySecond.exact) return second;
  // Neither read is known to hide all that the other hides: one rewrite of the original with what either hides replaced.
  let out = "";
  for (let i = 0, j; i < original.length; i = j) {
    const hide = a[i] | b[i];
    for (j = i + 1; j < original.length && (a[j] | b[j]) === hide; j++);
    out += hide ? replacement : original.slice(i, j);
  }
  return out;
}
/** `value` written once with every occurrence of each known secret as `value` spells it replaced, overlapping and touching occurrences
 * joined into one placeholder. One Knuth-Morris-Pratt pass per secret the text holds (one native search passes over the others) and one
 * pass to write: linear in the text and the secrets. */
function occurrencesHidden(value: string, secrets: readonly string[]): string {
  const n = value.length, cover = new Int32Array(n + 1);
  for (const secret of prepareSecrets(secrets)) {
    // A secret the text does not hold is passed over with one native search, as knownSecretText does.
    if (typeof secret !== "string" || !secret || value.indexOf(secret) < 0) continue;
    const size = secret.length, border = new Int32Array(size);
    for (let i = 1, k = 0; i < size; i++) {
      while (k > 0 && secret.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];
      if (secret.charCodeAt(i) === secret.charCodeAt(k)) k++;
      border[i] = k;
    }
    for (let i = 0, k = 0; i < n; i++) {
      while (k > 0 && value.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];
      if (value.charCodeAt(i) === secret.charCodeAt(k)) k++;
      if (k === size) { cover[i + 1 - size]++; cover[i + 1]--; k = border[k - 1]; }
    }
  }
  let out = "", depth = 0;
  for (let i = 0, j; i < n; i = j) {
    depth += cover[i];
    const hide = depth > 0;
    for (j = i + 1; j < n && (depth + cover[j] > 0) === hide; j++) depth += cover[j];
    out += hide ? replacement : value.slice(i, j);
  }
  return out;
}
/** occurrencesHidden for the joins that hide whole every registered value a quoted root holds, within a work bound: where the
 * registered values the text holds times its length pass 8 times its length plus 2^20 (the passes beyond 8 would read more than 2^20
 * characters), every character counts as
 * hidden, so the attribute alone fails closed as the quoted placeholder. One native search per registered value, then as
 * occurrencesHidden. */
function heldOccurrences(value: string, secrets: readonly string[]): string {
  let held = 0;
  for (const secret of prepareSecrets(secrets)) if (typeof secret === "string" && secret && value.indexOf(secret) >= 0 && ++held * value.length > 8 * value.length + (1 << 20)) return replacement;
  return occurrencesHidden(value, secrets);
}
/** Where each character of the string a quoted root encodes is spelled in the root (`value`, a JSON text that is one string with only JSON
 * whitespace around it): `start[i]` is the first raw index of character i and `start[i + 1]` the end of its spelling (one character, or an
 * escape of 2 or 6); `open` and `close` are the indexes of the root's quotes. Undefined where `value` is not of that form. Linear. */
function rootSpelling(value: string, text: string): { start: Int32Array; open: number; close: number } | undefined {
  let open = 0, close = value.length - 1;
  while (open < value.length && value[open] !== '"') open++;
  while (close > open && value[close] !== '"') close--;
  if (close <= open) return undefined;
  const start = new Int32Array(text.length + 1);
  let k = 0;
  for (let i = open + 1; i < close; k++) {
    if (k >= text.length) return undefined;
    start[k] = i;
    i += value[i] !== "\\" ? 1 : value[i + 1] === "u" ? 6 : 2;
  }
  if (k !== text.length) return undefined;
  start[k] = close;
  return { start, open, close };
}
/** Which characters of the string a quoted root encodes are hidden where `hidden` marks raw characters of the root (hiddenBy): those whose
 * spelling holds a marked character, so an escape a read hides any part of is hidden whole. Linear. */
function spelledHidden(spelling: { start: Int32Array }, size: number, hidden: Uint8Array): Uint8Array {
  const mask = new Uint8Array(size);
  for (let i = 0; i < size; i++) for (let r = spelling.start[i]; r < spelling.start[i + 1]; r++) if (hidden[r]) { mask[i] = 1; break; }
  return mask;
}
/** The characters 0..mask.length written once, runs `mask` marks replaced (touching ones joined) and the others written by `write`. */
function writeMasked(mask: Uint8Array, write: (from: number, to: number) => string): string {
  let out = "";
  for (let i = 0, j; i < mask.length; i = j) {
    for (j = i + 1; j < mask.length && mask[j] === mask[i]; j++);
    out += mask[i] ? replacement : write(i, j);
  }
  return out;
}
/** Reads of a quoted root decided on the string it encodes (`text`), where they cannot be placed in the root's raw bytes: each read that
 * is a JSON string is read as the string it encodes, each of `spelled` (reads that only replace parts of the raw bytes, a placeholder
 * written with the quotes of what it replaces, SPELLED_PLACEHOLDERS) by where its hidden bytes spell characters of `text`, and the union
 * of what all of them hide by position (positionalUnion) is written once as a JSON string, with the whitespace the first read writes
 * around it (the first read as it is, where it hides all the others hide). Undefined where a read is neither, or cannot be placed in
 * `text`. Linear in the root and the reads. */
function decodedUnion(value: string, text: string, reads: readonly string[], spelled: readonly string[]): string | undefined {
  let union: string | undefined, first: string | undefined;
  const join = (read: string) => { union = union === undefined ? hiddenBy(text, read) && read : positionalUnion(text, union, read); return union !== undefined; };
  for (const read of reads) {
    let node: unknown;
    try { node = JSON.parse(read); } catch { return undefined; }
    if (typeof node !== "string" || !join(node)) return undefined;
    first ??= node;
  }
  if (spelled.length) {
    const spelling = rootSpelling(value, text);
    if (!spelling) return undefined;
    for (const read of spelled) {
      const by = hiddenBy(value, read, SPELLED_PLACEHOLDERS);
      if (!by || !join(writeMasked(spelledHidden(spelling, text.length, by.hidden), (i, j) => text.slice(i, j)))) return undefined;
    }
  }
  if (union === undefined) return undefined;
  // The first read is kept as it is written where it hides all the others hide; otherwise the union is written with its whitespace.
  if (union === first) return reads[0];
  const space = /^[ \t\r\n]*/.exec(reads[0])![0], after = /[ \t\r\n]*$/.exec(reads[0])![0];
  return space + JSON.stringify(union) + after;
}
/** A union of reads of a quoted root, written on its raw bytes, kept one JSON string: where it is not one (it took the root's opening or
 * closing quote into a placeholder, or a part of an escape), it is written again with the root's quotes and the whitespace around them
 * shown, and every escape it hides any part of hidden whole. Undefined where it cannot be placed in the root. Linear. */
function rootString(value: string, text: string, union: string): string | undefined {
  let string = false;
  try { string = typeof JSON.parse(union) === "string"; } catch { /* Written again below. */ }
  const spelling = rootSpelling(value, text), by = spelling && hiddenBy(value, union);
  if (!spelling || !by) return string ? union : undefined;
  // A union that is one JSON string is kept as it is unless it hides part of an escape (the backslash of \u0050 and not its digits).
  if (string && !partEscapeHidden(spelling, text.length, by.hidden)) return union;
  const mask = spelledHidden(spelling, text.length, by.hidden), start = spelling.start;
  return value.slice(0, spelling.open + 1) + writeMasked(mask, (i, j) => value.slice(start[i], start[j])) + value.slice(spelling.close);
}
/** Whether `hidden` (raw characters of a quoted root, hiddenBy) marks some but not all of the characters of one escape's spelling. Linear. */
function partEscapeHidden(spelling: { start: Int32Array }, size: number, hidden: Uint8Array): boolean {
  for (let i = 0; i < size; i++) {
    const from = spelling.start[i], to = spelling.start[i + 1];
    if (to - from < 2) continue;
    let marked = 0;
    for (let r = from; r < to; r++) marked += hidden[r];
    if (marked && marked < to - from) return true;
  }
  return false;
}
/** A registered value only adds redaction. A text whose registered values take out the context of a shape or a pair (plainRuns) is read
 * twice: with what the shapes and the pair rule hide on the catalog's text joined into the union, and in the reference's order alone.
 * Where both reads only replace parts of the original text with placeholders (hiddenBy), what each hides is known by position: a
 * read is kept where it hides every character the other hides and its own hidden characters are known exactly (each kept part has one
 * place), and otherwise the original text is written once with the union of both (touching parts joined) replaced, a character counted
 * as shown by a read only where every way of reading its output shows it. So neither read can show a character the other hides. Where a read wrote anything else, the reference's order is kept where it hides at
 * least every character the joined read hides and shows no 4 characters together that the joined read does not show
 * (windowsWithin), the joined read where the same holds the other way, and otherwise the whole text is the placeholder. The alignment,
 * the subsequence tests and the window tests are linear in the text and both reads; they and the second read are not charged to the privacy budget. The joined read charges the budget; the other reads a copy of what is left. A budget the joined
 * read runs out of fails closed for this text alone: the text is read in the reference's order on the budget as it was (so the budget is
 * charged, and a text that order cannot read is refused, as the reference does) and the whole text becomes the placeholder; never the
 * reference's order with less hidden. A read inside another is not decided again. */
function onlyAdds(original: string, budget: PrivacyBudget | undefined, read: (budget: PrivacyBudget | undefined) => string): string {
  if (deciding) return read(budget);
  deciding = true; forcing = true; forcedCount = 0;
  try {
    // What is left of a budget held in plain fields (an accessor is not read here).
    const plain = budget && "value" in (Object.getOwnPropertyDescriptor(budget, "nodes") ?? {}) && "value" in (Object.getOwnPropertyDescriptor(budget, "text") ?? {}) ? { nodes: budget.nodes, text: budget.text } : undefined;
    let joined: string;
    try { joined = read(budget); }
    catch (error) {
      // A budget the joined read runs out of, where the reference's order may not: the text fails closed. It is read in that order
      // alone, on the budget as it was, so the budget is charged as the reference charges it (and refuses what it refuses); then the
      // whole text is the placeholder, since what the joined read would hide is not known.
      if (!forcedCount || !(error instanceof Error && /^Privacy .*budget$/.test(error.message))) throw error;
      if (plain && budget) { budget.nodes = plain.nodes; budget.text = plain.text; }
      forcing = false;
      read(budget);
      return replacement;
    }
    if (!forcedCount) return joined;
    forcing = false;
    // The budget is charged by the joined read; the reference's order is read on a copy of what is left of it.
    let ordered: string;
    // A budget the reference's order runs out of leaves the joined read, which hides more.
    try { ordered = read(budget && { nodes: budget.nodes, text: budget.text, seen: budget.seen }); }
    catch (error) { if (error instanceof Error && /^Privacy .*budget$/.test(error.message)) return joined; throw error; }
    if (ordered === joined) return ordered;
    const union = positionalUnion(original, ordered, joined);
    if (union !== undefined) return union;
    // A read that wrote more than placeholders cannot be placed in the original text, so what each hides is not known by position: a
    // read is kept only where it hides at least every character the other hides and shows no 4 characters together that the other does
    // not show (the subsequence test alone could match a character the other read hides to one it shows elsewhere). Otherwise the
    // whole text is the placeholder.
    if (hidesAtLeast(ordered, joined) && windowsWithin(ordered, joined)) return ordered;
    if (hidesAtLeast(joined, ordered) && windowsWithin(joined, ordered)) return joined;
    return replacement;
  } finally { deciding = false; forcing = true; }
}
function embeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false, nested = false): string {
  if (depth > catalog.limits.max_depth) throw new Error("Privacy structure budget");
  if (wholeSecret(value, secrets)) return replacement;
  // A whole data URI or URL owns its delimiters, including brackets and quotes.
  if (framed) { const uri = mediaReplacement(value); if (uri !== undefined) return uri; if (isURLValue(value)) return text(value, secrets, true, incomplete || incompleteURLs); }
  // The URL and data URI catalog reads the text first, as the reference does: a known secret is looked for in the catalog's output,
  // so it can neither consume a credential name, a delimiter or a userinfo '@' the pair rule needs, nor hide a data URI from
  // the catalog. A complete JSON text is read leaf by leaf instead, and each leaf goes through the same order.
  let catalogued = false;
  // The spans the catalog wrote in the text, by position, while the text is the catalog's output as edited below.
  let owned: Span[] | undefined;
  if (!nested && !wholeContainer(value)) { [value, owned] = catalogText(value, incomplete || incompleteURLs, secrets); catalogued = true; }
  // A union placeholder that holds a forced span is one the scrub wrote: the scan is not charged for it (see knownSecretRanges).
  const ranges = scanned(budget, () => knownSecretRanges(value, secrets, depth, budget, owned?.filter(span => span.forced) ?? []));
  // A text that opens with a bracket and is not one JSON document: when a known secret takes out part of a credential name, so that a
  // value the pair rule reads on the text with the secrets in place is neither a secret nor read once they are replaced, the whole
  // text is the placeholder.
  if (catalogued && ranges.length && /^\s*[\[{]/.test(value) && secretCutsName(value, ranges)) return replacement;
  // Owners are found on the text they frame; a pass that rewrites the text finds them again (once, below).
  let owners: TextOwner[] | undefined;
  const edited: SourceRange[] = [];
  if (ranges.length) {
    const found: TextOwner[] = owners = !nested && !catalogued ? scanned(budget, () => wholeOwner(value, depth, budget)) : scanned(budget, () => textOwners(value, depth, budget));
    // Where the first range the whole-text read redacts (with the known secrets replaced, as the reference reads) starts; read once, when
    // a secret inside brackets asks for it.
    let firstRead: number | undefined;
    const readBefore = nested ? undefined : (position: number) => (firstRead ??= referenceRedactions(value, ranges).reduce((low, range) => Math.min(low, range.start), Infinity)) < position;
    // Whether four characters are a run of a known secret (of any length, as the secrets were given). Fewer than four, at an end of the
    // text inside brackets, are read on with the text around the brackets, so they are a run where any secret holds them.
    let runs: ReadonlySet<string> | undefined;
    const secretRun = (run: string) => {
      if (run.length < 4 && !nested) return false;
      return (runs ??= secretRuns(secrets)).has(run);
    };
    let edits = scanned(budget, () => crossOwnerEdits(value, ranges, found, depth, budget, undefined, readBefore, secretRun));
    // An edit that is not one known secret took an owner out whole. The reference reads its pair rule over the text with only the
    // secrets replaced, so what that read redacts is taken out too: the owner's name must not be what ends a later pair's reading.
    const rawKeys = new Set(ranges.map(range => range.start + ":" + range.end));
    // A given secret that is one whole JSON object or array keeps its owner's quotes around the placeholder, where the reference reads the
    // placeholder alone: what the reference redacts on past its closing bracket (the rest of a pair's value) is taken out with the secret.
    const quoted = edits.filter(edit => edit.text !== undefined && rawKeys.has(edit.start + ":" + edit.end));
    if (!nested && (quoted.length || edits.some(edit => !rawKeys.has(edit.start + ":" + edit.end)))) {
      // Only what touches an owner or an edit can differ from what the pair rule reads later; a range inside one JSON string belongs to
      // that owner's own read.
      // What the reference redacts outside every owner, or inside one owner that is read as text, or whose own string no quote, backslash or
      // control character in the range can break, is replaced where it stands: the name that decides it may be in what an edit took out, so the owner does
      // not read it. Only a range that crosses an owner goes through the cross-owner edits.
      const outer: SourceRange[] = [], plain: SourceRange[] = [];
      const taken = edits.filter(edit => !rawKeys.has(edit.start + ":" + edit.end));
      const reads = joinRanges(referenceRedactions(value, ranges));
      for (const edit of quoted) { const range = firstEnding(reads, edit.end); if (range && range.start < edit.end) outer.push({ start: Math.max(range.start, edit.start), end: range.end }); }
      for (const range of reads) {
        // What an edit takes out changes how the quotes and names after it pair up, to the end of the text, so a range that overlaps it,
        // or follows it at any distance, can be read differently once the edit is made.
        let low = 0, high = taken.length;
        while (low < high) { const mid = (low + high) >> 1; if (taken[mid].start < range.end) low = mid + 1; else high = mid; }
        if (!low) continue;
        const owner = firstEnding(found, range.start);
        (!owner || owner.start >= range.end || owner.start <= range.start && owner.end >= range.end && (owner.kind === "tail" || owner.kind === "wrapper" || !/["\\\u0000-\u001f]/.test(value.slice(range.start, range.end))) ? plain : outer).push(range);
      }
      edits = withPlainRanges(scanned(budget, () => crossOwnerEdits(value, joinRanges([...ranges, ...outer]), found, depth, budget, { budget: workBudget(), secrets: rawKeys }, readBefore, secretRun)), plain);
    }
    const matched = replaceEdits(value, edits);
    // Where each edit's text stands in the replaced text: a secret that the neighbouring characters form with an edit is not one of
    // the text the reference reads, which replaces its known secrets once.
    if (!nested) { let shift = 0; for (const edit of edits) { const length = (edit.text ?? replacement).length; edited.push({ start: edit.start + shift, end: edit.start + shift + length }); shift += length - (edit.end - edit.start); } }
    // A nested owner's text was read by the whole-text pass with its known secrets in place, so a value shape beside one was cut
    // off by a word character; the reference replaces known secrets first, so read the shapes again on the replaced text.
    if (matched !== value) { if (owned) owned = movedSpans(owned, edits); value = nested ? credentialPairs(credentialText(matched, [])) : matched; owners = undefined; }
  }
  let whole = false;
  // A catalogued text is not one JSON document, and replacing its known secrets does not make it one: a secret in a wrapper or tail
  // that opens the text takes it out as a bare placeholder or is kept, and a JSON owner's string becomes a string, with the text
  // beside it left as it was. So only a text that was one JSON document is read as a whole container.
  if (!nested && !(whole = !catalogued && wholeContainer(value))) {
    // The credential pair rule reads the whole text once, after the URL catalog, known secrets and value shapes, before any
    // owner below rewrites a name or a value it must read. Known secrets outside every owner were replaced just above; the
    // owners replace their own (a JSON owner keeps its encoding), so this pass reads none.
    if (!catalogued) [value, owned] = catalogText(value, incomplete || incompleteURLs, secrets);
    // A known secret left to an owner is still in the text. The reference replaces every known secret before it reads a value shape or
    // a pair; when what those read differs with the secrets in place (a shape run or a pair that ends at, starts at or runs through a
    // secret, so the owner would find a secret changed or gone), the text is read in the reference's order instead.
    // The placeholders this scrub wrote are not charged by this second scan; one forward pass over the sorted edits keeps the secrets
    // that no edit touches.
    let edit = 0;
    const left = ranges.length ? scanned(budget, () => knownSecretRanges(value, secrets, depth, budget, edited)).filter(range => {
      while (edit < edited.length && edited[edit].end <= range.start) edit++;
      return !(edit < edited.length && edited[edit].start < range.end);
    }) : [];
    let ordered = false;
    const plainRead = catalogPairs(value, [], owned);
    if (left.length) {
      const view = joinRanges(referenceRedactions(value, left, true)), plain = joinRanges(referenceRedactions(value, [], true));
      // A bare media token read here is one the reference reads only after the secrets are replaced (a secret in it takes it apart).
      ordered = view.length !== plain.length || view.some((range, k) => range.start !== plain[k].start || range.end !== plain[k].end) || mediaTokens(plainRead) !== plainRead;
    }
    const paired = mediaTokens(ordered ? catalogPairs(value, secrets, owned) : plainRead);
    if (paired !== value) { value = paired; owners = undefined; }
  }
  owners ??= whole ? scanned(budget, () => wholeOwner(value, depth, budget)) : scanned(budget, () => textOwners(value, depth, budget));
  let end = 0, out = "";
  // Only a container goes one level deeper than the text that holds it; a quoted string in the text is read as text, at the text's depth.
  const below = (parsed: unknown) => parsed !== null && typeof parsed === "object" ? depth + 1 : depth;
  for (const owner of owners) {
    const raw = value.slice(owner.start, owner.end);
    out += bareGaps(value.slice(end, owner.start));
    if (owner.kind === "json") {
      // JSON inside a text that is not the value's own JSON document is text to the reference, which gives it no depth: one that reaches past
      // the depth limit is left to the reads of the whole text above, as brackets that enclose no JSON are.
      if (wholeSecret(raw, secrets)) { out += JSON.stringify(replacement); end = owner.end; continue; }
      let read = raw, parsed = owner.parsed;
      // A JSON string literal in a text is raw text to the reference, which replaces the text's known secrets there, longest first, on the
      // escaped bytes: a longer secret that only the decoded string holds must not take part of a shorter one first and leave the rest of
      // it readable. The literal is read with those secrets replaced; one whose replacement is not one JSON string is the placeholder.
      // Where the escaped bytes and the string they encode have the same known secrets replaced, the string is read as it is: a number
      // in a JSON text that the string holds is replaced by a quoted placeholder, so that text is still JSON. That read goes one level
      // into the JSON text and is bounded by a node budget of its own, as a value of its own would be: the literal is charged as one
      // node, as the reference charges a string, so its siblings are read with the same budget whatever their order. Where that read
      // reaches past the depth limit or its own node budget, the literal is read on its escaped bytes instead, with the budget as it was.
      if (typeof parsed === "string" && (read = knownSecretText(raw, secrets)) !== raw) {
        try { parsed = JSON.parse(read); } catch { parsed = undefined; }
        if (typeof parsed !== "string") { out += JSON.stringify(replacement); end = owner.end; continue; }
        if (parsed === knownSecretText(owner.parsed as string, secrets)) {
          const trial: PrivacyBudget = { nodes: catalog.limits.max_nodes, text: budget.text, seen: budget.seen };
          let clean: string | undefined;
          try { clean = scrubJSON(raw, owner.parsed, secrets, depth, trial, false, false, incomplete, incompleteURLs, payloadRole); }
          catch (error) { if (!(error instanceof Error) || error.message !== "Privacy structure budget") throw error; }
          // The read of the string it encodes replaces the known secrets of each string inside it, longest first, as that string spells
          // them; where a longer secret is spelled as given only there (it holds a quote, a backslash or the text of an escape), it can
          // take part of a shorter one that the escaped bytes hide whole. So that read is taken only where no reading of it shows a run of
          // a known secret more often than the read on the escaped bytes does, at any layer; otherwise the escaped-bytes read is taken.
          // The escaped-bytes read is made on a node budget of its own too; one that reaches past it is no fallback, so the decoded read is
          // not taken and the literal is read below as it is without one. Where the escaped-bytes read is taken, the budget is charged
          // the nodes it read.
          if (clean !== undefined) {
            const bytes: PrivacyBudget = { nodes: catalog.limits.max_nodes, text: budget.text, seen: budget.seen };
            let escaped: string | undefined, kept = false;
            const escapedRead = () => {
              try { return escaped ??= (catalogued || nested) && pastDepth(parsed, below(parsed)) ? read : scrubJSON(read, parsed, secrets, below(parsed), bytes, false, false, incomplete, incompleteURLs, payloadRole); }
              catch (error) { throw error instanceof Error && error.message === "Privacy structure budget" ? escapedRead : error; }
            };
            try { kept = lessShown(clean, secrets, escapedRead); } catch (error) { if (error !== escapedRead) throw error; }
            if (kept) { budget.text = trial.text; charge(budget, depth); out += clean; end = owner.end; continue; }
            if (escaped !== undefined) {
              budget.text = bytes.text;
              for (let nodes = catalog.limits.max_nodes - bytes.nodes; nodes > 0; nodes--) charge(budget, depth);
              out += escaped; end = owner.end; continue;
            }
          }
        }
      }
      if ((catalogued || nested) && pastDepth(parsed, below(parsed))) { out += read; end = owner.end; continue; }
      let replaced: string;
      if (!whole && read === raw && parsed !== null && typeof parsed === "object" && (replaced = knownSecretText(raw, secrets)) !== raw) {
        // A JSON object or array in a text that is not one JSON document is read leaf by leaf, each string with its known secrets replaced as it spells them; the
        // reference replaces them on the raw bytes, longest first. Where a longer secret is spelled as given only in a decoded string, it
        // can take part of a shorter one the raw bytes hide whole, so the leaf read is taken only where no reading of it shows a run of a
        // known secret more often than the read of the raw bytes with their known secrets replaced does: in that read the known secrets are
        // replaced on the raw bytes first; where those are then not one JSON text, the object is the quoted placeholder. An object whose
        // raw bytes hold no known secret has nothing the reference hides in it to keep.
        const leaves: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };
        const clean = scrubJSON(raw, parsed, secrets, below(parsed), leaves, false, false, incomplete, incompleteURLs, payloadRole);
        const rest: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };
        let fallback: string | undefined;
        const bytesRead = () => {
          if (fallback !== undefined) return fallback;
          let bytes: unknown;
          try { bytes = JSON.parse(replaced); } catch { bytes = undefined; }
          return fallback = bytes !== null && typeof bytes === "object" ? scrubJSON(replaced, bytes, secrets, below(bytes), rest, false, false, incomplete, incompleteURLs, payloadRole) : JSON.stringify(replacement);
        };
        // The fallback is read before the budget is written back, so it is charged also where the decoded read's readings passed their bound.
        const kept = lessShown(clean, secrets, bytesRead), read = kept ? clean : bytesRead();
        const chosen = kept ? leaves : rest;
        budget.nodes = chosen.nodes; budget.text = chosen.text;
        out += read;
        end = owner.end; continue;
      }
      out += scrubJSON(read, parsed, secrets, below(parsed), budget, false, false, incomplete, incompleteURLs, payloadRole);
    } else if (owner.kind === "field") {
      const readField = (source: string, key: string, keyEnd: number, valueStart: number, parsedValue: unknown, fieldBudget: PrivacyBudget) => {
        const cleanKey = text(key, secrets, false, incompleteURLs);
        const urlContext: JsonNumbers | undefined = incompleteURLs || payloadRole ? { input: new WeakMap(), output: new WeakMap(), incompleteURLs, payloadRole } : undefined;
        const encoded = scrubJSON(source.slice(valueStart), parsedValue, secrets, depth, fieldBudget, false, false, incomplete, incompleteURLs, payloadRole);
        const clean = walkValue(JSON.parse(encoded), secrets, key, depth, false, fieldBudget, noHints, true, false, incomplete, true, undefined, false, urlContext);
        return (cleanKey === key ? source.slice(0, keyEnd) : JSON.stringify(cleanKey)) + source.slice(keyEnd, valueStart)
          + (clean === parsedValue ? source.slice(valueStart) : JSON.stringify(clean));
      };
      const replaced = knownSecretText(raw, secrets);
      if (replaced === raw) { out += readField(raw, owner.key!, owner.keyEnd! - owner.start, owner.valueStart! - owner.start, owner.parsed, budget); end = owner.end; continue; }
      // Its name and value are read as the strings they encode; the reference replaces known secrets on the raw bytes, longest first. Where
      // that read shows a run of a known secret more often than the read of the pair with its known secrets replaced on its raw bytes does
      // (a longer secret spelled as given only once decoded took part of a shorter one), that read is taken; where the pair is then not one
      // name and JSON value, it is the quoted placeholder.
      const decoded: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen }, rest: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };
      const field = readField(raw, owner.key!, owner.keyEnd! - owner.start, owner.valueStart! - owner.start, owner.parsed, decoded);
      let fallback: string | undefined;
      const bytesRead = () => {
        if (fallback !== undefined) return fallback;
        const pair = /^("(?:[^"\\]|\\.)*")(\s*:\s*)([\s\S]*)$/.exec(replaced);
        let key: unknown, parsedValue: unknown;
        try { if (pair) { key = JSON.parse(pair[1]); parsedValue = JSON.parse(pair[3]); } } catch { key = undefined; }
        return fallback = pair && typeof key === "string" ? readField(replaced, key, pair[1].length, pair[1].length + pair[2].length, parsedValue, rest) : JSON.stringify(replacement);
      };
      const kept = lessShown(field, secrets, bytesRead), read = kept ? field : bytesRead(), chosen = kept ? decoded : rest;
      budget.nodes = chosen.nodes; budget.text = chosen.text;
      out += read;
    } else if (owner.kind === "wrapper") {
      // Brackets that enclose no JSON are read as text, as the reference reads them: past the depth limit the text is left to the reads made on the
      // whole text above instead of being read again one level deeper.
      out += depth + 1 > catalog.limits.max_depth ? raw : raw[0] + scrubEmbeddedText(raw.slice(1, -1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole, true) + raw.at(-1);
    } else {
      // A truncated JSON object cannot safely retain structural media context.
      const fields: [string, unknown][] = [];
      for (const [key, field] of stringPairs(raw)) {
        try { fields.push([JSON.parse(key), JSON.parse(field)]); } catch { /* malformed */ }
      }
      const hints = indexPrivacyHints(fields);
      const mediaFragment = [...hints.values()].some(h => h[0] || h[1]) || [...raw.matchAll(/"([^"\\]+)"\s*:/g)].some(m => incompleteMediaPath(m[1]));
      const propertyOnly = /^(\{\s*)("(?:\\.|[^"\\])*")(\s*:\s*)$/.exec(raw);
      const jsonTail = !propertyOnly && /^(?:\{\s*"|\[\s*(?:[\[{"\d-]|true\b|false\b|null\b))/.test(raw);
      // A truncated media fragment is omitted as incomplete audio; a truncated JSON text with no media field is omitted as what it is.
      if (incomplete || !propertyOnly && raw[0] === "{" && mediaFragment) out += "<omitted: incomplete audio>";
      else if (jsonTail) out += "<omitted: incomplete JSON>";
      else if (propertyOnly) {
        let key: unknown;
        try { key = JSON.parse(propertyOnly[2]); } catch { out += text(raw, secrets, false, incomplete); end = owner.end; continue; }
        const name = propertyOnly[2], replaced = knownSecretText(name, secrets);
        if (replaced === name) {
          const clean = scrubValue(key, secrets, "", depth + 1, false, budget, noHints, true, false, incomplete);
          out += clean === key ? raw : propertyOnly[1] + JSON.stringify(clean) + propertyOnly[3];
          end = owner.end; continue;
        }
        // The name is read as the string it encodes; the reference replaces known secrets on the raw bytes, longest first. Where that read
        // shows a run of a known secret more often than the read of the name with its known secrets replaced on its raw bytes does, that
        // read is taken; where the name is then not one JSON string, it is the quoted placeholder.
        const decoded: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen }, rest: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };
        const readName = (source: string, parsedName: unknown, nameBudget: PrivacyBudget) => {
          const clean = scrubValue(parsedName, secrets, "", depth + 1, false, nameBudget, noHints, true, false, incomplete);
          return clean === parsedName ? source : JSON.stringify(clean);
        };
        const field = readName(name, key, decoded);
        let fallback: string | undefined;
        const bytesRead = () => {
          if (fallback !== undefined) return fallback;
          let parsedName: unknown;
          try { parsedName = JSON.parse(replaced); } catch { parsedName = undefined; }
          return fallback = typeof parsedName === "string" ? readName(replaced, parsedName, rest) : JSON.stringify(replacement);
        };
        const kept = lessShown(field, secrets, bytesRead), read = kept ? field : bytesRead(), chosen = kept ? decoded : rest;
        budget.nodes = chosen.nodes; budget.text = chosen.text;
        out += read === name ? raw : propertyOnly[1] + read + propertyOnly[3];
      } else out += depth + 1 > catalog.limits.max_depth ? raw : raw[0] + scrubEmbeddedText(raw.slice(1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole, true);
    }
    end = owner.end;
  }
  // Only unowned gaps use the prose policy. Repaired JSON/quoted layers must
  // never return through URL or bare-media scanning as one blob.
  out += bareGaps(value.slice(end));
  const result = whole && /^\s*[\[{]/.test(value) ? structuredText(out, {}, secrets, framed, incomplete || incompleteURLs) : out;
  // A secret that sat inside an owner was left to it; when a read of the text changed that owner, the secret is read again here.
  return ranges.length && !nested && !whole ? knownSecretText(result, secrets) : result;
}
/** Free-text entry point: unlike scalar values, data: prefixes do not own trailing prose. */
export function scrubText(value: string, secrets: readonly string[] = [], depth = 0): string {
  const budget = newBudget(); charge(budget, depth, value.length);
  return pathBudgeted(() => {
    const opening = jsonTextStart(value);
    // A JSON text cut off at a quoted member's colon follows the value rule, not the prose fragment rule.
    if (value[opening] === "{" || value[opening] === "[") {
      let end = value.length;
      while (end > opening && pySpace(value[end - 1])) end--;
      if (value[end - 1] === ":") {
        end--;
        while (end > opening && JSON_WHITESPACE.has(value[end - 1])) end--;
        if (value[end - 1] === '"' && unreadableJSON(value, opening, secrets, depth, budget, { media: false, audio: false }, false)) return replacement;
      }
    }
    return scrubEmbeddedText(value, secrets, depth, budget, false);
  });
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
  const read = (source: string, node: unknown) => structuredText(scrubJSON(source, node, secrets, 0, newBudget(), false, false, false, incompleteURLs), node, secrets, true, false);
  const replaced = typeof parsed === "string" ? knownSecretText(value, secrets) : value;
  // The read of the string with no known secret: the URL catalog, the value shapes and the pair rule on the original text.
  const rulesRead = () => structuredText(scrubJSON(value, parsed, [], 0, newBudget(), false, false, false, incompleteURLs), parsed, [], true, false);
  if (replaced === value) {
    // A known secret spelled only as the string spells it (the raw bytes write it with escapes) can take part of a name or a shape there,
    // so the pair rule or a shape no longer reads what follows. It only adds redaction: the read of the string is joined, as the string it
    // encodes, with the read with no known secret and with every occurrence of a known secret in the string.
    const clean = read(value, parsed);
    if (typeof parsed !== "string" || clean === JSON.stringify(replacement)) return clean;
    // A quoted root with no known secret in its string is still joined with the read of its raw bytes with no known secret (withValueRead):
    // an escaped quoted pair, a key block whose end line is spelled with escapes, and a shape the raw bytes end are hidden as there.
    if (!secretIn(parsed, secrets)) return failsClosed(() => withValueRead(value, parsed, secrets, clean, incompleteURLs));
    return failsClosed(() => {
      const reads = [clean, rulesRead(), JSON.stringify(heldOccurrences(parsed, secrets))];
      return withValueRead(value, parsed, secrets, decodedUnion(value, parsed, reads, []) ?? keptWithin(reads[0], reads.slice(1), parsed, value), incompleteURLs);
    });
  }
  // Every read of the root runs on its own budget; where one runs out, this attribute alone is the quoted placeholder (failsClosed).
  return withValueRead(value, parsed as string, secrets, failsClosed(() => quotedRoot(value, parsed as string, secrets, incompleteURLs, read, rulesRead)), incompleteURLs);
}
/** `join()`, the union of a quoted root's reads with a read it adds; where that read runs out of a privacy budget, the attribute alone fails
 * closed: it is the quoted placeholder (never the read with less hidden, and no other field or call is affected). */
function failsClosed(join: () => string): string {
  try { return join(); } catch (error) { if (error instanceof Error && /^Privacy .*budget$/.test(error.message)) return JSON.stringify(replacement); throw error; }
}
/** A read of a quoted root as the string it encodes (the read as it is where it is not one JSON string). */
function decodedRead(read: string): string {
  try { const node: unknown = JSON.parse(read); return typeof node === "string" ? node : read; } catch { return read; }
}
/** Quotes, backslashes and JSON whitespace: the characters a read written again as JSON can move. */
const QUOTING = /["\\ \t\r\n]/g;
/** `kept`, a read of a quoted root that cannot be joined by position with the reads `dropped` (each read of the string the root encodes,
 * `text`, or of its raw bytes, `value`). A dropped read that hides nothing is passed over. Where a dropped read can be placed in `text`
 * or in `value` (hiddenBy, also with a placeholder written with the quotes of the pair value it replaces), the runs it hides are known
 * by position: `kept`, read as the string it encodes, must show no 4-character window that lies wholly inside one hidden run (as spelled
 * there or with its JSON escapes read) unless the dropped read shows that window elsewhere (by keyed rolling hash, windowKeys). A window
 * that runs across the edge of a hidden run into the text around it is not checked. Where a hidden run is under 4 characters (it has no
 * window of its own), `kept` must also show no character the dropped read does not show (with its JSON escapes as written or read),
 * quotes, backslashes and JSON whitespace aside (hidesAtLeast: a subsequence test, not by position). Where it cannot be placed, `kept` must hide at least
 * every character it hides (hidesAtLeast) and show no 4 characters together that it does not show (windowsWithin). Otherwise the
 * quoted placeholder: no read is kept that shows what another read hides. Linear in the texts (one window table per read). */
function keptWithin(kept: string, dropped: readonly string[], text: string, value: string): string {
  const k = decodedRead(kept), placeholder = JSON.stringify(replacement);
  for (const read of dropped) {
    const d = decodedRead(read);
    if (!PLACEHOLDERS[0].test(d)) continue;
    // Placed as it is written, or with a placeholder written with the quotes of the pair value it replaces (SPELLED_PLACEHOLDERS).
    const place = (source: string, output: string) => hiddenBy(source, output) ?? hiddenBy(source, output, SPELLED_PLACEHOLDERS);
    const byText = place(text, d), by = byText ?? place(value, read), source = byText ? text : value;
    if (!by) { if (!hidesAtLeast(k, d) || !windowsWithin(k, d)) return placeholder; continue; }
    // What the dropped read shows, as written and with its JSON escapes read (the kept read is read as the string it encodes).
    const shownRead = byText ? d : read;
    // The runs the dropped read hides, as spelled in the source and with their JSON escapes read.
    let runs = "", short = false;
    for (let i = 0, j; i < source.length; i = j) {
      for (j = i + 1; j < source.length && by.hidden[j] === by.hidden[i]; j++);
      if (by.hidden[i]) { runs += replacement + source.slice(i, j) + replacement + unescapedRun(source.slice(i, j)); short ||= j - i < 4; }
    }
    // A run under 4 characters has no window of its own: the kept read must then show no character the dropped read does not show
    // (hidesAtLeast, a subsequence test), quotes, backslashes and JSON whitespace aside (a read written again moves its quoting), with
    // the dropped read's JSON escapes as written or read (a read written again writes \u0071 as q).
    if (short) {
      const shown = k.replace(QUOTING, "");
      if (!hidesAtLeast(shown, d.replace(QUOTING, "")) && !hidesAtLeast(shown, unescapedRun(d).replace(QUOTING, ""))) return placeholder;
    }
    // The windows of the hidden runs that the kept read shows; each must be shown by the dropped read too. The tables hold only the
    // hidden runs' windows and those of them the kept read shows; the kept and the dropped read are each read once in order.
    const hidden = keyTable([runs]), shown = keyTable([k], key => hidden.has(key));
    if (shown.empty) continue;
    for (const t of [shownRead, unescapedRun(shownRead)]) windowKeys(t, key => { shown.mark(key); return true; });
    if (!shown.allMarked()) return placeholder;
  }
  return kept;
}
/** The set of the window keys (windowKeys) of `texts` that `keep` keeps: one open-addressing table of 8-byte slots (at least twice as many
 * as keys; the texts are read twice, to count and to fill) and one mark per slot. A key two different windows share counts as present;
 * the bases are drawn when the module loads. Linear in the texts. */
function keyTable(texts: readonly string[], keep: (key: number) => boolean = () => true): { empty: boolean; has(key: number): boolean; mark(key: number): void; allMarked(): boolean } {
  let count = 0, size = 16;
  for (const text of texts) windowKeys(text, key => { if (keep(key)) count++; return true; });
  while (size < 2 * count) size *= 2;
  const table = new Float64Array(size), marks = new Uint8Array(size), mask = size - 1;
  const slot = (key: number) => { let i = (key - Math.floor(key / size) * size) & mask; while (table[i] !== 0 && table[i] !== key) i = (i + 1) & mask; return i; };
  for (const text of texts) windowKeys(text, key => { if (keep(key)) table[slot(key)] = key; return true; });
  return {
    empty: count === 0,
    has: key => table[slot(key)] === key,
    mark: key => { const i = slot(key); if (table[i] === key) marks[i] = 1; },
    allMarked: () => { for (let i = 0; i < size; i++) if (table[i] !== 0 && !marks[i]) return false; return true; },
  };
}
/** Calls `each` with the keyed rolling-hash key (as windowsWithin keys them; 1 or more) of every 4 characters together in each part of
 * `text` between its placeholders, until it returns false; false then. Linear. */
function windowKeys(text: string, each: (key: number) => boolean): boolean {
  const [m1, m2] = PIECE_MOD, [b1, b2] = PIECE_BASE;
  let w1 = 1, w2 = 1;
  for (let i = 1; i < 4; i++) { w1 = reduce(w1 * b1, m1); w2 = reduce(w2 * b2, m2); }
  for (const part of text.split(PLACEHOLDERS[0])) {
    let h1 = 0, h2 = 0;
    for (let i = 0; i < part.length; i++) {
      if (i >= 4) { const d = part.charCodeAt(i - 4); h1 = reduce(h1 + m1 * 65536 - d * w1, m1); h2 = reduce(h2 + m2 * 65536 - d * w2, m2); }
      const c = part.charCodeAt(i);
      h1 = reduce(h1 * b1 + c, m1); h2 = reduce(h2 * b2 + c, m2);
      if (i >= 3 && !each(h1 * m2 + h2 + 1)) return false;
    }
  }
  return true;
}
/** `run` with its JSON escapes (\uXXXX, \" and the others) read; an escape cut by the run's end is left as it is. */
function unescapedRun(run: string): string {
  return run.replace(/\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g, (_, hex: string | undefined, c: string | undefined) => hex ? String.fromCharCode(parseInt(hex, 16)) : ({ b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" } as Record<string, string>)[c!] ?? c!);
}
/** The read of a quoted root (a JSON text that is one string) whose raw bytes hold a known secret: the read of the string it encodes and
 * the read of the raw bytes in the reference's order, decided and joined as below. */
function quotedRoot(value: string, parsed: string, secrets: readonly string[], incompleteURLs: boolean, read: (source: string, node: unknown) => string, rulesRead: () => string): string {
  // A quoted root is read as the string it encodes; the reference reads the raw bytes in its own order: the URL catalog first, then the
  // known secrets, longest first, on the catalog's output. Where the read of the string shows a run of a known secret more often than
  // that read of the raw bytes does, the raw-bytes read is taken; where the text is then not one JSON string, it is the quoted
  // placeholder. A known secret is never replaced on the raw bytes before the catalog reads them: it could take out the scheme, the
  // userinfo '@' or the name the catalog needs, and leave the credential the catalog hides.
  const clean = read(value, parsed);
  let fallback: string | undefined;
  const bytesRead = () => {
    if (fallback !== undefined) return fallback;
    const bytes = knownSecretText(catalogText(value, incompleteURLs, [])[0], secrets);
    let node: unknown;
    try { node = JSON.parse(bytes); } catch { node = undefined; }
    return fallback = typeof node === "string" ? read(bytes, node) : JSON.stringify(replacement);
  };
  // A known secret replaced on the raw bytes can take part of a credential's name, so the pair rule no longer reads a pair that the read of
  // the string hides; and the raw-bytes read can hide what follows a credential (the rest of a URL fragment, a flag's value) that the read
  // of the string shows. Neither read is kept with less hidden than that: the read picked here is kept with, by position
  // (positionalUnion), what the other read hides added where the read of the string is kept; and where the raw-bytes read is kept, what
  // the catalog, the value shapes and the pair rule hide on the string with no known secret (a known secret only adds redaction), and every
  // occurrence of a known secret as the raw bytes spell it (one replaced first can leave part of an overlapping one shown).
  const kept = lessShown(clean, secrets, bytesRead), bytes = bytesRead();
  if (bytes === clean) return clean;
  // A union written on the raw bytes is kept one JSON string (rootString). Where a read cannot be placed in the raw bytes (the read of the
  // string writes its escapes again as JSON writes them), the reads are joined as the strings they encode (decodedUnion), with the read with
  // no known secret and every occurrence of a known secret as the string and as the raw bytes spell it. Where that cannot be placed either:
  // the read of the string is kept as it is (it shows no run of a known secret more often); the raw-bytes read only where, both read as the
  // strings they encode, it hides at least every character the read with no known secret hides and shows no 4 characters together that it
  // does not show; otherwise the text is the quoted placeholder. A raw-bytes read that is the quoted placeholder (the raw bytes are not one
  // JSON string once the known secrets are replaced) adds nothing to a kept read of the string.
  const text = parsed as string, placeholder = JSON.stringify(replacement);
  if (kept) {
    // Every occurrence of a known secret as the raw bytes spell it is hidden whole as well: the catalog can cut one that runs from URL text
    // into a credential, so the raw-bytes read replaces only the part the catalog left.
    const placed = bytes === placeholder ? undefined : positionalUnion(value, clean, bytes);
    const union = placed === undefined ? undefined : positionalUnion(value, placed, heldOccurrences(value, secrets));
    if (union !== undefined) return rootString(value, text, union) ?? placeholder;
    // A read that cannot be placed in the raw bytes: the reads are joined as the strings they encode, with the read with no known secret
    // and every occurrence of a known secret as the string and as the raw bytes spell it.
    return failsClosed(() => {
      const reads = [clean, rulesRead(), JSON.stringify(heldOccurrences(text, secrets))], all = bytes === placeholder ? reads : [...reads, bytes];
      return decodedUnion(value, text, all, [heldOccurrences(value, secrets)]) ?? keptWithin(clean, all.slice(1), text, value);
    });
  }
  const rules = rulesRead();
  const joined = positionalUnion(value, bytes, rules);
  const union = joined === undefined ? undefined : positionalUnion(value, joined, heldOccurrences(value, secrets));
  if (union !== undefined) return rootString(value, text, union) ?? placeholder;
  const spelledUnion = bytes === placeholder ? undefined : decodedUnion(value, text, [bytes, rules, JSON.stringify(heldOccurrences(text, secrets))], [heldOccurrences(value, secrets)]);
  if (spelledUnion !== undefined) return spelledUnion;
  const b = decodedRead(bytes), r = decodedRead(rules);
  return hidesAtLeast(b, r) && windowsWithin(b, r) ? bytes : JSON.stringify(replacement);
}
/** `result`, a read of a quoted root, joined by position with `read`, a read of its raw bytes: on the raw bytes where both can be placed
 * there (kept one JSON string, rootString), otherwise as the strings they encode (decodedUnion); where neither can be made, `result`. */
function joinRead(value: string, text: string, result: string, read: string): string {
  const placed = positionalUnion(value, result, read);
  if (placed !== undefined) return rootString(value, text, placed) ?? JSON.stringify(replacement);
  // `scrubValue` writes a JSON string it reads again as JSON writes it: then it is joined as the string it encodes.
  return decodedUnion(value, text, [result], [read]) ?? decodedUnion(value, text, [result, read], []) ?? keptWithin(result, [read], text, value);
}
/** Whether `read` hides, beyond what `result` hides, only quotes, backslashes and JSON whitespace of the raw bytes (a pair value's escaped
 * quotes), so joining it adds no character of a value. False where either cannot be placed in `value`. Linear. */
function addsOnlyQuoting(value: string, result: string, read: string): boolean {
  const byRead = hiddenBy(value, read), byResult = byRead && hiddenBy(value, result);
  // What `result` hides is known only where each of its kept parts has one place: a part that stands at two places counts as hidden at both.
  if (!byRead || !byResult || !byResult.exact) return false;
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (byRead.hidden[i] && !byResult.hidden[i] && c !== 34 && c !== 92 && c !== 32 && c !== 9 && c !== 13 && c !== 10) return false;
  }
  return true;
}
/** A known secret only adds redaction to a quoted root (a JSON text that is one string) that holds one: the root never shows a character
 * that the read of its raw bytes with no known secret (`scrubValue` with none) hides. That read reads the raw bytes as the reference does:
 * a pair whose value is written with escaped quotes runs to the end of the text, a key block whose end line is spelled with escapes runs
 * to the end, and a shape ends where the raw bytes end it; a known secret that the read of the string replaced first can no longer take
 * the name or the shape that read needs. `result`, the root's read, is joined with it by position: on the raw bytes where both can be
 * placed there (kept one JSON string, rootString), otherwise as the strings they encode (decodedUnion). Where that read refuses the text
 * (a budget it runs out of), the attribute is the quoted placeholder; where neither union can be made, `result` is kept. */
function withValueRead(value: string, text: string, secrets: readonly string[], result: string, incompleteURLs = false): string {
  const placeholder = JSON.stringify(replacement);
  if (result === placeholder || result === replacement) return result;
  let byValue: unknown;
  try { byValue = scrubValue(value, []); } catch { return placeholder; }
  if (typeof byValue === "string" && byValue !== value) result = joinRead(value, text, result, byValue);
  if (!secrets.length || result === placeholder) return result;
  // The raw bytes read in the reference's order, as text: the URL catalog first, then the known secrets on its output (longest first),
  // then the value shapes and the pair rule on that text (referenceRedactions). A known secret that ends inside a pair value's escaped
  // closing quote leaves that value running to the end of the text there; several known secrets that each take part of a shape's or a
  // pair's neighbourhood can leave the read of the string showing what that read hides. It is joined by position too; a budget it runs
  // out of makes this attribute alone the quoted placeholder (failsClosed).
  return failsClosed(() => {
    const catalogued = catalogText(value, incompleteURLs, [])[0];
    // Only the registered values the catalog's text holds are located (one search each); absent ones are not charged.
    const held = secrets.filter(secret => typeof secret === "string" && catalogued.indexOf(secret) >= 0);
    const known = held.length ? knownSecretRanges(catalogued, held, 0, newBudget()) : [];
    const read = known.length ? replaceEdits(catalogued, joinRanges([...known, ...referenceRedactions(catalogued, known)])) : catalogued;
    const joined = read === value || addsOnlyQuoting(value, result, read) ? result : joinRead(value, text, result, read);
    // A registered value the raw bytes hold is hidden whole: where the catalog cuts one that runs from URL text into a credential, the
    // reads above replace only the part the catalog left, so every occurrence as the raw bytes spell it is joined by position too.
    if (joined === placeholder) return joined;
    const spelled = heldOccurrences(value, secrets);
    return spelled === value ? joined : joinRead(value, text, joined, spelled);
  });
}

/** S5 harvesting over already-parsed data, sharing exactly the export predicate.
 * No body reads, user conversions or per-trace credential groups. Exhaustion throws.
 */
export function discoverSecrets(sources: Iterable<unknown>, entries = dataEntries, inspect?: (characters: number) => void): string[] {
  return discover(sources, entries, undefined, newBudget(), inspect);
}
function discover(sources: Iterable<unknown>, entries = dataEntries, tokenParents?: WeakSet<object>, budget: PrivacyBudget = newBudget(), inspect?: (characters: number) => void): string[] {
  const found = new Set<string>(), seen = new Set<object>();
  const add = (value: string) => { if (!longEnoughSecret(value) || value === replacement) return; if (found.size >= 1024 && !found.has(value)) throw new Error("Privacy credential budget"); found.add(value); };
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
  return [...values].filter(value => longEnoughSecret(value) && value !== replacement).sort((a, b) => b.length - a.length);
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
