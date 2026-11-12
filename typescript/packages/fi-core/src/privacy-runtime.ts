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
const valueShapes = catalog.credentials.value_shapes.map(item => new RegExp(item.regex, item.flags + "g"));
const signed = new Set(catalog.signed_url.parameters.map(normalizePrivacyName));
const excluded = (folded: string) => exclusions.some(({ regex, guards }) => regex.test(folded) && !guards.some(marker => folded.includes(marker)));
/** The ONE credential-name predicate. Query mode adds signed-URL parameters.
 * S5 environment seeding intentionally includes metric/identifier names: its
 * initialization contract protects every credential-marked environment value.
 */
export function isCredentialName(name: string, query: boolean | "environment" = false): boolean {
  const folded = normalizePrivacyName(name);
  if (query && query !== "environment" && signed.has(folded)) return true;
  // Most prose assignments and attribute names contain no credential marker.
  // Reject those before path splitting/exclusions; this is only a negative gate.
  if (!patterns.some(pattern => folded.includes(pattern))) return false;
  if (query === "environment") return true;
  const parts = components(name).map(normalizePrivacyName);
  let exempt = false;
  for (let i = 0; i < parts.length; i++) {
    if (excluded(parts[i]) || contextual.some(regex => regex.test(parts.slice(0, i + 1).join(".")))) exempt = true;
    else if (patterns.some(pattern => parts[i].includes(pattern))) return true;
  }
  return !exempt && !excluded(folded);
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
const DATA = String.raw`(?<![A-Za-z0-9+.\-_:])data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*`;
const URL_BODY = String.raw`(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\s"<>\\])+`;
const URL_TOKEN = String.raw`(?:https?://|(?<!\S)[/?])${URL_BODY}`;
// Fragment-only references own their delimiters without becoming query URLs.
const ASSIGNMENT_URL_TOKEN = String.raw`(?:${URL_TOKEN}|(?<!\S)#${URL_BODY})`;
const URL_WHOLE = new RegExp(`^(?:${URL_TOKEN})$`, "i");
const TEXT = new RegExp(`(${URL_TOKEN})|(${DATA})`, "gi");
const DISCOVERY_URLS = new RegExp(URL_TOKEN, "gi");
const dataText = (value: string, framed = false): string => (framed ? dataURI(value) : undefined) ?? value.replace(new RegExp(DATA, "gi"), original => dataURI(original) ?? original);

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
  const hints = new Map(previous);
  for (const [name, value] of items) {
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
  const { base, question, pairs, hash, fragment } = queryParts(original);
  // A truncated query may end in a syntactically valid MIME prefix. Without
  // complete context no query MIME can attest to a non-media value.
  const hints = indexPrivacyHints(pairs.filter(pair => pair.equals).map(pair => [pair.decodedName, pair.decodedValue]), "", noHints, incomplete ? 0 : Infinity);
  const query = pairs.map(pair => {
    let value = pair.value;
    if (pair.equals) {
      if (isCredentialName(pair.decodedName, true)) value = replacement;
      else {
        let clean = mediaReplacement(pair.decodedValue, pair.decodedName, hints, true, false, false, incomplete);
        if (clean === undefined) { const text = dataText(pair.decodedValue, true); if (text !== pair.decodedValue) clean = text; }
        if (clean !== undefined) value = clean;
      }
    }
    return pair.name + pair.equals + value;
  }).join("&");
  return dataText(base.replace(/^(https?:\/\/)[^\s/@]+:[^\s/@]+@/i, `$1${replacement}@`)) + question + query + hash + dataText(fragment);
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
  for (const shape of valueShapes) value = value.replace(shape, replacement);
  return value;
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
function text(value: string, secrets: readonly string[], framed: boolean, incomplete = false): string {
  // This function only receives prose gaps, names or URL-owned values. Match
  // original credentials before URL query/userinfo rewrites can split them.
  value = knownSecretText(value, secrets);
  if (framed && isURLValue(value)) return credentialText(urlValue(value, incomplete), secrets);
  let end = 0, result = "";
  // matchAll clones its matcher; recursion never shares an active exec cursor.
  TEXT.lastIndex = 0;
  for (const match of value.matchAll(typeof value === "string" ? TEXT : new RegExp(TEXT.source, TEXT.flags))) {
    const start = match.index!, original = match[0];
    result += bareMedia(credentialText(value.slice(end, start), secrets));
    if (match[1]) {
      const closing = start > 0 && value[start - 1] === "'" && original.endsWith("'") ? "'" : "";
      const token = closing ? original.slice(0, -1) : original;
      // A complete bare JPEG/MP3 can also begin with '/'. Check the WHOLE
      // framed token, never a slash-prefixed substring of an authority/path.
      result += (mediaReplacement(token) ?? urlValue(token, incomplete)) + closing;
    } else result += dataText(original);
    end = start + original.length;
  }
  result += bareMedia(credentialText(value.slice(end), secrets));
  // Known values/shapes still apply to URI components and across text gaps.
  // Do not run bare-media recognition again over the assembled syntax.
  return credentialText(result, secrets);
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
          hints = indexPrivacyHints(items, path, hints);
          sourceHints = indexPrivacyHints(items, path, sourceHints);
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
type IncompleteContentKind = "audio" | "tool arguments";
const incompleteContentLabel = (kind: IncompleteContentKind): string => `<omitted: incomplete ${kind}>`;
function isToolArgumentPath(path: string): boolean {
  const parts = pathParts(path).map(normalizePrivacyName);
  return /(?:^|\.)toolcalls\.\d+\.(?:function\.)?(?:arguments|input)$/.test(parts.join("."))
    || /(?:^|\.)functioncall\.arguments$/.test(parts.join("."));
}

/** Walk only plain, already-materialized data. The Node S1 boundary rejects proxies before calling. */
export function scrubValue(value: any, secrets: readonly string[] = [], path = "", depth = 0, inheritedAudio = false, budget: PrivacyBudget = newBudget(), hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, incomplete = false, ambiguousBase64 = true): any {
  return walkValue(value, secrets, path, depth, inheritedAudio, budget, hints, framed, inheritedMedia, incomplete, ambiguousBase64);
}
function walkValue(value: any, secrets: readonly string[], path: string, depth: number, inheritedAudio: boolean, budget: PrivacyBudget, hints: PrivacyHints, framed: boolean, inheritedMedia: boolean, incomplete: boolean, ambiguousBase64: boolean, policy?: ResponsePolicy, exemptToken = false, numbers?: JsonNumbers, numericLexeme?: string): any {
  charge(budget, depth);
  if (isCredentialName(path) && !exemptToken) return replacement;
  if (value && typeof value === "object") {
    if (budget.seen.has(value)) return "<omitted>";
    budget.seen.add(value);
    if (!Array.isArray(value)) {
      if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return typeof value;
      const items = dataEntries(value);
      if (items.length > budget.nodes) throw new Error("Privacy structure budget");
      budget.text -= items.reduce((n, [name]) => n + name.length, 0); if (budget.text < 0) throw new Error("Privacy text budget");
      hints = indexPrivacyHints(items, path, hints);
      const token = policy?.tokenParents.has(value) ? own(value, "token") : undefined;
      const cleanToken = typeof token === "string"
        ? walkValue(token, secrets, "", depth + 1, false, budget, noHints, true, false, incomplete, ambiguousBase64) : undefined;
      const maskedBytes = typeof token === "string" && (cleanToken !== token || policy!.textPolicy(cleanToken) !== token);
      const numericOutput = new Map<string, string>();
      const result = Object.fromEntries(items.map(([name, item]) => {
        // Token bytes are another encoding of the same value, not harmless metadata.
        const masked = name === "logprobs" && policy?.maskedChoices.has(value) || policy?.maskedText.get(value)?.has(name) || name === "bytes" && maskedBytes;
        const exempt = name === "token" && typeof item === "string" && !!policy?.tokenParents.has(value);
        const cleanName = text(name, secrets, false, numbers?.incompleteURLs), lexeme = numbers?.input.get(value)?.get(name);
        const clean = masked ? replacement : walkValue(item, secrets, path ? `${path}.${name}` : name, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, exempt, numbers, lexeme);
        // A redacted key can collide with another key. Match Object.fromEntries'
        // last-value-wins result, not the numeric origin of an overwritten value.
        numericOutput.delete(cleanName);
        if (typeof clean === "number" && lexeme !== undefined) numericOutput.set(cleanName, lexeme);
        return [cleanName, clean];
      }));
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
    if (isToolArgumentPath(path)) {
      const label = incompleteContentLabel("tool arguments");
      // Admission can run at both write and export time. Keep authored omission
      // markers stable, and never export an unparsed argument fragment.
      if (value === label || value === replacement || value === "__REDACTED__") return value;
      try { JSON.parse(value); } catch { return label; }
    }
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
        return scrubJSON(value, parsed, secrets, depth + 1, budget, ctx.audio, ctx.media, incomplete, numbers?.incompleteURLs, payloadRole);
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
/** Text assignments use the same catalog predicate as structured fields. */
function scrubAssignments(value: string, owners: readonly TextOwner[] = []): string {
  const assignment = /\b([A-Za-z_][A-Za-z0-9_.-]*)(["']?)\s*[:=]\s*/g;
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
  for (let match: RegExpExecArray | null; (match = assignment.exec(value));) {
    while (urlIndex < urls.length && urls[urlIndex][1] <= match.index) urlIndex++;
    if (urlIndex < urls.length && urls[urlIndex][0] <= match.index) continue;
    const name = match[1];
    let credential = names.get(name);
    if (credential === undefined) {
      credential = isCredentialName(name);
      if (names.size < 256 && name.length <= 256) names.set(name, credential);
    }
    if (!credential) continue;
    const start = assignment.lastIndex;
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
    assignment.lastIndex = end;
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
function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false): string {
  if (depth > catalog.limits.max_depth) throw new Error("Privacy structure budget");
  if (wholeSecret(value, secrets)) return replacement;
  const ranges = knownSecretRanges(value, secrets, depth, budget);
  // A whole data URI or URL owns its delimiters, including brackets and quotes.
  if (framed) { const uri = mediaReplacement(value); if (uri !== undefined) return uri; if (isURLValue(value)) return text(value, secrets, true, incomplete || incompleteURLs); }
  let owners = textOwners(value, depth, budget);
  const matched = redactCrossOwnerSecrets(value, ranges, owners, depth, budget);
  if (matched !== value) { value = matched; owners = textOwners(value, depth, budget); }
  const assigned = scrubAssignments(value, owners);
  if (assigned !== value) { value = assigned; owners = textOwners(value, depth, budget); }
  let end = 0, out = "";
  for (const owner of owners) {
    const raw = value.slice(owner.start, owner.end);
    out += text(value.slice(end, owner.start), secrets, false, incomplete || incompleteURLs);
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
      out += raw[0] + scrubEmbeddedText(raw.slice(1, -1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole) + raw.at(-1);
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
      } else out += raw[0] + scrubEmbeddedText(raw.slice(1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole);
    }
    end = owner.end;
  }
  // Only unowned gaps use the prose policy. Repaired JSON/quoted layers must
  // never return through assignment, URL or bare-media scanning as one blob.
  return out + text(value.slice(end), secrets, false, incomplete || incompleteURLs);
}
/** Free-text entry point: unlike scalar values, data: prefixes do not own trailing prose. */
export function scrubText(value: string, secrets: readonly string[] = [], depth = 0): string {
  const budget = newBudget(); charge(budget, depth, value.length);
  return scrubEmbeddedText(value, secrets, depth, budget, false);
}
export function scrubAttribute(value: string, secrets: readonly string[] = [], incompleteURLs = false): string {
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
  return scrubJSON(value, parsed, secrets, 0, newBudget(), false, false, false, incompleteURLs);
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
/** @internal FITracer typed-context handoff only; never infer from attributes. */
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
