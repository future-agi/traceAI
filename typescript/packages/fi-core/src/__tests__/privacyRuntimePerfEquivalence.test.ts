import { describe, expect, test } from "@jest/globals";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as runtime from "../privacy-runtime";

// Verbatim oracle functions from 15c96fdcc12af3eba0a6c4859930f34cc7781a0f.
// Evaluate them in their original module scope so scrubAttribute uses the OLD
// normalizePrivacyName and knownSecretText even on indirect/recursive calls.
// These are literal source copies, not a second implementation of the policy.
const ORIGINAL_NORMALIZE = String.raw`export const normalizePrivacyName = (name: string): string => name.replace(/[A-Z]/g, c => c.toLowerCase()).replace(/[-_.]/g, "");`;
const ORIGINAL_KNOWN_SECRET_TEXT = String.raw`function knownSecretText(value: string, secrets: readonly string[]): string {
  for (const secret of [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))].sort((a, b) => b.length - a.length)) value = value.split(replacement).map(part => part.split(secret).join(replacement)).join(replacement);
  return value;
}`;
const ORIGINAL_SCRUB_ATTRIBUTE = String.raw`export function scrubAttribute(value: string, secrets: readonly string[] = [], incompleteURLs = false): string {
  if (value.length > catalog.limits.max_text_chars) throw new Error("Privacy text budget");
  if (wholeSecret(value, secrets)) return replacement;
  // Preserve S1's JSON-scalar handling as well as S6's object/array walker. SDKs
  // can serialize a primitive media value as a quoted JSON output.value.
  let parsed: unknown;
  try { parsed = JSON.parse(value); } catch {
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
}`;

// Additional verbatim 15c96fdc originals; these six functions are unchanged at
// the immediate c6225168 parent. Full-module hashes below enforce both facts.
const ORIGINAL_KNOWN_SECRET_RANGES = String.raw`function knownSecretRanges(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget): SourceRange[] {
  const matches: SourceRange[] = [];
  let gaps: SourceRange[] = [], at = 0;
  for (let next = value.indexOf(replacement); next >= 0; next = value.indexOf(replacement, at)) {
    charge(budget, depth);
    if (next > at) gaps.push({ start: at, end: next });
    at = next + replacement.length;
  }
  if (at < value.length) gaps.push({ start: at, end: value.length });
  for (const secret of [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))].sort((a, b) => b.length - a.length)) {
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
}`;
const ORIGINAL_TEXT = String.raw`function text(value: string, secrets: readonly string[], framed: boolean, incomplete = false): string {
  // This function only receives prose gaps, names or URL-owned values. Match
  // original credentials before URL query/userinfo rewrites can split them.
  value = knownSecretText(value, secrets);
  if (framed && isURLValue(value)) return credentialText(urlValue(value, incomplete), secrets);
  let end = 0, result = "";
  for (const match of value.matchAll(new RegExp(TEXT.source, TEXT.flags))) {
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
}`;
const ORIGINAL_TEXT_OWNERS = String.raw`function textOwners(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {
  charge(budget, depth, value.length);
  const owners: TextOwner[] = [];
  const urls: RegExpMatchArray[] = []; let urlIndex = 0;
  for (const match of value.matchAll(new RegExp(TEXT.source, TEXT.flags))) { charge(budget, depth); urls.push(match); }
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
}`;
const ORIGINAL_CROSS_OWNER = String.raw`function redactCrossOwnerSecrets(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget): string {
  if (!ranges.length) return value;
  const urls = [...value.matchAll(new RegExp(TEXT.source, TEXT.flags))].map(match => ({ start: match.index!, end: match.index! + match[0].length }));
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
}`;
const ORIGINAL_EMBEDDED = String.raw`function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false): string {
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
}`;
// A normal string literal preserves discover's nested template literal exactly.
const ORIGINAL_DISCOVER = "function discover(sources: Iterable<unknown>, entries = dataEntries, tokenParents?: WeakSet<object>, budget: PrivacyBudget = newBudget(), inspect?: (characters: number) => void): string[] {\n  const found = new Set<string>(), seen = new Set<object>();\n  const add = (value: string) => { if (value.length < 8 || value === replacement) return; if (found.size >= 1024 && !found.has(value)) throw new Error(\"Privacy credential budget\"); found.add(value); };\n  const urls = (value: string) => {\n    const visit = (url: string) => {\n      inspect?.(url.length);\n      for (const pair of queryParts(url).pairs) if (pair.equals && isCredentialName(pair.decodedName, true)) { add(pair.value); add(pair.decodedValue); }\n    };\n    if (isURLValue(value, inspect)) visit(value);\n    else {\n      inspect?.(value.length);\n      for (const match of value.matchAll(new RegExp(URL_TOKEN, \"gi\"))) {\n        const closing = match.index! > 0 && value[match.index! - 1] === \"'\" && match[0].endsWith(\"'\");\n        visit(closing ? match[0].slice(0, -1) : match[0]);\n      }\n    }\n  };\n  const jsonContainer = (value: string): boolean => {\n    // Probe only characters actually examined, including arbitrarily long\n    // whitespace prefixes. Reuse the result instead of probing the input twice.\n    for (let i = 0; i < value.length; i++) {\n      inspect?.(1);\n      const char = value[i];\n      if (!/\\s/.test(char)) return char === \"[\" || char === \"{\";\n    }\n    return false;\n  };\n  const visit = (value: unknown, path = \"\", depth = 0, query = false, exemptToken = false) => {\n    const json = typeof value === \"string\" && jsonContainer(value);\n    if (typeof value === \"string\" && json && value.length > MAX_DISCOVERY_BODY_BYTES) throw new Error(\"Privacy discovery body budget\");\n    charge(budget, depth, typeof value === \"string\" ? value.length : 0);\n    const credential = isCredentialName(path, query) && !exemptToken;\n    if (typeof value === \"string\") {\n      if (credential) {\n        add(value);\n        if (leaf(path).endsWith(\"authorization\")) { inspect?.(value.length); add(value.replace(/^\\S+\\s+/, \"\")); }\n      } else {\n        urls(value);\n        if (json) {\n          inspect?.(value.length);\n          let parsed: unknown; try { parsed = JSON.parse(value); } catch { return; }\n          visit(parsed, \"\", depth + 1);\n        }\n      }\n    } else if (credential && (typeof value === \"number\" || typeof value === \"boolean\")) add(String(value));\n    else if (value && typeof value === \"object\" && !seen.has(value)) {\n      seen.add(value);\n      const items = entries(value); if (items.length > budget.nodes) throw new Error(\"Privacy discovery budget\");\n      for (const [name, item] of items) {\n        if ((budget.text -= name.length) < 0) throw new Error(\"Privacy text budget\");\n        visit(item, path ? `${path}.${name}` : name, depth + 1, query || /^(?:query|defaultQuery|searchParams)$/.test(path), name === \"token\" && typeof item === \"string\" && !!tokenParents?.has(value));\n      }\n    }\n  };\n  for (const source of sources) visit(source);\n  return [...found].sort((a, b) => b.length - a.length);\n}";
const PARENT_KNOWN_SECRET_TEXT = String.raw`function knownSecretText(value: string, secrets: readonly string[]): string {
  for (const secret of [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))].sort((a, b) => b.length - a.length)) {
    // Absent from the whole value implies absent from every sentinel-separated
    // part. This pure no-op has no charge; do NOT apply it to knownSecretRanges.
    // Preserve the original operation for non-string runtime inputs as well.
    if (typeof value === "string" && typeof secret === "string" && value.indexOf(secret) < 0) continue;
    value = value.split(replacement).map(part => part.split(secret).join(replacement)).join(replacement);
  }
  return value;
}`;
// Option B (r12) rebase: the typed-session name exception changed isSessionIdentity; the r8 parent had the
// always-false body below. Restore it here so the parent/oracle reconstruction stays pinned (no other change).
const ORIGINAL_SESSION_IDENTITY = String.raw`export function isSessionIdentity(context: object | undefined, value: unknown): boolean {
  // B2 is not established: setSession accepts arbitrary caller strings. Exact
  // context equality alone does not prove a non-authorizing identifier.
  // Keep credential-name/value masking until a trusted producer contract exists.
  return false;
}`;
// 1.2.0 scheme-URL fix: restore the exact bareMedia bytes at
// 3888b65cdc2b7976e96754cb1b4a8de446aed691 before rebuilding the older oracles.
const ORIGINAL_BARE_MEDIA = String.raw`function bareMedia(value: string): string {
  // Only free-text gaps enter this lexer, never reconstructed URL/JSON syntax.
  return value.replace(/[A-Za-z0-9+/_=-]{8,}/g, candidate =>
    candidate !== "__REDACTED__" && magic(candidate) ? omitted(candidate) : candidate);
}`;
// 1.2.1 typed-image/video fixes: restore the changed sections to their exact
// 59fa6fa813b4877a6106f78b540a25bae0e343e6 bytes before rebuilding older oracles.
// This is reconstruction only: candidate tests still load the current runtime
// with the current catalog, and the existing parent/base seals stay unchanged.
const RELEASED_RUNTIME_SHA256 = "60f077212c48817dc81ee3c1826f3945dc677ddfcff74d325d72140fa2ecad7c";
const RELEASED_IMAGE_HINTS = "// The third bit is a complete, non-media MIME discriminator. It is never set\n// for a value at the SDK's truncation boundary (even a syntactically valid prefix).\ntype Hint = readonly [boolean, boolean, boolean?];\nexport type PrivacyHints = ReadonlyMap<string, Hint>;\nfunction hint(name: string, value: unknown, valueLimit = Infinity): Hint {\n  if (Array.isArray(value)) {\n    let m = false, a = false, safe = value.length > 0;\n    for (const item of value) {\n      if (typeof item !== \"string\") { safe = false; continue; }\n      const next = hint(name, item, valueLimit); m ||= next[0]; a ||= next[1]; safe &&= !!next[2];\n    }\n    return [m, a, safe && !m && !a];\n  }\n  if (typeof value !== \"string\") return [false, false];\n  const nameLeaf = leaf(name);\n  // Do not split/lowercase megabyte exception messages merely to learn that\n  // their key is not a MIME/type discriminator.\n  if (!mime.names.includes(nameLeaf) && nameLeaf !== \"type\") return [false, false];\n  const type = value.split(\";\", 1)[0].trim().toLowerCase();\n  if (mime.names.includes(nameLeaf)) {\n    const m = mime.prefixes.some(prefix => type.startsWith(prefix)) || mime.exact.includes(type);\n    return [m, type.startsWith(\"audio/\"), !m && value.length < valueLimit && /^[a-z0-9!#$&^_.+-]+\\/[a-z0-9!#$&^_.+-]+$/.test(type)];\n  }\n  return [false, nameLeaf === \"type\" && audio.type_names.includes(normalizePrivacyName(value))];\n}\n/** Index all siblings once, preserving exact indexed parents and combining duplicate query hints. */\nexport function indexPrivacyHints(items: Iterable<readonly [string, unknown]>, path = \"\", previous: PrivacyHints = new Map(), valueLimit = Infinity): PrivacyHints {\n  const hints = new Map(previous);\n  for (const [name, value] of items) {\n    const next = hint(name, value, valueLimit);\n    if (!next[0] && !next[1] && !mime.names.includes(leaf(name))) continue;\n    const parts = pathParts(path ? `${path}.${name}` : name);\n    while (parts.length && /^\\d+$/.test(parts.at(-1)!)) parts.pop();\n    parts.pop(); const parent = parts.join(\".\"), old = hints.get(parent);\n    const m = next[0] || !!old?.[0], a = next[1] || !!old?.[1];\n    hints.set(parent, [m, a, !m && !a && !!next[2] && (old === undefined || !!old[2])]);\n  }\n  return hints;\n}\nfunction context(path: string, hints: PrivacyHints, inheritedMedia = false, inheritedAudio = false, retainPayload = false) {\n  let m = inheritedMedia || !!hints.get(\"\")?.[0], a = inheritedAudio || !!hints.get(\"\")?.[1], payload = false, direct = false;\n  const prefix: string[] = [];\n  for (const part of pathParts(path)) {\n    prefix.push(part); const name = normalizePrivacyName(part);\n    if (!/^\\d+$/.test(part)) {\n      if (audio.metadata_fields.includes(name)) { m = false; a = false; if (!retainPayload) payload = false; }\n      direct = audio.fields.includes(name); a ||= direct;\n      if (m && mime.payload_fields.includes(name) || a && audio.payload_fields.includes(name)) payload = true;\n    }\n    const next = hints.get(prefix.join(\".\")); m ||= !!next?.[0]; a ||= !!next?.[1];\n  }\n  return { media: m, audio: a, payload: payload || direct };\n}\n";
const PARENT_RUNTIME_SHA256 = "d4601e3aff870d917336c78510ffa0544eb62204383d8e429c7ab489fc7efc17";
const BASE_RUNTIME_SHA256 = "411d30cc45c3cdc801a6a9e172ddd3cf41a6ffdc4b193861cec6962a067b95a9";
// Rebase both runtime oracles onto scalar catalog v8 (exact typed legacy and aggregate audio
// counters). Runtime seals and all equivalence assertions are unchanged;
// genkitContentPolicy.test.ts checks valid counts, invalid values and media hints.
const BASE_CATALOG_SHA256 = "8d926d9184bcfd57da577ec07c2c7652d1f2cbff556dfccd6931ce2299ba84fe";
const source = readFileSync(join(__dirname, "..", "privacy-runtime.ts"), "utf8");
const currentCatalogSource = readFileSync(join(__dirname, "..", "privacy.json"), "utf8");
// Strip each additive declaration section explicitly; neither belongs to 1.2.0.
let catalogSource = replaceSection(currentCatalogSource, '    "video": {\n', '    "audio": {\n', "");
catalogSource = replaceSection(catalogSource, '    "image": {\n', '    "audio": {\n', "");
const catalog = JSON.parse(currentCatalogSource);
const MAX_TEXT: number = catalog.limits.max_text_chars;
const digest = (value: string) => createHash("sha256").update(value).digest("hex");

function replaceSection(input: string, start: string, stop: string, original: string): string {
  const begin = input.indexOf(start), end = input.indexOf(stop, begin);
  if (begin < 0 || end < begin || input.indexOf(start, begin + start.length) >= 0) {
    throw new Error("Privacy oracle source boundary drift");
  }
  return input.slice(0, begin) + original + input.slice(end);
}
let releasedSource = replaceSection(source, "    video: {", "    audio: {", "");
releasedSource = replaceSection(releasedSource, "    image: {", "    audio: {", "");
// The pinned section also replaces the new video branch inside hint().
releasedSource = replaceSection(releasedSource, "// The third bit is a complete, non-media MIME discriminator.", "const zeroDetailDeferral:", RELEASED_IMAGE_HINTS);
releasedSource = replaceSection(releasedSource, "    if (declaredImagePayload(path, hints)", "    if (magic(value))", "");
if (digest(releasedSource) !== RELEASED_RUNTIME_SHA256) throw new Error("Privacy oracle no longer reconstructs the 1.2.0 runtime");
let parentSource = replaceSection(releasedSource, "// A warm identity is not proof", "function knownSecretText(", "");
parentSource = replaceSection(parentSource, "const DISCOVERY_URLS =", "const dataText =", "");
parentSource = replaceSection(parentSource, "function knownSecretText(", "\ninterface SourceRange", PARENT_KNOWN_SECRET_TEXT);
parentSource = replaceSection(parentSource, "function knownSecretRanges(", "\nconst wholeSecret =", ORIGINAL_KNOWN_SECRET_RANGES);
parentSource = replaceSection(parentSource, "function bareMedia(", "\nfunction text(", ORIGINAL_BARE_MEDIA);
parentSource = replaceSection(parentSource, "function text(", "\nexport interface PrivacyBudget", ORIGINAL_TEXT);
parentSource = replaceSection(parentSource, "function textOwners(", "\n/** Reconcile original credential ranges", ORIGINAL_TEXT_OWNERS);
parentSource = replaceSection(parentSource, "function redactCrossOwnerSecrets(", "\n/** S1's additional malformed/embedded JSON policy", ORIGINAL_CROSS_OWNER);
parentSource = replaceSection(parentSource, "function scrubEmbeddedText(", "\n/** Free-text entry point", ORIGINAL_EMBEDDED);
parentSource = replaceSection(parentSource, "function discover(", "\nexport function credentialSecrets(", ORIGINAL_DISCOVER);
parentSource = replaceSection(parentSource, "export function isSessionIdentity(", "\n\nexport interface ProviderResponseBinding", ORIGINAL_SESSION_IDENTITY);
if (digest(parentSource) !== PARENT_RUNTIME_SHA256) throw new Error("Privacy oracle no longer reconstructs the immediate r8 parent");
let oracleSource = replaceSection(parentSource,
  "// Names repeat across spans and recognition passes. Bound both cardinality and\n",
  "\nconst pathParts =", ORIGINAL_NORMALIZE);
oracleSource = replaceSection(oracleSource, "function knownSecretText(", "\ninterface SourceRange", ORIGINAL_KNOWN_SECRET_TEXT);
oracleSource = replaceSection(oracleSource, "export function scrubAttribute(", "\n\n/** S5 harvesting", ORIGINAL_SCRUB_ATTRIBUTE);
// Refuse a silently moving oracle: the unchanged dependencies must reconstruct
// the ENTIRE pinned source, not just functions that happen to agree today.
// A later privacy change must explicitly rebase this versioned oracle.
if (digest(oracleSource) !== BASE_RUNTIME_SHA256 || digest(catalogSource) !== BASE_CATALOG_SHA256) {
  throw new Error("Privacy oracle no longer reconstructs the pinned runtime/catalog");
}

type TestRange = { start: number; end: number };
type TestOwner = TestRange & { kind: "json" | "wrapper" | "tail" | "field"; parsed?: unknown; key?: string; valueStart?: number; keyEnd?: number };
type PreparedEntry = { input: string[]; ordered: string[] };
type LoadedRuntime = typeof runtime & {
  __perfInternals: {
    knownSecretText: (value: string, secrets: readonly string[]) => string;
    knownSecretRanges: (value: string, secrets: readonly string[], depth: number, budget: runtime.PrivacyBudget) => TestRange[];
    text: (value: string, secrets: readonly string[], framed: boolean, incomplete?: boolean) => string;
    textOwners: (value: string, depth: number, budget: runtime.PrivacyBudget) => TestOwner[];
    redactCrossOwnerSecrets: (value: string, ranges: TestRange[], owners: TestOwner[], depth: number, budget: runtime.PrivacyBudget) => string;
    scrubEmbeddedText: (value: string, secrets: readonly string[], depth: number, budget: runtime.PrivacyBudget, framed: boolean, incomplete?: boolean, incompleteURLs?: boolean, payloadRole?: boolean) => string;
    discover: (sources: Iterable<unknown>, entries?: (value: object) => [string, unknown][], tokenParents?: WeakSet<object>, budget?: runtime.PrivacyBudget, inspect?: (characters: number) => void) => string[];
    textRanges: (value: string) => TestRange[];
    prepareSecrets?: (secrets: readonly string[]) => readonly string[];
    preparedMemo?: () => WeakMap<object, PreparedEntry>;
    admissions?: () => number;
    normalizedPrivacyNames?: Map<string, string>;
  };
};
function loadRuntime(input: string, exposeMemo = false): LoadedRuntime {
  const compiled = transpileModule(input, {
    fileName: "privacy-runtime.ts",
    compilerOptions: { module: ModuleKind.CommonJS, target: ScriptTarget.ES2022 },
    reportDiagnostics: true,
  });
  if (compiled.diagnostics?.some(diagnostic => diagnostic.category === 1)) {
    throw new Error("Privacy equivalence fixture did not transpile");
  }
  const module = { exports: {} };
  // Same realm as the test: plain-object recognition depends on prototype
  // identity. A fresh vm realm would reject the test's ordinary object inputs.
  // Only the dependency-free leaf and its catalog are evaluated. Nothing is
  // written to disk and private helpers do not become production exports.
  const evaluate = new Function("require", "module", "exports", compiled.outputText
    + "\nmodule.exports.__perfInternals = { knownSecretText, knownSecretRanges, text, textOwners, redactCrossOwnerSecrets, scrubEmbeddedText, discover,"
    + " textRanges: value => [...value.matchAll(new RegExp(TEXT.source, TEXT.flags))].map(match => ({start: match.index, end: match.index + match[0].length}))"
    + (exposeMemo ? ", normalizedPrivacyNames, prepareSecrets, preparedMemo: () => preparedSecrets, admissions: () => preparedSecretAdmissions" : "") + " };\n");
  evaluate((id: string) => {
    if (id !== "./privacy.json") throw new Error("Unexpected privacy fixture import");
    return catalog;
  }, module, module.exports);
  return module.exports as LoadedRuntime;
}
const oracle = loadRuntime(oracleSource);
const candidate = loadRuntime(source, true);
const parent = loadRuntime(parentSource);

type Outcome = { kind: "return"; value: unknown } | {
  kind: "throw"; errorClass?: Function; name?: string; message?: string; value?: unknown;
};
function outcome(run: () => unknown): Outcome {
  try { return { kind: "return", value: run() }; }
  catch (error) {
    if (error instanceof Error) return { kind: "throw", errorClass: error.constructor, name: error.name, message: error.message };
    return { kind: "throw", value: error };
  }
}
function equal(run: () => unknown, old: () => unknown, caseId: string): void {
  const expected = outcome(old), actual = outcome(run);
  expect({ caseId, result: actual }).toEqual({ caseId, result: expected });
}
function prng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state ^= state << 13; state ^= state >>> 17; state ^= state << 5;
    return state >>> 0;
  };
}
const ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_./[]{}:=,;<>!?&%#'\"\\\t\r\n";
const UNICODE = ["é", "漢", "𝄞", "\ud800", "\udfff", "\u0000", " ", " ", "﻿", "İ", "ß", "Σ"];
function randomText(random: () => number, length: number, unicode = false): string {
  let out = "";
  for (let i = 0; i < length; i++) {
    out += unicode && random() % 4 === 0 ? UNICODE[random() % UNICODE.length] : ASCII[random() % ASCII.length];
  }
  return out;
}
type Case = { text: string; secrets: string[]; path: string };
function generatedCase(index: number, random: () => number): Case {
  const stem = "fixture-" + randomText(random, 12, true);
  const short = stem.slice(0, 8), long = stem + "-tail", overlap = stem.slice(3) + "-end";
  const secrets = [short, long, stem, overlap, stem, "", "tiny", "<redacted>", "pre<redacted>post", "redacted", "<redacted>x"];
  for (let i = secrets.length - 1; i > 0; i--) {
    const j = random() % (i + 1); [secrets[i], secrets[j]] = [secrets[j], secrets[i]];
  }
  const prose = randomText(random, random() % 160, true);
  const paths = ["", "message", "provider_client_secret_tokens", "gen_ai.usage.input_tokens",
    "parts[0].content.chunk", "parts.1.transcript.content", "audio.data", "gen_ai.audio.bytes"];
  let text: string;
  switch (index % 24) {
    case 0: text = randomText(random, random() % 256); break;
    case 1: text = prose; break;
    case 2: text = "before " + stem + " after " + prose; break;
    case 3: text = short + long + short + short + long; break;
    // stem and overlap both match stem + '-end', starting three units apart.
    case 4: text = stem + "-end " + stem + stem.slice(0, -1) + stem; break;
    case 5: text = "<redacted>" + stem + "<redacted>pre<redacted>post"; break;
    case 6: text = JSON.stringify({ message: prose, authorization: "Bearer " + stem, prompt_tokens: random() % 1000 }); break;
    case 7: text = JSON.stringify(JSON.stringify({ message: long, n: random() % 1000 })); break;
    case 8:
      text = '{"n":9007199254740993,"n":1,"message":' + JSON.stringify(prose) + '}';
      secrets.push("9007199254740993"); break;
    case 9:
      text = " \t" + String(10000000 + random() % 10000000) + "\r\n";
      if (random() % 2) secrets.push(text.trim()); break;
    // URLSearchParams also accepts the intentionally generated lone surrogates.
    case 10: text = "https://example.invalid/path?" + new URLSearchParams({ api_key: stem }).toString() + "&model=keep#anchor"; break;
    case 11: text = "https://example.invalid/?content=AQIDBA==&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor"; break;
    case 12: text = ""; break;
    case 13: text = ["true", "false", "null", "-0", "1e999", "\t-1.25e+3 \n", " 0.000000001 "][random() % 7]; break;
    case 14: text = '{"message":' + JSON.stringify(prose) + ',"audio":{"data":'; break;
    case 15: text = "Note: [prose " + JSON.stringify({ message: stem }) + "] suffix " + prose; break;
    case 16: text = 'data:image/svg+xml,<svg title="' + stem + '">payload</svg>'; break;
    case 17: text = JSON.stringify("iVBORw0KGgoAAAANSUhEUg=="); break;
    case 18: text = JSON.stringify({ audio: { data: [1, 2, 3], transcript: { content: prose } }, n: 7 }); break;
    case 19: text = "x " + JSON.stringify({ message: stem, number: random() }) + " then " + prose; break;
    case 20: text = [" ", "\t\r\n", " ", "﻿", " ", "\v"][random() % 6] + JSON.stringify(prose); break;
    case 21: text = "pre<redacted>post<redacted>x " + long + " <redacted>"; break;
    case 22: text = ["not JSON ", "falsehood ", "true-ish ", "-nope ", ".5 ", "+1 "][random() % 6] + prose; break;
    default: text = 'message="' + prose + '" password=' + stem + '\nmodel=keep';
  }
  // Four genuinely large generated cases in EACH 20,000-case function corpus,
  // at max-1, max, max+1, max-2. Keep random bulk cases small for CI throughput.
  if (index % 5000 === 4999) {
    const delta = [-1, 0, 1, -2][Math.floor(index / 5000)];
    const prefix = "Q" + String(random());
    text = prefix + "x".repeat(MAX_TEXT + delta - prefix.length - stem.length) + stem;
  }
  return { text, secrets, path: paths[random() % paths.length] };
}

const COUNT = 20_000;
describe("privacy hot-path equivalence against the pinned implementation", () => {
  test("oracle reconstructs the exact pinned source and catalog", () => {
    expect(digest(oracleSource)).toBe(BASE_RUNTIME_SHA256);
    expect(digest(catalogSource)).toBe(BASE_CATALOG_SHA256);
    expect(Object.keys(candidate).filter(key => key !== "__perfInternals").sort()).toEqual(Object.keys(runtime).sort());
  });

  test.each(["normalizePrivacyName", "knownSecretText", "scrubAttribute"] as const)(
    "%s preserves values and thrown classes/messages on 20,000 seeded inputs", name => {
      const random = prng(0x51c0f17e);
      for (let i = 0; i < COUNT; i++) {
        const { text, secrets } = generatedCase(i, random);
        const caseId = name + "/seed-51c0f17e/" + i;
        if (name === "normalizePrivacyName") {
          equal(() => runtime.normalizePrivacyName(text), () => oracle.normalizePrivacyName(text), caseId);
          // A second call exercises hits (including cached empty-string results).
          equal(() => runtime.normalizePrivacyName(text), () => oracle.normalizePrivacyName(text), caseId + "/warm");
        } else if (name === "knownSecretText") {
          equal(() => candidate.__perfInternals.knownSecretText(text, secrets), () => oracle.__perfInternals.knownSecretText(text, secrets), caseId);
        } else {
          equal(() => runtime.scrubAttribute(text, secrets, !!(i % 2)), () => oracle.scrubAttribute(text, secrets, !!(i % 2)), caseId);
        }
      }
    }, 120_000,
  );

  test("budget debits and post-error state match, including absent secrets and warm names", () => {
    const random = prng(0xb0d6e712);
    for (let i = 0; i < 2048; i++) {
      const c = generatedCase(i, random);
      const value = i % 2 ? c.text : { message: c.text, n: i, nested: [c.text, false] };
      const nodes = [-1, 0, 1, 2, 4, 8, 16, 32, 128, 16384][random() % 10];
      const text = [-1, 0, 1, c.text.length - 1, c.text.length, c.text.length * 2, MAX_TEXT][random() % 7];
      const depth = [-1, 0, 1, 31, 32, 33][random() % 6];
      const hints = runtime.indexPrivacyHints([["parts[0].mime_type", "audio/pcm"]]);
      const run = (api: typeof runtime) => {
        const budget: runtime.PrivacyBudget = { nodes, text, seen: new WeakSet() };
        const result = outcome(() => api.scrubValue(value, c.secrets, c.path, depth, false, budget, hints));
        return { result, nodes: budget.nodes, text: budget.text,
          seenRoot: typeof value === "object" && budget.seen.has(value) };
      };
      expect({ i, state: run(runtime) }).toEqual({ i, state: run(oracle) });
    }
  }, 60_000);

  test("knownSecretRanges still charges every absent secret and gap", () => {
    for (const count of [8190, 8191, 8192]) {
      const secrets = Array.from({ length: count }, (_, i) => "absent-fixture-" + i.toString(36).padStart(4, "0"));
      equal(() => runtime.scrubText("ordinary prose", secrets), () => oracle.scrubText("ordinary prose", secrets), "absent-budget/" + count);
      equal(() => runtime.scrubAttribute("ordinary prose", secrets), () => oracle.scrubAttribute("ordinary prose", secrets), "attribute-budget/" + count);
      if (count === 8192) expect(() => runtime.scrubText("ordinary prose", secrets)).toThrow("Privacy structure budget");
    }
  });

  test("normalization memo has bounded entries and bounded key/value lengths", () => {
    const memo = candidate.__perfInternals.normalizedPrivacyNames!;
    memo.clear();
    for (let i = 0; i < 2049; i++) {
      const value = "Fixture_Field-" + i;
      expect(candidate.normalizePrivacyName(value)).toBe(oracle.normalizePrivacyName(value));
      expect(memo.size).toBeLessThanOrEqual(512);
    }
    expect(memo.has("Fixture_Field-0")).toBe(false);
    const oversized = "A".repeat(257);
    const before = [...memo];
    expect(candidate.normalizePrivacyName(oversized)).toBe(oracle.normalizePrivacyName(oversized));
    expect([...memo]).toEqual(before);
    candidate.normalizePrivacyName(""); candidate.normalizePrivacyName("");
    candidate.normalizePrivacyName("B".repeat(256));
    expect(memo.get("")).toBe("");
    expect(memo.size).toBeLessThanOrEqual(512);
    for (const [key, value] of memo) {
      expect(key.length).toBeLessThanOrEqual(256);
      expect(value.length).toBeLessThanOrEqual(256);
    }
  });

  test("mutable, sparse, frozen and getter-backed secret arrays keep their semantics", () => {
    const mutable = ["abcdefgh", "abcdefghi", "bcdefghij", "abcdefgh"];
    const check = (secrets: readonly string[], id: string) => {
      for (const value of ["", "ordinary prose", "abcdefghijabcdefgh", "pre<redacted>post", "<redacted>abcdefgh"]) {
        equal(() => candidate.__perfInternals.knownSecretText(value, secrets), () => oracle.__perfInternals.knownSecretText(value, secrets), id);
        equal(() => runtime.scrubAttribute(value, secrets), () => oracle.scrubAttribute(value, secrets), id + "/public");
      }
    };
    check(mutable, "initial"); mutable[0] = "ordinary prose"; check(mutable, "replace");
    mutable.reverse(); check(mutable, "reorder"); mutable.length = 1; check(mutable, "shrink");
    mutable.push("pre<redacted>post"); check(mutable, "grow");
    const sparse: string[] = []; sparse[3] = "abcdefgh"; check(sparse, "sparse");
    check(Object.freeze(["abcdefgh", "abcdefgh", "<redacted>"]), "frozen");
    const withGetter = (fn: (value: string, secrets: readonly string[]) => string) => {
      let reads = 0;
      const array = Object.defineProperty(["abcdefgh"], "0", { get: () => { reads++; return "abcdefgh"; } });
      const result = outcome(() => fn("ordinary prose", array));
      return { result, reads };
    };
    expect(withGetter(candidate.__perfInternals.knownSecretText)).toEqual(withGetter(oracle.__perfInternals.knownSecretText));
  });

  test("non-string runtime inputs retain the old errors and coercions", () => {
    const values: unknown[] = [undefined, null, false, true, 0, -0, 12345678, NaN, Infinity, BigInt(42),
      Symbol("synthetic"), new String("Fixture_NAME"), {}, [], ["ordinary"]];
    for (const [i, value] of values.entries()) {
      equal(() => runtime.normalizePrivacyName(value as string), () => oracle.normalizePrivacyName(value as string), "non-string-name/" + i);
      for (const secrets of [[], ["abcdefgh"]]) {
        equal(() => candidate.__perfInternals.knownSecretText(value as string, secrets), () => oracle.__perfInternals.knownSecretText(value as string, secrets), "non-string-text/" + i);
        equal(() => runtime.scrubAttribute(value as string, secrets), () => oracle.scrubAttribute(value as string, secrets), "non-string-attribute/" + i);
      }
    }
    for (const secrets of [[null], [undefined], [12345678], [new String("abcdefgh")], null, undefined]) {
      equal(() => candidate.__perfInternals.knownSecretText("ordinary prose", secrets as unknown as string[]),
        () => oracle.__perfInternals.knownSecretText("ordinary prose", secrets as unknown as string[]), "invalid-secret-entry");
    }
    const hook = (fn: (value: string, secrets: readonly string[]) => string) => {
      let calls = 0;
      const secret = { length: 8, [Symbol.split](value: string) { calls++; return [value]; } };
      return { result: outcome(() => fn("ordinary prose", [secret as unknown as string])), calls: () => calls };
    };
    const old = hook(oracle.__perfInternals.knownSecretText), next = hook(candidate.__perfInternals.knownSecretText);
    expect({ result: next.result, calls: next.calls() }).toEqual({ result: old.result, calls: old.calls() });
  });

  test("JSON prefix gate retains all valid scalar initials and exact whitespace rules", () => {
    const roots = ['{"message":"ordinary"}', '[1,"ordinary"]', '"ordinary"', "-0", "0", "1e999", "true", "false", "null"];
    for (let code = 0; code < 128; code++) for (const root of roots) {
      const value = String.fromCharCode(code) + root;
      equal(() => runtime.scrubAttribute(value), () => oracle.scrubAttribute(value), "prefix/" + code + "/" + root);
    }
    for (const space of ["", " ", "\t\n\r ", " ", "﻿", " ", " ", "\v", "\f"]) {
      for (const root of roots) {
        equal(() => runtime.scrubAttribute(space + root), () => oracle.scrubAttribute(space + root), "unicode-prefix");
      }
    }
  });

  test("replacement sentinels, overlap order and newly inserted sentinels stay protected", () => {
    const table: Array<[string, string[], string]> = [
      ["abcdefghij", ["abcdefgh", "bcdefghij"], "a<redacted>"],
      ["xxabcdefghabcdefghyy", ["abcdefgh"], "xx<redacted><redacted>yy"],
      ["pre<redacted>post", ["pre<redacted>post"], "pre<redacted>post"],
      ["<redacted>abcdefgh", ["abcdefgh", "redacted"], "<redacted><redacted>"],
      ["<redacted>abc", ["dacted>abc"], "<redacted>abc"],
      ["abcdefgh", ["abcdefgh", "<redacted>", "redacted"], "<redacted>"],
    ];
    for (const [value, secrets, expected] of table) {
      expect(oracle.__perfInternals.knownSecretText(value, secrets)).toBe(expected);
      expect(candidate.__perfInternals.knownSecretText(value, secrets)).toBe(expected);
    }
  });
});

const fullBudget = (): runtime.PrivacyBudget => ({ nodes: catalog.limits.max_nodes, text: MAX_TEXT, seen: new WeakSet() });
const originalPreparation = (secrets: readonly string[]) => [...new Set(secrets.filter(s => s.length >= 8 && s !== catalog.credentials.replacement))].sort((a, b) => b.length - a.length);

describe("r3b preparation and matcher reuse with original scan ordering", () => {
  test("both complete source seals remain exact", () => {
    expect(digest(parentSource)).toBe(PARENT_RUNTIME_SHA256);
    expect(digest(oracleSource)).toBe(BASE_RUNTIME_SHA256);
    expect(digest(catalogSource)).toBe(BASE_CATALOG_SHA256);
  });

  test.each(["knownSecretRanges", "text", "textOwners", "redactCrossOwnerSecrets", "scrubEmbeddedText", "discover"] as const)(
    "%s preserves values, errors and ledgers on 20,000 seeded inputs", name => {
      const random = prng(0x73337066);
      for (let i = 0; i < COUNT; i++) {
        const c = generatedCase(i, random);
        const nodes = i % 3 === 0 ? catalog.limits.max_nodes : [-1, 0, 1, 2, 8, 32, 128, 8191, 16384][random() % 9];
        const chars = i % 3 === 0 ? MAX_TEXT : [-1, 0, 1, c.text.length - 1, c.text.length, c.text.length * 2, MAX_TEXT][random() % 7];
        const depth = i % 3 === 0 ? 0 : [-1, 0, 1, 31, 32, 33][random() % 6];
        const fixtureBudget = (): runtime.PrivacyBudget => ({ nodes: Infinity, text: Infinity, seen: new WeakSet() });
        let ranges: TestRange[] = [], owners: TestOwner[] = [];
        if (name === "redactCrossOwnerSecrets") {
          ranges = oracle.__perfInternals.knownSecretRanges(c.text, c.secrets, 0, fixtureBudget());
          // Include boundary-crossing intervals even when random secrets are absent.
          if (i % 4 === 0 && c.text.length) ranges = [{ start: 0, end: c.text.length }];
          if (i % 4 === 1 && c.text.length > 2) ranges = [{ start: 1, end: c.text.length - 1 }];
          const found = outcome(() => oracle.__perfInternals.textOwners(c.text, 0, fixtureBudget()));
          if (found.kind === "return") owners = found.value as TestOwner[];
        }
        const run = (api: LoadedRuntime) => {
          const budget: runtime.PrivacyBudget = { nodes, text: chars, seen: new WeakSet() };
          const inspect: number[] = [];
          const helper = api.__perfInternals;
          const result = outcome(() => {
            switch (name) {
              case "knownSecretRanges": return helper.knownSecretRanges(c.text, c.secrets, depth, budget);
              case "text": return helper.text(c.text, c.secrets, !!(i & 1), !!(i & 2));
              case "textOwners": return helper.textOwners(c.text, depth, budget);
              case "redactCrossOwnerSecrets": return helper.redactCrossOwnerSecrets(c.text, ranges, owners, depth, budget);
              case "scrubEmbeddedText": return helper.scrubEmbeddedText(c.text, c.secrets, depth, budget, !!(i & 1), !!(i & 2), !!(i & 4), !!(i & 8));
              case "discover": return helper.discover([c.text, { nested: { authorization: "Bearer fixture-credential" } }], undefined, undefined, budget, n => { inspect.push(n); });
            }
          });
          return { result, nodes: budget.nodes, text: budget.text, inspect };
        };
        const caseId = name + "/seed-73337066/" + i;
        const expected = run(oracle);
        expect({ caseId, state: run(parent) }).toEqual({ caseId, state: expected });
        expect({ caseId, state: run(candidate) }).toEqual({ caseId, state: expected });
        // The removed range-sharing path has no optional arguments. Retain the
        // third comparison as a warm call, without bypassing either native scan.
        expect({ caseId, state: run(candidate) }).toEqual({ caseId, state: expected });
      }
    }, 180_000,
  );

  test("prepared secrets preserve the original expression on 20,000 seeded mutable arrays", () => {
    const random = prng(0x73337365);
    for (let i = 0; i < COUNT; i++) {
      const { text, secrets } = generatedCase(i, random);
      const id = "prepare/seed-73337365/" + i;
      equal(() => candidate.__perfInternals.prepareSecrets!(secrets), () => originalPreparation(secrets), id);
      equal(() => candidate.__perfInternals.knownSecretText(text, secrets), () => parent.__perfInternals.knownSecretText(text, secrets), id + "/parent");
      const first = candidate.__perfInternals.prepareSecrets!(secrets);
      expect(candidate.__perfInternals.prepareSecrets!(secrets)).toBe(first);
      switch (i % 6) {
        case 0: secrets.reverse(); break;
        case 1: secrets[0] = "ordinary prose"; break;
        case 2: secrets.length = 1; break;
        case 3: secrets.push("pre<redacted>post", "abcdefgh"); break;
        case 4: delete secrets[0]; break;
        default: secrets.push(...secrets.slice(0, 3));
      }
      equal(() => candidate.__perfInternals.prepareSecrets!(secrets), () => originalPreparation(secrets), id + "/mutated");
      equal(() => candidate.__perfInternals.knownSecretText(text, secrets), () => oracle.__perfInternals.knownSecretText(text, secrets), id + "/mutated-text");
    }
  }, 120_000);

  test("weak generations, content order and retained string bounds are enforced", () => {
    const api = candidate.__perfInternals;
    const keys: string[][] = [];
    for (let i = 0; i < 193; i++) {
      const key = ["fixture-secret-" + i]; keys.push(key);
      const result = api.prepareSecrets!(key);
      expect(api.prepareSecrets!(key)).toBe(result);
      expect(api.admissions!()).toBeLessThanOrEqual(64);
      const memo = api.preparedMemo!();
      expect(keys.filter(item => memo.has(item)).length).toBeLessThanOrEqual(64);
      for (const item of keys) {
        const entry = memo.get(item); if (!entry) continue;
        expect(entry.input.length).toBeLessThanOrEqual(64);
        expect(entry.input.reduce((n, s) => n + s.length, 0)).toBeLessThanOrEqual(4096);
        for (const secret of entry.input) expect(secret.length).toBeLessThanOrEqual(256);
        expect(entry.ordered).toEqual(originalPreparation(item));
      }
    }
    expect(api.preparedMemo!().has(keys[0])).toBe(false);
    const tied = ["abcdefgh", "bcdefghi"];
    expect(api.prepareSecrets!(tied)).toEqual(tied);
    tied.reverse(); expect(api.prepareSecrets!(tied)).toEqual(tied);
    const retained = api.prepareSecrets!(tied);
    tied.push(tied[0]); expect(api.prepareSecrets!(tied)).toBe(retained);
    tied[0] = "x".repeat(257); api.prepareSecrets!(tied);
    expect(api.preparedMemo!().has(tied)).toBe(false);
    for (const values of [
      Array.from({ length: 65 }, (_, i) => "fixture-" + i),
      Array.from({ length: 17 }, (_, i) => ("fixture-" + i).padEnd(256, "x")),
      ["x".repeat(257)],
    ]) {
      expect(api.prepareSecrets!(values)).toEqual(originalPreparation(values));
      expect(api.preparedMemo!().has(values)).toBe(false);
    }
    const atLimit = Array.from({ length: 16 }, (_, i) => ("fixture-" + i).padEnd(256, "x"));
    api.prepareSecrets!(atLimit); expect(api.preparedMemo!().has(atLimit)).toBe(true);
    const frozen = Object.freeze(["abcdefgh", "abcdefghi"]);
    expect(api.prepareSecrets!(frozen)).toEqual(originalPreparation(frozen));
  });

  test("warm reads preserve getters, proxy traps, species, custom iterators and failures", () => {
    type Factory = (log: string[]) => unknown;
    const factories: Factory[] = [
      log => {
        let n = 0;
        return Object.defineProperty(["abcdefgh"], "0", { get() { log.push("get:" + n); return n++ % 2 ? "bcdefghi" : "abcdefgh"; } });
      },
      log => {
        let n = 0;
        return Object.freeze(Object.defineProperty(["abcdefgh"], "0", { get() { log.push("frozen:" + n); if (n++ === 2) throw new RangeError("fixture getter"); return "abcdefgh"; } }));
      },
      log => new Proxy(["abcdefgh", , "bcdefghi"], {
        get(target, key, receiver) { log.push("get:" + String(key)); return Reflect.get(target, key, receiver); },
        has(target, key) { log.push("has:" + String(key)); return Reflect.has(target, key); },
        getOwnPropertyDescriptor(target, key) { log.push("descriptor:" + String(key)); return Reflect.getOwnPropertyDescriptor(target, key); },
      }),
      log => {
        const value = ["abcdefgh", "bcdefghi"];
        Object.defineProperty(value, "constructor", { value: { [Symbol.species]: function(length: number) {
          log.push("species:" + length);
          return new Proxy(new Array(length), {
            defineProperty(target, key, descriptor) { log.push("define:" + String(key)); return Reflect.defineProperty(target, key, descriptor); },
            get(target, key, receiver) { log.push("filtered-get:" + String(key)); return Reflect.get(target, key, receiver); },
          });
        } } });
        return value;
      },
      log => ({ filter() { log.push("filter"); return { *[Symbol.iterator]() { log.push("iterate"); yield "abcdefgh"; yield "abcdefgh"; yield "bcdefghi"; } }; } }),
      log => {
        let calls = 0;
        return { filter() { log.push("filter:" + calls); if (calls++ === 2) return null; return ["abcdefgh"]; } };
      },
      log => {
        let calls = 0;
        return { filter() { log.push("filter"); return { *[Symbol.iterator]() { log.push("yield"); yield "abcdefgh"; if (calls++ === 2) throw new SyntaxError("fixture iterator"); } }; } };
      },
      log => {
        let revoke = () => {};
        const revocable = Proxy.revocable({ filter() { log.push("revoke"); revoke(); return ["abcdefgh"]; } }, {});
        revoke = revocable.revoke; return revocable.proxy;
      },
      log => [{ get length() { log.push("length"); return 8; }, [Symbol.split](value: string) { log.push("split"); return [value]; }, toString() { log.push("coerce"); return "abcdefgh"; } }],
      () => [null], () => [undefined], () => [12345678], () => [new String("abcdefgh")],
      () => null, () => undefined,
    ];
    for (const [id, make] of factories.entries()) for (const name of ["knownSecretText", "knownSecretRanges"] as const) {
      const run = (api: LoadedRuntime) => {
        const log: string[] = [], secrets = make(log) as readonly string[];
        const calls: unknown[] = [];
        for (let i = 0; i < 4; i++) {
          const budget = fullBudget();
          const result = outcome(() => name === "knownSecretText"
            ? api.__perfInternals.knownSecretText("abcdefghij ordinary prose", secrets)
            : api.__perfInternals.knownSecretRanges("abcdefghij ordinary prose", secrets, 0, budget));
          calls.push({ result, nodes: budget.nodes, text: budget.text });
        }
        return { log, calls };
      };
      const expected = run(oracle);
      expect({ id, name, state: run(parent) }).toEqual({ id, name, state: expected });
      expect({ id, name, state: run(candidate) }).toEqual({ id, name, state: expected });
    }
  });

  test("discovery matcher survives nested inspect callbacks and failure recovery", () => {
    const values = ["before https://example.invalid/?api_key=fixture-one then /?secret=fixture-two after", "'https://example.invalid/?password=fixture-three' trailing", "https://example.invalid/?token=fixture-four", "ordinary prose", ""];
    for (let at = 1; at <= 32; at++) for (const fail of [false, true]) {
      const run = (api: LoadedRuntime) => {
        const log: unknown[] = [], budget = fullBudget(); let count = 0;
        const result = outcome(() => api.__perfInternals.discover(values, undefined, undefined, budget, n => {
          log.push(n);
          if (++count !== at) return;
          log.push(api.discoverSecrets(["nested /?api_key=fixture-nested then https://example.invalid/?secret=fixture-second"]));
          if (fail) throw new RangeError("fixture inspection");
        }));
        return { result, log, nodes: budget.nodes, text: budget.text, next: api.discoverSecrets(values) };
      };
      expect(run(candidate)).toEqual(run(oracle));
      expect(run(candidate)).toEqual(run(parent));
    }
  });

  test("non-string matchAll callers still receive fresh matcher arguments", () => {
    for (const name of ["textOwners", "redactCrossOwnerSecrets", "text"] as const) {
      const run = (api: LoadedRuntime) => {
        const matchers: RegExp[] = [];
        const value = {
          length: 0,
          indexOf() { return -1; },
          slice() { return ""; },
          matchAll(regex: RegExp) { matchers.push(regex); regex.lastIndex = 999; return []; },
        } as unknown as string;
        const results = [];
        for (let i = 0; i < 2; i++) results.push(outcome(() => {
          if (name === "textOwners") return api.__perfInternals.textOwners(value, 0, fullBudget());
          if (name === "text") return api.__perfInternals.text(value, [], false);
          return api.__perfInternals.redactCrossOwnerSecrets(value, [{ start: 0, end: 0 }], [], 0, fullBudget());
        }));
        return { results, count: matchers.length, fresh: matchers[0] !== matchers[1], sources: matchers.map(r => r.source) };
      };
      expect(run(candidate)).toEqual(run(parent));
      expect(run(candidate)).toEqual(run(oracle));
    }
  });

  test("both original scans leave every budget boundary and rewrite pass intact", () => {
    const fixtures: Array<[string, string[]]> = [
      ['prefix {"message":"fixture-secret"} suffix', ['prefix {"message"', 'fixture-secret']],
      ['before https://example.invalid/?q=fixture-secret after', ['before https://', 'fixture-secret']],
      ['Note: [prose {"message":"fixture-secret"}] password=abcdefgh suffix', ['fixture-secret', 'abcdefgh']],
      ['"password":"fixture-secret" trailing data:image/svg+xml,<svg/>', ['fixture-secret']],
      ['data:image/svg+xml,<svg/> "message": "abcdefgh"', ['abcdefgh']],
      ['plain prose', []],
      ['{"audio":{"data":"AQIDBA=="', ['AQIDBA==']],
    ];
    for (const [text, secrets] of fixtures) for (let nodes = -1; nodes < 90; nodes++) for (const chars of [0, text.length - 1, text.length, text.length * 2, MAX_TEXT]) {
      const run = (api: LoadedRuntime) => {
        const budget: runtime.PrivacyBudget = { nodes, text: chars, seen: new WeakSet() };
        const result = outcome(() => api.__perfInternals.scrubEmbeddedText(text, secrets, 0, budget, false));
        return { result, nodes: budget.nodes, text: budget.text };
      };
      expect(run(candidate)).toEqual(run(oracle));
      expect(run(candidate)).toEqual(run(parent));
    }
  });
});

// Read native legacy statics immediately at each observation point: assertion
// libraries and subsequent regex operations can overwrite these process-local
// values. No built-in, prototype or RegExp static property is patched here.
const legacyRegExpState = () => {
  // Some TypeScript lib versions omit these native legacy properties.
  const native = RegExp as typeof RegExp & { readonly lastMatch: string; readonly $1: string; readonly leftContext: string };
  return { lastMatch: native.lastMatch, capture1: native.$1, leftContext: native.leftContext };
};
const seedLegacyRegExpState = () => { /(fixture-seed)/.exec("seed-prefix fixture-seed seed-suffix"); };
type LegacyRegExpKey = keyof ReturnType<typeof legacyRegExpState>;
const legacyRegExpKeys: LegacyRegExpKey[] = ["lastMatch", "capture1", "leftContext"];

describe("r3b native RegExp legacy-static equivalence", () => {
  test("public accessor-budget errors observe the second token scan, cold and warm", () => {
    // EQUIV-R3-001: the seventh node read precedes the field-colon regex. The
    // eighth is reconciliation, AFTER the original second TEXT scan. Reusing
    // the first scan's ranges would leave lastMatch=': ', $1='', leftContext=''.
    const token = "https://example.invalid/?q=plain";
    const value = 'before https://example.invalid/?q=plain "message": "abcdefgh" suffix';
    const expectedStatics = { lastMatch: token, capture1: token, leftContext: "before " };
    for (const property of legacyRegExpKeys) for (const warm of [false, true]) {
      const run = (api: typeof runtime) => {
        const secrets = ["abcdefgh"];
        if (warm) api.scrubValue(value, secrets);
        let nodes = catalog.limits.max_nodes, reads = 0;
        let observed: ReturnType<typeof legacyRegExpState> | undefined;
        const budget: runtime.PrivacyBudget = {
          get nodes() {
            if (++reads === 8) {
              observed = legacyRegExpState();
              throw new Error(observed[property]);
            }
            return nodes;
          },
          set nodes(value: number) { nodes = value; },
          text: MAX_TEXT,
          seen: new WeakSet(),
        };
        seedLegacyRegExpState();
        const result = outcome(() => api.scrubValue(value, secrets, "", 0, false, budget));
        // Do not re-read budget.nodes: its getter is part of the observation.
        return { result, observed, reads, nodes, text: budget.text };
      };
      const expected = run(parent);
      expect(expected).toEqual({
        result: { kind: "throw", errorClass: Error, name: "Error", message: expectedStatics[property] },
        observed: expectedStatics, reads: 8, nodes: catalog.limits.max_nodes - 7,
        text: MAX_TEXT - 2 * value.length,
      });
      for (const api of [oracle, candidate, runtime]) {
        expect({ property, warm, state: run(api) }).toEqual({ property, warm, state: expected });
      }
    }
  });

  test("sort reuse preserves getter-produced statics before a budget throws", () => {
    for (const property of legacyRegExpKeys) for (const warm of [false, true]) {
      const run = (api: LoadedRuntime) => {
        let secretReads = 0, budgetReads = 0, nodes = catalog.limits.max_nodes;
        const secrets = Object.defineProperty(["abcdefgh", "abcdefghi"], "0", {
          get() {
            secretReads++;
            /fixture-(\d+)/.exec("left fixture-" + secretReads + " right");
            return "abcdefgh";
          },
        });
        if (warm) api.__perfInternals.knownSecretText("ordinary prose", secrets);
        let observed: ReturnType<typeof legacyRegExpState> | undefined;
        const budget: runtime.PrivacyBudget = {
          get nodes(): number {
            budgetReads++;
            observed = legacyRegExpState();
            throw new RangeError(observed[property]);
          },
          set nodes(value: number) { nodes = value; },
          text: MAX_TEXT,
          seen: new WeakSet(),
        };
        seedLegacyRegExpState();
        const result = outcome(() => api.__perfInternals.knownSecretRanges("ordinary prose", secrets, 0, budget));
        return { result, observed, secretReads, budgetReads, nodes, text: budget.text };
      };
      const secretReads = warm ? 2 : 1;
      const expectedStatics = { lastMatch: "fixture-" + secretReads, capture1: String(secretReads), leftContext: "left " };
      const expected = run(parent);
      expect(expected).toEqual({
        result: { kind: "throw", errorClass: RangeError, name: "RangeError", message: expectedStatics[property] },
        observed: expectedStatics, secretReads, budgetReads: 1, nodes: catalog.limits.max_nodes, text: MAX_TEXT,
      });
      expect(run(oracle)).toEqual(expected);
      expect(run(candidate)).toEqual(expected);
    }
  });

  test("template hoists preserve discovery inspect statics and thrown messages", () => {
    const sources = ["before https://example.invalid/?q=plain then /?q=ordinary after"];
    for (const property of legacyRegExpKeys) for (let stop = 1; stop <= 12; stop++) {
      const run = (api: typeof runtime) => {
        const observations: Array<{ characters: number; statics: ReturnType<typeof legacyRegExpState> }> = [];
        seedLegacyRegExpState();
        const result = outcome(() => api.discoverSecrets(sources, undefined, characters => {
          const statics = legacyRegExpState();
          observations.push({ characters, statics });
          if (observations.length === stop) throw new Error(statics[property]);
        }));
        return { result, observations };
      };
      const expected = run(parent);
      expect(expected.observations.length).toBeGreaterThan(0);
      expect(run(candidate)).toEqual(expected);
      expect(run(runtime)).toEqual(expected);
    }
  });
});

// Representative public fixtures copied from src/__tests__/privacy.test.ts at
// the pinned commit: explicit expected outputs prevent a vacuous paired oracle.
describe("existing public privacy representatives remain unchanged", () => {
  test.each([
    [JSON.stringify("iVBORw0KGgoAAAANSUhEUg=="), [], JSON.stringify("<omitted: 24 base64 chars>")],
    [JSON.stringify('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">payload</svg>'), [], JSON.stringify("<omitted: data URI>")],
    ["12345678", ["12345678"], "<redacted>"],
    ["https://example.invalid/?content=AQIDBA==&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor", [],
      "https://example.invalid/?content=<omitted: 8 base64 chars>&mime_type=audio%2Fpcm&mime_type=text%2Fplain&model=keep#anchor"],
  ] as Array<[string, string[], string]>)("scrubAttribute fixture %s", (value, secrets, expected) => {
    expect(runtime.scrubAttribute(value, secrets)).toBe(expected);
    expect(oracle.scrubAttribute(value, secrets)).toBe(expected);
  });
  test.each([
    ["choices.0.logprobs.content.0.token", true],
    ["secret.choices.0.logprobs.content.0.token", true],
    ["gen_ai.usage.input_tokens_details.cache_read", false],
    ["provider_client_secret_tokens", true],
  ] as Array<[string, boolean]>)("credential ancestry %s", (name, expected) => {
    expect(runtime.isCredentialName(name)).toBe(expected);
    expect(oracle.isCredentialName(name)).toBe(expected);
  });
  test("indexed hints isolate siblings and reset at text metadata boundaries", () => {
    const hints = runtime.indexPrivacyHints([["parts[0].mimeType[0]", "audio/pcm"], ["parts.0.mimeType", ["text/plain"]]]);
    expect(runtime.mediaReplacement("AQIDBA==", "parts.0.content.chunk", hints)).toBe("<omitted: 8 base64 chars>");
    expect(runtime.mediaReplacement("AQIDBA==", "parts.1.content.chunk", hints)).toBeUndefined();
    expect(runtime.mediaReplacement("spoken words", "parts.0.transcript.content", hints)).toBeUndefined();
    expect(runtime.discoverSecrets([{ secret: { input_tokens: ["credential-123456"] } }])).toContain("credential-123456");
  });
  test("plain-data walking does not invoke getters or conversions and handles cycles", () => {
    let executions = 0;
    const execute = () => { executions++; throw new Error("user code executed"); };
    const value = Object.defineProperty({ safe: "ordinary words", unsupported: { toString: execute, toJSON: execute } }, "api_key", { enumerable: true, get: execute });
    expect(runtime.scrubValue(value)).toEqual(oracle.scrubValue(value));
    expect(runtime.scrubValue(value).safe).toBe("ordinary words");
    expect(runtime.scrubValue(value).api_key).toBeUndefined();
    expect(executions).toBe(0);
    const cycle: any = { safe: "ordinary words" }; cycle.self = cycle;
    expect(runtime.scrubValue(cycle)).toEqual({ safe: "ordinary words", self: "<omitted>" });
  });
  test.each(["depth", "nodes", "text"])("public %s exhaustion stays fail-closed", kind => {
    let value: any = kind === "nodes" ? Array(16385).fill(0) : kind === "text" ? "x".repeat(MAX_TEXT + 1) : "ordinary";
    if (kind === "depth") for (let i = 0; i < 34; i++) value = [value];
    equal(() => runtime.scrubValue(value), () => oracle.scrubValue(value), "public-budget/" + kind);
    expect(() => runtime.scrubValue(value)).toThrow(/Privacy (structure|text) budget/);
  });
});
