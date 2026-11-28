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
// fi-core 1.2.2: rebase both runtime oracles onto scalar catalog v6. The
// algorithm is unchanged; audioCacheCount.test.ts verifies the new typed key.
const BASE_CATALOG_SHA256 = "2e278f5ea945d7049cbbfff0ba8c38d98cdc2f9959ed78a29fd0ba59c9ff2539";
const source = readFileSync(join(__dirname, "..", "privacy-runtime.ts"), "utf8");
const currentCatalogSource = readFileSync(join(__dirname, "..", "privacy.json"), "utf8");
// Strip each additive declaration section explicitly; neither belongs to 1.2.0.
// credentials.vocabulary (shared privacy spec v4) is an additive declaration; strip it like video/image.
let catalogSource = replaceSection(currentCatalogSource, '    "vocabulary": {\n', '    "exclusions": [\n', "");
// The v4 catalog also moved its own version number; restore the pinned one (the first line of the file).
if (!catalogSource.startsWith('{\n  "version": 4,\n')) throw new Error("Privacy oracle source boundary drift");
catalogSource = '{\n  "version": 3,\n' + catalogSource.slice('{\n  "version": 4,\n'.length);
catalogSource = replaceSection(catalogSource, '    "video": {\n', '    "audio": {\n', "");
catalogSource = replaceSection(catalogSource, '    "image": {\n', '    "audio": {\n', "");
// Linear-time credential shapes (sk-, jwt) changed two catalog regexes after this oracle was pinned.
// Restore the pinned spellings so the BASE hashes still describe the oracle's baseline.
catalogSource = catalogSource
  .replace('"\\\\bsk-[A-Za-z0-9_-]{20,}"', '"\\\\bsk-[A-Za-z0-9_-]{20,}\\\\b"')
  .replace('"\\\\beyJ[A-Za-z0-9_-]{8,}\\\\.[A-Za-z0-9_-]{8,}\\\\.[A-Za-z0-9_-]{8,}"', '"\\\\beyJ[A-Za-z0-9_-]{8,}\\\\.[A-Za-z0-9_-]{8,}\\\\.[A-Za-z0-9_-]{8,}\\\\b"');
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
// ---- Shared privacy spec v4 (credential pair rule, URL catalog pairs, vocabulary, map keys, name budgets). ----
// The runtime gained a marked section plus call-site edits after this oracle was pinned.
// restoreV4 swaps them out so the pinned reconstruction below is unchanged; extendOracleV4 then
// re-applies the SAME rules to the oracle/parent copies, whose old hot-path code stays under
// comparison. Nothing here removes a rule from the comparison.
const V4_START = "// ---- Shared privacy spec v4";
const V4_END = "// ---- End of the shared privacy spec v4 section. ----\n";
const BASE_V4_RUNTIME_SHA256 = "505b268eb4dbd3ea3dd2934c6cb951bc16a9dab646c1124630426d1de4d95983";
// Python-semantics value shapes (explicit \\b, \\s and IGNORECASE classes) replace the pre-v4 runtime's per-shape regexes. The pins
// keep the old runtime; the oracle gains the same shapes in extendOracleV4 (its jwt shape stays a plain regex).
const PY_SWAPS: [label: string, current: string, base: string][] = [
  ["structured string text in attributeText",
   "  const read = (source: string, node: unknown) => structuredText(scrubJSON(source, node, secrets, 0, newBudget(), false, false, false, incompleteURLs), node, secrets, true, false);\n  const replaced = typeof parsed === \"string\" ? knownSecretText(value, secrets) : value;\n  // The read of the string with no known secret: the URL catalog, the value shapes and the pair rule on the original text.\n  const rulesRead = () => structuredText(scrubJSON(value, parsed, [], 0, newBudget(), false, false, false, incompleteURLs), parsed, [], true, false);\n  if (replaced === value) {\n    // A known secret spelled only as the string spells it (the raw bytes write it with escapes) can take part of a name or a shape there,\n    // so the pair rule or a shape no longer reads what follows. It only adds redaction: the read of the string is joined, as the string it\n    // encodes, with the read with no known secret and with every occurrence of a known secret in the string.\n    const clean = read(value, parsed);\n    if (typeof parsed !== \"string\" || clean === JSON.stringify(replacement)) return clean;\n    // A quoted root with no known secret in its string is still joined with the read of its raw bytes with no known secret (withValueRead):\n    // an escaped quoted pair, a key block whose end line is spelled with escapes, and a shape the raw bytes end are hidden as there.\n    if (!secretIn(parsed, secrets)) return failsClosed(() => withValueRead(value, parsed, secrets, clean, incompleteURLs));\n    return failsClosed(() => {\n      const reads = [clean, rulesRead(), JSON.stringify(heldOccurrences(parsed, secrets))];\n      return withValueRead(value, parsed, secrets, decodedUnion(value, parsed, reads, []) ?? keptWithin(reads[0], reads.slice(1), parsed, value), incompleteURLs);\n    });\n  }\n  // Every read of the root runs on its own budget; where one runs out, this attribute alone is the quoted placeholder (failsClosed).\n  return withValueRead(value, parsed as string, secrets, failsClosed(() => quotedRoot(value, parsed as string, secrets, incompleteURLs, read, rulesRead)), incompleteURLs);\n}\n/** `join()`, the union of a quoted root's reads with a read it adds; where that read runs out of a privacy budget, the attribute alone fails\n * closed: it is the quoted placeholder (never the read with less hidden, and no other field or call is affected). */\nfunction failsClosed(join: () => string): string {\n  try { return join(); } catch (error) { if (error instanceof Error && /^Privacy .*budget$/.test(error.message)) return JSON.stringify(replacement); throw error; }\n}\n/** A read of a quoted root as the string it encodes (the read as it is where it is not one JSON string). */\nfunction decodedRead(read: string): string {\n  try { const node: unknown = JSON.parse(read); return typeof node === \"string\" ? node : read; } catch { return read; }\n}\n/** Quotes, backslashes and JSON whitespace: the characters a read written again as JSON can move. */\nconst QUOTING = /[\"\\\\ \\t\\r\\n]/g;\n/** `kept`, a read of a quoted root that cannot be joined by position with the reads `dropped` (each read of the string the root encodes,\n * `text`, or of its raw bytes, `value`). A dropped read that hides nothing is passed over. Where a dropped read can be placed in `text`\n * or in `value` (hiddenBy, also with a placeholder written with the quotes of the pair value it replaces), the runs it hides are known\n * by position: `kept`, read as the string it encodes, must show no 4-character window that lies wholly inside one hidden run (as spelled\n * there or with its JSON escapes read) unless the dropped read shows that window elsewhere (by keyed rolling hash, windowKeys). A window\n * that runs across the edge of a hidden run into the text around it is not checked. Where a hidden run is under 4 characters (it has no\n * window of its own), `kept` must also show no character the dropped read does not show (with its JSON escapes as written or read),\n * quotes, backslashes and JSON whitespace aside (hidesAtLeast: a subsequence test, not by position). Where it cannot be placed, `kept` must hide at least\n * every character it hides (hidesAtLeast) and show no 4 characters together that it does not show (windowsWithin). Otherwise the\n * quoted placeholder: no read is kept that shows what another read hides. Linear in the texts (one window table per read). */\nfunction keptWithin(kept: string, dropped: readonly string[], text: string, value: string): string {\n  const k = decodedRead(kept), placeholder = JSON.stringify(replacement);\n  for (const read of dropped) {\n    const d = decodedRead(read);\n    if (!PLACEHOLDERS[0].test(d)) continue;\n    // Placed as it is written, or with a placeholder written with the quotes of the pair value it replaces (SPELLED_PLACEHOLDERS).\n    const place = (source: string, output: string) => hiddenBy(source, output) ?? hiddenBy(source, output, SPELLED_PLACEHOLDERS);\n    const byText = place(text, d), by = byText ?? place(value, read), source = byText ? text : value;\n    if (!by) { if (!hidesAtLeast(k, d) || !windowsWithin(k, d)) return placeholder; continue; }\n    // What the dropped read shows, as written and with its JSON escapes read (the kept read is read as the string it encodes).\n    const shownRead = byText ? d : read;\n    // The runs the dropped read hides, as spelled in the source and with their JSON escapes read.\n    let runs = \"\", short = false;\n    for (let i = 0, j; i < source.length; i = j) {\n      for (j = i + 1; j < source.length && by.hidden[j] === by.hidden[i]; j++);\n      if (by.hidden[i]) { runs += replacement + source.slice(i, j) + replacement + unescapedRun(source.slice(i, j)); short ||= j - i < 4; }\n    }\n    // A run under 4 characters has no window of its own: the kept read must then show no character the dropped read does not show\n    // (hidesAtLeast, a subsequence test), quotes, backslashes and JSON whitespace aside (a read written again moves its quoting), with\n    // the dropped read's JSON escapes as written or read (a read written again writes \\u0071 as q).\n    if (short) {\n      const shown = k.replace(QUOTING, \"\");\n      if (!hidesAtLeast(shown, d.replace(QUOTING, \"\")) && !hidesAtLeast(shown, unescapedRun(d).replace(QUOTING, \"\"))) return placeholder;\n    }\n    // The windows of the hidden runs that the kept read shows; each must be shown by the dropped read too. The tables hold only the\n    // hidden runs' windows and those of them the kept read shows; the kept and the dropped read are each read once in order.\n    const hidden = keyTable([runs]), shown = keyTable([k], key => hidden.has(key));\n    if (shown.empty) continue;\n    for (const t of [shownRead, unescapedRun(shownRead)]) windowKeys(t, key => { shown.mark(key); return true; });\n    if (!shown.allMarked()) return placeholder;\n  }\n  return kept;\n}\n/** The set of the window keys (windowKeys) of `texts` that `keep` keeps: one open-addressing table of 8-byte slots (at least twice as many\n * as keys; the texts are read twice, to count and to fill) and one mark per slot. A key two different windows share counts as present;\n * the bases are drawn when the module loads. Linear in the texts. */\nfunction keyTable(texts: readonly string[], keep: (key: number) => boolean = () => true): { empty: boolean; has(key: number): boolean; mark(key: number): void; allMarked(): boolean } {\n  let count = 0, size = 16;\n  for (const text of texts) windowKeys(text, key => { if (keep(key)) count++; return true; });\n  while (size < 2 * count) size *= 2;\n  const table = new Float64Array(size), marks = new Uint8Array(size), mask = size - 1;\n  const slot = (key: number) => { let i = (key - Math.floor(key / size) * size) & mask; while (table[i] !== 0 && table[i] !== key) i = (i + 1) & mask; return i; };\n  for (const text of texts) windowKeys(text, key => { if (keep(key)) table[slot(key)] = key; return true; });\n  return {\n    empty: count === 0,\n    has: key => table[slot(key)] === key,\n    mark: key => { const i = slot(key); if (table[i] === key) marks[i] = 1; },\n    allMarked: () => { for (let i = 0; i < size; i++) if (table[i] !== 0 && !marks[i]) return false; return true; },\n  };\n}\n/** Calls `each` with the keyed rolling-hash key (as windowsWithin keys them; 1 or more) of every 4 characters together in each part of\n * `text` between its placeholders, until it returns false; false then. Linear. */\nfunction windowKeys(text: string, each: (key: number) => boolean): boolean {\n  const [m1, m2] = PIECE_MOD, [b1, b2] = PIECE_BASE;\n  let w1 = 1, w2 = 1;\n  for (let i = 1; i < 4; i++) { w1 = reduce(w1 * b1, m1); w2 = reduce(w2 * b2, m2); }\n  for (const part of text.split(PLACEHOLDERS[0])) {\n    let h1 = 0, h2 = 0;\n    for (let i = 0; i < part.length; i++) {\n      if (i >= 4) { const d = part.charCodeAt(i - 4); h1 = reduce(h1 + m1 * 65536 - d * w1, m1); h2 = reduce(h2 + m2 * 65536 - d * w2, m2); }\n      const c = part.charCodeAt(i);\n      h1 = reduce(h1 * b1 + c, m1); h2 = reduce(h2 * b2 + c, m2);\n      if (i >= 3 && !each(h1 * m2 + h2 + 1)) return false;\n    }\n  }\n  return true;\n}\n/** `run` with its JSON escapes (\\uXXXX, \\\" and the others) read; an escape cut by the run's end is left as it is. */\nfunction unescapedRun(run: string): string {\n  return run.replace(/\\\\(?:u([0-9a-fA-F]{4})|([\"\\\\/bfnrt]))/g, (_, hex: string | undefined, c: string | undefined) => hex ? String.fromCharCode(parseInt(hex, 16)) : ({ b: \"\\b\", f: \"\\f\", n: \"\\n\", r: \"\\r\", t: \"\\t\" } as Record<string, string>)[c!] ?? c!);\n}\n/** The read of a quoted root (a JSON text that is one string) whose raw bytes hold a known secret: the read of the string it encodes and\n * the read of the raw bytes in the reference's order, decided and joined as below. */\nfunction quotedRoot(value: string, parsed: string, secrets: readonly string[], incompleteURLs: boolean, read: (source: string, node: unknown) => string, rulesRead: () => string): string {\n  // A quoted root is read as the string it encodes; the reference reads the raw bytes in its own order: the URL catalog first, then the\n  // known secrets, longest first, on the catalog's output. Where the read of the string shows a run of a known secret more often than\n  // that read of the raw bytes does, the raw-bytes read is taken; where the text is then not one JSON string, it is the quoted\n  // placeholder. A known secret is never replaced on the raw bytes before the catalog reads them: it could take out the scheme, the\n  // userinfo '@' or the name the catalog needs, and leave the credential the catalog hides.\n  const clean = read(value, parsed);\n  let fallback: string | undefined;\n  const bytesRead = () => {\n    if (fallback !== undefined) return fallback;\n    const bytes = knownSecretText(catalogText(value, incompleteURLs, [])[0], secrets);\n    let node: unknown;\n    try { node = JSON.parse(bytes); } catch { node = undefined; }\n    return fallback = typeof node === \"string\" ? read(bytes, node) : JSON.stringify(replacement);\n  };\n  // A known secret replaced on the raw bytes can take part of a credential's name, so the pair rule no longer reads a pair that the read of\n  // the string hides; and the raw-bytes read can hide what follows a credential (the rest of a URL fragment, a flag's value) that the read\n  // of the string shows. Neither read is kept with less hidden than that: the read picked here is kept with, by position\n  // (positionalUnion), what the other read hides added where the read of the string is kept; and where the raw-bytes read is kept, what\n  // the catalog, the value shapes and the pair rule hide on the string with no known secret (a known secret only adds redaction), and every\n  // occurrence of a known secret as the raw bytes spell it (one replaced first can leave part of an overlapping one shown).\n  const kept = lessShown(clean, secrets, bytesRead), bytes = bytesRead();\n  if (bytes === clean) return clean;\n  // A union written on the raw bytes is kept one JSON string (rootString). Where a read cannot be placed in the raw bytes (the read of the\n  // string writes its escapes again as JSON writes them), the reads are joined as the strings they encode (decodedUnion), with the read with\n  // no known secret and every occurrence of a known secret as the string and as the raw bytes spell it. Where that cannot be placed either:\n  // the read of the string is kept as it is (it shows no run of a known secret more often); the raw-bytes read only where, both read as the\n  // strings they encode, it hides at least every character the read with no known secret hides and shows no 4 characters together that it\n  // does not show; otherwise the text is the quoted placeholder. A raw-bytes read that is the quoted placeholder (the raw bytes are not one\n  // JSON string once the known secrets are replaced) adds nothing to a kept read of the string.\n  const text = parsed as string, placeholder = JSON.stringify(replacement);\n  if (kept) {\n    // Every occurrence of a known secret as the raw bytes spell it is hidden whole as well: the catalog can cut one that runs from URL text\n    // into a credential, so the raw-bytes read replaces only the part the catalog left.\n    const placed = bytes === placeholder ? undefined : positionalUnion(value, clean, bytes);\n    const union = placed === undefined ? undefined : positionalUnion(value, placed, heldOccurrences(value, secrets));\n    if (union !== undefined) return rootString(value, text, union) ?? placeholder;\n    // A read that cannot be placed in the raw bytes: the reads are joined as the strings they encode, with the read with no known secret\n    // and every occurrence of a known secret as the string and as the raw bytes spell it.\n    return failsClosed(() => {\n      const reads = [clean, rulesRead(), JSON.stringify(heldOccurrences(text, secrets))], all = bytes === placeholder ? reads : [...reads, bytes];\n      return decodedUnion(value, text, all, [heldOccurrences(value, secrets)]) ?? keptWithin(clean, all.slice(1), text, value);\n    });\n  }\n  const rules = rulesRead();\n  const joined = positionalUnion(value, bytes, rules);\n  const union = joined === undefined ? undefined : positionalUnion(value, joined, heldOccurrences(value, secrets));\n  if (union !== undefined) return rootString(value, text, union) ?? placeholder;\n  const spelledUnion = bytes === placeholder ? undefined : decodedUnion(value, text, [bytes, rules, JSON.stringify(heldOccurrences(text, secrets))], [heldOccurrences(value, secrets)]);\n  if (spelledUnion !== undefined) return spelledUnion;\n  const b = decodedRead(bytes), r = decodedRead(rules);\n  return hidesAtLeast(b, r) && windowsWithin(b, r) ? bytes : JSON.stringify(replacement);\n}\n/** `result`, a read of a quoted root, joined by position with `read`, a read of its raw bytes: on the raw bytes where both can be placed\n * there (kept one JSON string, rootString), otherwise as the strings they encode (decodedUnion); where neither can be made, `result`. */\nfunction joinRead(value: string, text: string, result: string, read: string): string {\n  const placed = positionalUnion(value, result, read);\n  if (placed !== undefined) return rootString(value, text, placed) ?? JSON.stringify(replacement);\n  // `scrubValue` writes a JSON string it reads again as JSON writes it: then it is joined as the string it encodes.\n  return decodedUnion(value, text, [result], [read]) ?? decodedUnion(value, text, [result, read], []) ?? keptWithin(result, [read], text, value);\n}\n/** Whether `read` hides, beyond what `result` hides, only quotes, backslashes and JSON whitespace of the raw bytes (a pair value's escaped\n * quotes), so joining it adds no character of a value. False where either cannot be placed in `value`. Linear. */\nfunction addsOnlyQuoting(value: string, result: string, read: string): boolean {\n  const byRead = hiddenBy(value, read), byResult = byRead && hiddenBy(value, result);\n  // What `result` hides is known only where each of its kept parts has one place: a part that stands at two places counts as hidden at both.\n  if (!byRead || !byResult || !byResult.exact) return false;\n  for (let i = 0; i < value.length; i++) {\n    const c = value.charCodeAt(i);\n    if (byRead.hidden[i] && !byResult.hidden[i] && c !== 34 && c !== 92 && c !== 32 && c !== 9 && c !== 13 && c !== 10) return false;\n  }\n  return true;\n}\n/** A known secret only adds redaction to a quoted root (a JSON text that is one string) that holds one: the root never shows a character\n * that the read of its raw bytes with no known secret (`scrubValue` with none) hides. That read reads the raw bytes as the reference does:\n * a pair whose value is written with escaped quotes runs to the end of the text, a key block whose end line is spelled with escapes runs\n * to the end, and a shape ends where the raw bytes end it; a known secret that the read of the string replaced first can no longer take\n * the name or the shape that read needs. `result`, the root's read, is joined with it by position: on the raw bytes where both can be\n * placed there (kept one JSON string, rootString), otherwise as the strings they encode (decodedUnion). Where that read refuses the text\n * (a budget it runs out of), the attribute is the quoted placeholder; where neither union can be made, `result` is kept. */\nfunction withValueRead(value: string, text: string, secrets: readonly string[], result: string, incompleteURLs = false): string {\n  const placeholder = JSON.stringify(replacement);\n  if (result === placeholder || result === replacement) return result;\n  let byValue: unknown;\n  try { byValue = scrubValue(value, []); } catch { return placeholder; }\n  if (typeof byValue === \"string\" && byValue !== value) result = joinRead(value, text, result, byValue);\n  if (!secrets.length || result === placeholder) return result;\n  // The raw bytes read in the reference's order, as text: the URL catalog first, then the known secrets on its output (longest first),\n  // then the value shapes and the pair rule on that text (referenceRedactions). A known secret that ends inside a pair value's escaped\n  // closing quote leaves that value running to the end of the text there; several known secrets that each take part of a shape's or a\n  // pair's neighbourhood can leave the read of the string showing what that read hides. It is joined by position too; a budget it runs\n  // out of makes this attribute alone the quoted placeholder (failsClosed).\n  return failsClosed(() => {\n    const catalogued = catalogText(value, incompleteURLs, [])[0];\n    // Only the registered values the catalog's text holds are located (one search each); absent ones are not charged.\n    const held = secrets.filter(secret => typeof secret === \"string\" && catalogued.indexOf(secret) >= 0);\n    const known = held.length ? knownSecretRanges(catalogued, held, 0, newBudget()) : [];\n    const read = known.length ? replaceEdits(catalogued, joinRanges([...known, ...referenceRedactions(catalogued, known)])) : catalogued;\n    const joined = read === value || addsOnlyQuoting(value, result, read) ? result : joinRead(value, text, result, read);\n    // A registered value the raw bytes hold is hidden whole: where the catalog cuts one that runs from URL text into a credential, the\n    // reads above replace only the part the catalog left, so every occurrence as the raw bytes spell it is joined by position too.\n    if (joined === placeholder) return joined;\n    const spelled = heldOccurrences(value, secrets);\n    return spelled === value ? joined : joinRead(value, text, joined, spelled);\n  });\n}",
   "  return scrubJSON(value, parsed, secrets, 0, newBudget(), false, false, false, incompleteURLs);\n}"],
  ["python value shapes",
   "// Python 3 re, as the reference reads the catalog's value shapes: \\b and \\w are Unicode (a letter, a number or '_'), \\s is\n// str.isspace, and IGNORECASE folds U+017F with s, U+212A with k and U+0130/U+0131 with i. JS \\s, \\b, \\w and /i differ on\n// each, so a shape is built from explicit classes. A shape this table does not know keeps its catalog regex, unchanged.\nconst PY_WORD = String.raw`\\p{L}\\p{N}_`;\nconst PY_SPACE = String.raw`\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000`;\nconst PY_BEFORE = `(?<![${PY_WORD}])`, PY_AFTER = `(?![${PY_WORD}])`;\nconst PY_FOLD: Record<string, string> = { s: \"sS\\u017f\", k: \"kK\\u212a\", i: \"iI\\u0130\\u0131\" };\nconst pyFold = (word: string) => [...word].map(c => `[${PY_FOLD[c.toLowerCase()] ?? c.toLowerCase() + c.toUpperCase()}]`).join(\"\");\nconst PY_SHAPES: Record<string, { regex: string; flags: string; lead: string; body: string }> = {\n  \"provider-api-key\": { regex: \"\\\\bsk-[A-Za-z0-9_-]{20,}\", flags: \"\", lead: PY_BEFORE, body: \"sk-[A-Za-z0-9_-]{20,}\" },\n  \"aws-access-key\": { regex: \"\\\\b(?:AKIA|ASIA)[A-Z0-9]{16}\\\\b\", flags: \"\", lead: PY_BEFORE, body: `(?:AKIA|ASIA)[A-Z0-9]{16}${PY_AFTER}` },\n  \"authorization-value\": {\n    regex: \"\\\\b(?:Bearer|Basic)\\\\s+[A-Za-z0-9+/_=-]{8,}\", flags: \"i\", lead: PY_BEFORE,\n    body: `(?:${pyFold(\"Bearer\")}|${pyFold(\"Basic\")})[${PY_SPACE}]+[A-Za-z0-9+/_=\\\\-\\u0130\\u0131\\u017f\\u212a]{8,}`,\n  },\n  // Python's $ also holds before a final newline; the lazy body stops there.\n  \"private-key-pem\": {\n    regex: \"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\\\\s\\\\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)\", flags: \"\", lead: \"\",\n    body: \"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\\\\s\\\\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?=\\\\n?(?![\\\\s\\\\S])))\",\n  },\n};\nconst pyShape = (item: { id: string; regex: string; flags: string }) => {\n  const known = PY_SHAPES[item.id];\n  return known && known.regex === item.regex && known.flags === item.flags ? known : undefined;\n};\n// The jwt shape is scanned by replaceJwt (exactly equivalent, linear time); other shapes run as regexes.\nconst valueShapes: (RegExp | ((value: string) => string))[] = catalog.credentials.value_shapes.map(item => {\n  if (item.id === \"jwt\" && item.regex === JWT_SHAPE && item.flags === \"\") return replaceJwt;\n  const known = pyShape(item);\n  return known ? new RegExp(known.lead + known.body, \"gu\") : new RegExp(item.regex, item.flags + \"g\");\n});\n",
   "// The jwt shape is scanned by replaceJwt (exactly equivalent, linear time); other shapes run as regexes.\nconst valueShapes: (RegExp | ((value: string) => string))[] = catalog.credentials.value_shapes.map(item =>\n  item.id === \"jwt\" && item.regex === JWT_SHAPE && item.flags === \"\" ? replaceJwt : new RegExp(item.regex, item.flags + \"g\"));\n"],
  ["python word boundary helper",
   "const isRunCode = (c: number) => (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95 || c === 45;\nconst PY_WORD_CHAR = new RegExp(`[${PY_WORD}]`, \"u\");\n/** Whether the code point ending at index i is a Python \\\\w character (the word side of \\\\b). */\nfunction wordBefore(value: string, i: number): boolean {\n  if (i <= 0) return false;\n  const low = value.charCodeAt(i - 1);\n  const start = low >= 0xdc00 && low <= 0xdfff && i >= 2 && value.charCodeAt(i - 2) >= 0xd800 && value.charCodeAt(i - 2) <= 0xdbff ? i - 2 : i - 1;\n  return PY_WORD_CHAR.test(String.fromCodePoint(value.codePointAt(start)!));\n}\n",
   "const isRunCode = (c: number) => (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95 || c === 45;\n"],
  ["python word boundary in replaceJwt",
   "        if (value.charCodeAt(p) === 101 && value.charCodeAt(p + 1) === 121 && value.charCodeAt(p + 2) === 74 && (p === a ? !wordBefore(value, a) : value.charCodeAt(p - 1) === 45)",
   "        if (value.charCodeAt(p) === 101 && value.charCodeAt(p + 1) === 121 && value.charCodeAt(p + 2) === 74 && (p === a || value.charCodeAt(p - 1) === 45)"],
];
// Explicit [label, current, base] list. Each string must be found exactly once.
const V4_SWAPS: [label: string, current: string, base: string][] = [
  ["catalog vocabulary type",
   "    seed_names: string[];\n    value_shapes: { id: string; regex: string; flags: string }[];\n    vocabulary: { words: string[] };\n",
   "    seed_names: string[];\n    value_shapes: { id: string; regex: string; flags: string }[];\n"],
  ["whitespace in URL and data URI tokens",
   "  return !payload || encoded(payload) ? omitted(payload) : media.data_uri.other_replacement;\n}\n// The reference's whitespace (Python's str.isspace), not JS \\s: it adds U+001C-U+001F and U+0085 and leaves out U+FEFF.\nconst SPACE = String.raw`\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000`;\nconst DATA = String.raw`(?<![A-Za-z0-9+.\\-_:])data:[^${SPACE}\"'<>\\\\]*(?:[\\r\\n]+[A-Za-z0-9+/_=\\-]+)*`;\nconst URL_BODY = String.raw`(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^${SPACE}\"<>\\\\])+`;\nconst URL_TOKEN = String.raw`(?:https?://|(?<![^${SPACE}])[/?])${URL_BODY}`;\n",
   "  return !payload || encoded(payload) ? omitted(payload) : media.data_uri.other_replacement;\n}\nconst DATA = String.raw`(?<![A-Za-z0-9+.\\-_:])data:[^\\s\"'<>\\\\]*(?:[\\r\\n]+[A-Za-z0-9+/_=\\-]+)*`;\nconst URL_BODY = String.raw`(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\\s\"<>\\\\])+`;\nconst URL_TOKEN = String.raw`(?:https?://|(?<!\\S)[/?])${URL_BODY}`;\n"],
  ["isCredentialName",
   "export function isCredentialName(name: string, query: boolean | \"environment\" = false): boolean {\n  // A name of more than limits.max_depth components is not read: it fails closed.\n  if (typeof name === \"string\" && isPathOverBudget(name)) return true;\n  const folded = normalizePrivacyName(name);\n  if (query && query !== \"environment\" && signed.has(folded)) return true;\n  // Most prose assignments and attribute names contain no credential marker\n  // (a pattern or a vocabulary word). Reject those before path splitting/exclusions.\n  if (!patterns.some(pattern => folded.includes(pattern)) && !vocabulary(name)) return false;\n  if (query === \"environment\") return true;\n  return credentialName(name);\n}\n",
   "export function isCredentialName(name: string, query: boolean | \"environment\" = false): boolean {\n  const folded = normalizePrivacyName(name);\n  if (query && query !== \"environment\" && signed.has(folded)) return true;\n  // Most prose assignments and attribute names contain no credential marker.\n  // Reject those before path splitting/exclusions; this is only a negative gate.\n  if (!patterns.some(pattern => folded.includes(pattern))) return false;\n  if (query === \"environment\") return true;\n  const parts = components(name).map(normalizePrivacyName);\n  let exempt = false;\n  for (let i = 0; i < parts.length; i++) {\n    if (excluded(parts[i]) || contextual.some(regex => regex.test(parts.slice(0, i + 1).join(\".\")))) exempt = true;\n    else if (patterns.some(pattern => parts[i].includes(pattern))) return true;\n  }\n  return !exempt && !excluded(folded);\n}\n"],
  ["dataText",
   "const dataText = (value: string, framed = false): string => (framed ? dataURI(value) : undefined) ?? substituteData(value);\n\n",
   "const dataText = (value: string, framed = false): string => (framed ? dataURI(value) : undefined) ?? value.replace(new RegExp(DATA, \"gi\"), original => dataURI(original) ?? original);\n\n"],
  ["indexPrivacyHints",
   "export function indexPrivacyHints(items: Iterable<readonly [string, unknown]>, path = \"\", previous: PrivacyHints = new Map(), valueLimit = Infinity): PrivacyHints {\n  return hintIndex(items, path, previous, valueLimit, false);\n}\n/** budgeted: the scrub reads each hint's full name through the path budget (an over-budget name exports the placeholder). */\nfunction hintIndex(items: Iterable<readonly [string, unknown]>, path: string, previous: PrivacyHints, valueLimit: number, budgeted: boolean): PrivacyHints {\n  const hints = new Map(previous);\n  for (const [name, value] of items) {\n    if (budgeted && (typeof value === \"string\" || (Array.isArray(value) && value.some(item => typeof item === \"string\")))) pathBudget(path ? `${path}.${name}` : name);\n    const next = hint(name, value, valueLimit);\n",
   "export function indexPrivacyHints(items: Iterable<readonly [string, unknown]>, path = \"\", previous: PrivacyHints = new Map(), valueLimit = Infinity): PrivacyHints {\n  const hints = new Map(previous);\n  for (const [name, value] of items) {\n    const next = hint(name, value, valueLimit);\n"],
  ["urlValue",
   "function urlValue(original: string, incomplete = false): string {\n  return framedUrl(original, incomplete)[0];\n}\n",
   "function urlValue(original: string, incomplete = false): string {\n  const { base, question, pairs, hash, fragment } = queryParts(original);\n  // A truncated query may end in a syntactically valid MIME prefix. Without\n  // complete context no query MIME can attest to a non-media value.\n  const hints = indexPrivacyHints(pairs.filter(pair => pair.equals).map(pair => [pair.decodedName, pair.decodedValue]), \"\", noHints, incomplete ? 0 : Infinity);\n  const query = pairs.map(pair => {\n    let value = pair.value;\n    if (pair.equals) {\n      if (isCredentialName(pair.decodedName, true)) value = replacement;\n      else {\n        let clean = mediaReplacement(pair.decodedValue, pair.decodedName, hints, true, false, false, incomplete);\n        if (clean === undefined) { const text = dataText(pair.decodedValue, true); if (text !== pair.decodedValue) clean = text; }\n        if (clean !== undefined) value = clean;\n      }\n    }\n    return pair.name + pair.equals + value;\n  }).join(\"&\");\n  return dataText(base.replace(/^(https?:\\/\\/)[^\\s/@]+:[^\\s/@]+@/i, `$1${replacement}@`)) + question + query + hash + dataText(fragment);\n}\n"],
  ["jsonNumbers hints",
   "          hints = hintIndex(items, path, hints, Infinity, true);\n          sourceHints = hintIndex(items, path, sourceHints, Infinity, true);\n",
   "          hints = indexPrivacyHints(items, path, hints);\n          sourceHints = indexPrivacyHints(items, path, sourceHints);\n"],
  ["scrubValue",
   "export function scrubValue(value: any, secrets: readonly string[] = [], path = \"\", depth = 0, inheritedAudio = false, budget: PrivacyBudget = newBudget(), hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, incomplete = false, ambiguousBase64 = true): any {\n  return pathBudgeted(() => walkValue(value, secrets, path, depth, inheritedAudio, budget, hints, framed, inheritedMedia, incomplete, ambiguousBase64));\n}\n/** A name (a path, a map key, a pair name in text) of more than max_depth components exports the whole value as the placeholder. */\nfunction pathBudgeted<T>(scrub: () => T): T | string {\n  try { return scrub(); } catch (error) { if (error instanceof PrivacyPathBudget) return replacement; throw error; }\n}\n",
   "export function scrubValue(value: any, secrets: readonly string[] = [], path = \"\", depth = 0, inheritedAudio = false, budget: PrivacyBudget = newBudget(), hints: PrivacyHints = noHints, framed = true, inheritedMedia = false, incomplete = false, ambiguousBase64 = true): any {\n  return walkValue(value, secrets, path, depth, inheritedAudio, budget, hints, framed, inheritedMedia, incomplete, ambiguousBase64);\n}\n"],
  ["walkValue",
   "function walkValue(value: any, secrets: readonly string[], path: string, depth: number, inheritedAudio: boolean, budget: PrivacyBudget, hints: PrivacyHints, framed: boolean, inheritedMedia: boolean, incomplete: boolean, ambiguousBase64: boolean, policy?: ResponsePolicy, exemptToken = false, numbers?: JsonNumbers, numericLexeme?: string): any {\n  // The name path budget first: a path of more than limits.max_depth components exports the whole value as the placeholder,\n  // whatever the depth or node budget says.\n  pathBudget(path);\n  charge(budget, depth);\n  if (credentialName(path) && !exemptToken) return replacement;\n  if (value && typeof value === \"object\") {\n    if (budget.seen.has(value)) return \"<omitted>\";\n    budget.seen.add(value);\n    if (!Array.isArray(value)) {\n      if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return typeof value;\n      const items = dataEntries(value);\n      if (items.length > budget.nodes) throw new Error(\"Privacy structure budget\");\n      budget.text -= items.reduce((n, [name]) => n + name.length, 0); if (budget.text < 0) throw new Error(\"Privacy text budget\");\n      hints = hintIndex(items, path, hints, Infinity, true);\n      const token = policy?.tokenParents.has(value) ? own(value, \"token\") : undefined;\n      const cleanToken = typeof token === \"string\"\n        ? walkValue(token, secrets, \"\", depth + 1, false, budget, noHints, true, false, incomplete, ambiguousBase64) : undefined;\n      const maskedBytes = typeof token === \"string\" && (cleanToken !== token || policy!.textPolicy(cleanToken) !== token);\n      const numericOutput = new Map<string, string>();\n      const entries = new Map<string, unknown>();\n      for (const [name, item] of items) {\n        // Token bytes are another encoding of the same value, not harmless metadata.\n        const masked = name === \"logprobs\" && policy?.maskedChoices.has(value) || policy?.maskedText.get(value)?.has(name) || name === \"bytes\" && maskedBytes;\n        const exempt = name === \"token\" && typeof item === \"string\" && !!policy?.tokenParents.has(value);\n        const lexeme = numbers?.input.get(value)?.get(name);\n        const clean = masked ? replacement : walkValue(item, secrets, path ? `${path}.${name}` : name, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, exempt, numbers, lexeme);\n        // A key goes through the export text rules of a framed string value.\n        const cleanName = mapKey(name, secrets, numbers?.incompleteURLs);\n        // Two keys that are one after scrubbing: one key is kept, in the first one's place, and its value is the placeholder.\n        if (entries.has(cleanName)) { entries.set(cleanName, replacement); numericOutput.delete(cleanName); continue; }\n        if (typeof clean === \"number\" && lexeme !== undefined) numericOutput.set(cleanName, lexeme);\n        entries.set(cleanName, clean);\n      }\n      const result = Object.fromEntries(entries);\n      numbers?.output.set(result, numericOutput);\n      return result;\n    }\n    const length = Object.getOwnPropertyDescriptor(value, \"length\")!.value;\n    if (length > budget.nodes) throw new Error(\"Privacy structure budget\");\n    const items = Array.from({ length }, (_, i) => { const d = Object.getOwnPropertyDescriptor(value, String(i)); return d && \"value\" in d ? d.value : undefined; });\n    const clean = replaceMedia(items, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, true, numbers?.payloadRole);\n    if (clean !== undefined) return clean;\n    const numericOutput = new Map<string, string>();\n    const result = items.map((item, i) => {\n      const lexeme = numbers?.input.get(value)?.get(String(i));\n      const clean = walkValue(item, secrets, `${path}.${i}`, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, false, numbers, lexeme);\n      if (typeof clean === \"number\" && lexeme !== undefined) numericOutput.set(String(i), lexeme);\n      return clean;\n    });\n    numbers?.output.set(result, numericOutput);\n    return result;\n  }\n  if (typeof value === \"string\") {\n    budget.text -= value.length; if (budget.text < 0) throw new Error(\"Privacy text budget\");\n    if (wholeSecret(value, secrets)) return replacement;\n    const clean = replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64, numbers?.payloadRole); if (clean !== undefined) return clean;\n    // A text that begins as a JSON object or array and is not one JSON value: the placeholder when it repeats a member name or holds\n    // a credential name (version 6, round 3); any other such text takes the text path.\n    const opening = jsonTextStart(value);\n    if (value[opening] === \"{\" || value[opening] === \"[\") {\n      let one = false;\n      try { JSON.parse(value); one = true; } catch { /* not one JSON value */ }\n      if (!one && unreadableJSON(value, opening, secrets, depth, budget, context(path, hints, inheritedMedia, inheritedAudio), incomplete)) return replacement;\n    }\n    // JSON tool arguments are independently framed. Share budgets across nested encodings.\n    if (/^\\s*[\\[{\"]/.test(value)) {\n      let parsed: unknown; let valid = false;\n      try { parsed = JSON.parse(value); valid = true; } catch { /* Prose is handled below. */ }\n      // The reference reads only an object or an array as JSON: a string literal, number or keyword is read as raw text at the depth of\n      // the value that holds it, with its quotes and backslashes as they stand.\n      if (valid && (parsed === null || typeof parsed !== \"object\")) valid = false;\n      if (valid) {\n        const ctx = context(path, hints, inheritedMedia, inheritedAudio);\n        // Source-number origins and budgets remain private to this JSON owner.\n        // Never reprocess complete JSON as prose or match a rounded numeric alias.\n        const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;\n        return structuredText(scrubJSON(value, parsed, secrets, depth + 1, budget, ctx.audio, ctx.media, incomplete, numbers?.incompleteURLs, payloadRole), parsed, secrets, framed, incomplete);\n      }\n    }\n    const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;\n    // Text is read at the depth of the value that holds it, as the reference reads a string leaf: only a container or a parsed JSON text\n    // goes one level deeper.\n    return scrubEmbeddedText(value, secrets, depth, budget, framed, incomplete, numbers?.incompleteURLs, payloadRole);\n  }\n  const clean = replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64, numbers?.payloadRole);\n  if (clean !== undefined) {\n    // Restored numeric media classification must not preempt the existing\n    // source-aware secret repair, including original JSON numeric lexemes.\n    if (typeof value === \"number\") {\n      const raw = numericLexeme ?? String(value), masked = knownSecretText(raw, secrets);\n      if (masked !== raw) return masked;\n    }\n    return clean;\n  }\n  if (typeof value === \"number\") {\n    const raw = numericLexeme ?? String(value), clean = knownSecretText(raw, secrets);\n    if (clean !== raw) return clean;\n    // Valid JSON may contain overflowing or rounded numbers. Its source owner\n    // re-emits the original token; native numbers have only canonical spelling.\n    return numericLexeme !== undefined || Number.isFinite(value) ? value : String(value);\n  }\n  return value == null || typeof value === \"boolean\" ? value : typeof value;\n}\n",
   "function walkValue(value: any, secrets: readonly string[], path: string, depth: number, inheritedAudio: boolean, budget: PrivacyBudget, hints: PrivacyHints, framed: boolean, inheritedMedia: boolean, incomplete: boolean, ambiguousBase64: boolean, policy?: ResponsePolicy, exemptToken = false, numbers?: JsonNumbers, numericLexeme?: string): any {\n  charge(budget, depth);\n  if (isCredentialName(path) && !exemptToken) return replacement;\n  if (value && typeof value === \"object\") {\n    if (budget.seen.has(value)) return \"<omitted>\";\n    budget.seen.add(value);\n    if (!Array.isArray(value)) {\n      if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return typeof value;\n      const items = dataEntries(value);\n      if (items.length > budget.nodes) throw new Error(\"Privacy structure budget\");\n      budget.text -= items.reduce((n, [name]) => n + name.length, 0); if (budget.text < 0) throw new Error(\"Privacy text budget\");\n      hints = indexPrivacyHints(items, path, hints);\n      const token = policy?.tokenParents.has(value) ? own(value, \"token\") : undefined;\n      const cleanToken = typeof token === \"string\"\n        ? walkValue(token, secrets, \"\", depth + 1, false, budget, noHints, true, false, incomplete, ambiguousBase64) : undefined;\n      const maskedBytes = typeof token === \"string\" && (cleanToken !== token || policy!.textPolicy(cleanToken) !== token);\n      const numericOutput = new Map<string, string>();\n      const result = Object.fromEntries(items.map(([name, item]) => {\n        // Token bytes are another encoding of the same value, not harmless metadata.\n        const masked = name === \"logprobs\" && policy?.maskedChoices.has(value) || policy?.maskedText.get(value)?.has(name) || name === \"bytes\" && maskedBytes;\n        const exempt = name === \"token\" && typeof item === \"string\" && !!policy?.tokenParents.has(value);\n        const cleanName = text(name, secrets, false, numbers?.incompleteURLs), lexeme = numbers?.input.get(value)?.get(name);\n        const clean = masked ? replacement : walkValue(item, secrets, path ? `${path}.${name}` : name, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, exempt, numbers, lexeme);\n        // A redacted key can collide with another key. Match Object.fromEntries'\n        // last-value-wins result, not the numeric origin of an overwritten value.\n        numericOutput.delete(cleanName);\n        if (typeof clean === \"number\" && lexeme !== undefined) numericOutput.set(cleanName, lexeme);\n        return [cleanName, clean];\n      }));\n      numbers?.output.set(result, numericOutput);\n      return result;\n    }\n    const length = Object.getOwnPropertyDescriptor(value, \"length\")!.value;\n    if (length > budget.nodes) throw new Error(\"Privacy structure budget\");\n    const items = Array.from({ length }, (_, i) => { const d = Object.getOwnPropertyDescriptor(value, String(i)); return d && \"value\" in d ? d.value : undefined; });\n    const clean = replaceMedia(items, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, true, numbers?.payloadRole);\n    if (clean !== undefined) return clean;\n    const numericOutput = new Map<string, string>();\n    const result = items.map((item, i) => {\n      const lexeme = numbers?.input.get(value)?.get(String(i));\n      const clean = walkValue(item, secrets, `${path}.${i}`, depth + 1, inheritedAudio, budget, hints, true, inheritedMedia, incomplete, true, policy, false, numbers, lexeme);\n      if (typeof clean === \"number\" && lexeme !== undefined) numericOutput.set(String(i), lexeme);\n      return clean;\n    });\n    numbers?.output.set(result, numericOutput);\n    return result;\n  }\n  if (typeof value === \"string\") {\n    budget.text -= value.length; if (budget.text < 0) throw new Error(\"Privacy text budget\");\n    if (wholeSecret(value, secrets)) return replacement;\n    const clean = replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64, numbers?.payloadRole); if (clean !== undefined) return clean;\n    // JSON tool arguments are independently framed. Share budgets across nested encodings.\n    if (/^\\s*[\\[{\"]/.test(value)) {\n      let parsed: unknown; let valid = false;\n      try { parsed = JSON.parse(value); valid = true; } catch { /* Prose is handled below. */ }\n      if (valid) {\n        const ctx = context(path, hints, inheritedMedia, inheritedAudio);\n        // Source-number origins and budgets remain private to this JSON owner.\n        // Never reprocess complete JSON as prose or match a rounded numeric alias.\n        const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;\n        return scrubJSON(value, parsed, secrets, depth + 1, budget, ctx.audio, ctx.media, incomplete, numbers?.incompleteURLs, payloadRole);\n      }\n    }\n    const payloadRole = !!numbers?.payloadRole || context(path, hints, inheritedMedia, inheritedAudio, true).payload;\n    return scrubEmbeddedText(value, secrets, depth + 1, budget, framed, incomplete, numbers?.incompleteURLs, payloadRole);\n  }\n  const clean = replaceMedia(value, path, hints, framed, inheritedMedia, inheritedAudio, incomplete, ambiguousBase64, numbers?.payloadRole);\n  if (clean !== undefined) {\n    // Restored numeric media classification must not preempt the existing\n    // source-aware secret repair, including original JSON numeric lexemes.\n    if (typeof value === \"number\") {\n      const raw = numericLexeme ?? String(value), masked = knownSecretText(raw, secrets);\n      if (masked !== raw) return masked;\n    }\n    return clean;\n  }\n  if (typeof value === \"number\") {\n    const raw = numericLexeme ?? String(value), clean = knownSecretText(raw, secrets);\n    if (clean !== raw) return clean;\n    // Valid JSON may contain overflowing or rounded numbers. Its source owner\n    // re-emits the original token; native numbers have only canonical spelling.\n    return numericLexeme !== undefined || Number.isFinite(value) ? value : String(value);\n  }\n  return value == null || typeof value === \"boolean\" ? value : typeof value;\n}\n"],
  ["scrubEmbeddedText",
   "function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false, nested = false): string {\n  if (typeof value !== \"string\" || !secretIn(value, secrets)) return embeddedText(value, secrets, depth, budget, framed, incomplete, incompleteURLs, payloadRole, nested);\n  return onlyAdds(value, budget, read => embeddedText(value, secrets, depth, read!, framed, incomplete, incompleteURLs, payloadRole, nested));\n}\n/** Whether the catalog joins the forced spans (plainRuns) into its union, and how many it joined while `onlyAdds` reads a text. */\nlet forcing = true, forcedCount = 0, deciding = false;\n/** Whether every character `a` shows is one `b` shows too: what is left of `a` without its placeholders is a subsequence of what is\n * left of `b` (both are the same text with parts replaced). Linear. */\nfunction hidesAtLeast(a: string, b: string): boolean {\n  const x = a.split(replacement).join(\"\"), y = b.split(replacement).join(\"\");\n  let j = 0;\n  for (let i = 0; i < x.length; i++, j++) { while (j < y.length && y[j] !== x[i]) j++; if (j >= y.length) return false; }\n  return true;\n}\n/** The two moduli (each under 2^26, so every product stays exact in a double) and the bases of the rolling hash `windowsWithin` uses. The\n * bases are drawn when the module loads, so a text cannot be built in advance to make its windows collide with a piece. */\nconst PIECE_MOD = [67108859, 67108837] as const;\nconst PIECE_BASE = PIECE_MOD.map(mod => 65537 + Math.floor(Math.random() * (mod - 2 * 65537)));\n/** `x` modulo `m`, for 0 <= x < 2^53 (no floating remainder). */\nconst reduce = (x: number, m: number) => { const r = x - Math.floor(x / m) * m; return r < 0 ? r + m : r >= m ? r - m : r; };\n/** Whether every 4 characters together in a kept part of `a` (a part between its placeholders, PLACEHOLDERS) are 4 characters together\n * in a kept part of `b`: `a` shows no run of 4 that `b` does not show. Each window of `b` is put in a table by its keyed rolling hash (the\n * bases are drawn when the module loads), and each window of `a` is looked up there. Linear in both texts, with one 8-byte slot per\n * window of `b` (and as many again free). A hash that two different windows share counts as shown; the bases are not known in advance. */\nfunction windowsWithin(a: string, b: string): boolean {\n  const [m1, m2] = PIECE_MOD, [b1, b2] = PIECE_BASE;\n  let w1 = 1, w2 = 1;\n  for (let i = 1; i < 4; i++) { w1 = reduce(w1 * b1, m1); w2 = reduce(w2 * b2, m2); }\n  // Calls `each` with the key (1 or more) of each window of each kept part of `text`, until it returns false.\n  const windows = (text: string, each: (key: number) => boolean) => {\n    for (const part of text.split(PLACEHOLDERS[0])) {\n      let h1 = 0, h2 = 0;\n      for (let i = 0; i < part.length; i++) {\n        if (i >= 4) { const d = part.charCodeAt(i - 4); h1 = reduce(h1 + m1 * 65536 - d * w1, m1); h2 = reduce(h2 + m2 * 65536 - d * w2, m2); }\n        const c = part.charCodeAt(i);\n        h1 = reduce(h1 * b1 + c, m1); h2 = reduce(h2 * b2 + c, m2);\n        if (i >= 3 && !each(h1 * m2 + h2 + 1)) return false;\n      }\n    }\n    return true;\n  };\n  let count = 0;\n  windows(b, () => { count++; return true; });\n  let size = 16;\n  while (size < 2 * count) size *= 2;\n  const table = new Float64Array(size), mask = size - 1;\n  const slot = (key: number) => { let i = (key - Math.floor(key / size) * size) & mask; while (table[i] !== 0 && table[i] !== key) i = (i + 1) & mask; return i; };\n  windows(b, key => { table[slot(key)] = key; return true; });\n  return windows(a, key => table[slot(key)] === key);\n}\n/** The first index at or after `from` where `pattern` (not empty) occurs in `text`, or -1. Knuth-Morris-Pratt: linear in the text\n * read and the pattern. */\nfunction indexFrom(text: string, pattern: string, from: number): number {\n  const border = new Int32Array(pattern.length);\n  for (let i = 1, k = 0; i < pattern.length; i++) {\n    while (k > 0 && pattern.charCodeAt(i) !== pattern.charCodeAt(k)) k = border[k - 1];\n    if (pattern.charCodeAt(i) === pattern.charCodeAt(k)) k++;\n    border[i] = k;\n  }\n  for (let i = from, k = 0; i < text.length; i++) {\n    while (k > 0 && text.charCodeAt(i) !== pattern.charCodeAt(k)) k = border[k - 1];\n    if (text.charCodeAt(i) === pattern.charCodeAt(k)) k++;\n    if (k === pattern.length) return i + 1 - k;\n  }\n  return -1;\n}\n/** The last index at or before `end - pattern.length` where `pattern` (not empty) occurs in `text`, or -1: indexFrom read backwards. */\nfunction indexBefore(text: string, pattern: string, end: number): number {\n  const size = pattern.length, border = new Int32Array(size);\n  // The pattern read from its last character to its first.\n  for (let i = 1, k = 0; i < size; i++) {\n    while (k > 0 && pattern.charCodeAt(size - 1 - i) !== pattern.charCodeAt(size - 1 - k)) k = border[k - 1];\n    if (pattern.charCodeAt(size - 1 - i) === pattern.charCodeAt(size - 1 - k)) k++;\n    border[i] = k;\n  }\n  for (let i = end - 1, k = 0; i >= 0; i--) {\n    while (k > 0 && text.charCodeAt(i) !== pattern.charCodeAt(size - 1 - k)) k = border[k - 1];\n    if (text.charCodeAt(i) === pattern.charCodeAt(size - 1 - k)) k++;\n    if (k === size) return i;\n  }\n  return -1;\n}\n/** The placeholders a read writes: the credential placeholder and the omissions of media, data URIs and incomplete audio; and the same\n * with the quotes (or JSON-escaped quotes) a read writes around a placeholder that replaces a value to the end of the text. */\nconst PLACEHOLDER_SOURCE = replacement.replace(/[.*+?^${}()|[\\]\\\\]/g, \"\\\\$&\") + \"|<omitted(?:: [^<>\\\\n]*)?>\";\nconst PLACEHOLDERS = [new RegExp(PLACEHOLDER_SOURCE), new RegExp(`(?:\\\\\\\\?\")?(?:${PLACEHOLDER_SOURCE})(?:\\\\\\\\?\")?`)];\n/** The same, and a placeholder written with the single quotes (or JSON-escaped double quotes) of the pair value it replaces: the forms\n * the read of the raw bytes with no known secret writes (`'<redacted>'` for `\\\\t\\\\\"value\\\\\"`). */\nconst SPELLED_PLACEHOLDERS = [...PLACEHOLDERS, new RegExp(`(?:\\\\\\\\?[\"'])?(?:${PLACEHOLDER_SOURCE})(?:\\\\\\\\?[\"'])?`)];\n/** Which characters of `original` a read hides, where its `output` is `original` with parts (each of one or more characters) replaced\n * by placeholders (PLACEHOLDERS; where that does not match, a placeholder with the quotes around it): 1 for a hidden character.\n * Undefined where the output is not of that form (a read that wrote anything else). Each kept part is placed at its first place after\n * the previous one and, reading backwards, at its last place before the next one; every way of reading the output places it between\n * the two. A character counts as shown only where the part covers it at both places, so it is shown however the output is read; `exact`\n * where each part has one place (the mask is then what the read hid). Linear. */\nfunction hiddenBy(original: string, output: string, forms: readonly RegExp[] = PLACEHOLDERS): { hidden: Uint8Array; exact: boolean } | undefined {\n  const n = original.length;\n  for (const placeholders of forms) {\n    const parts = output.split(placeholders);\n    if (parts.length === 1) { if (output === original) return { hidden: new Uint8Array(n), exact: true }; continue; }\n    const first = placedParts(original, parts), last = first && lastPlacedParts(original, parts);\n    if (!first || !last) continue;\n    const hidden = new Uint8Array(n).fill(1);\n    let exact = true;\n    for (let k = 0; k < parts.length; k++) {\n      const size = parts[k].length;\n      if (!size) continue;\n      if (last[k] !== first[k]) exact = false;\n      hidden.fill(0, last[k], first[k] + size);\n    }\n    return { hidden, exact };\n  }\n  return undefined;\n}\n/** Where each kept part of a read's output stands in `original`, each at its last place before the next one (at least one hidden\n * character between two), the first at the start and the last at the end; undefined where there is no such place. */\nfunction lastPlacedParts(original: string, parts: readonly string[]): number[] | undefined {\n  const last = parts.length - 1, at: number[] = new Array(parts.length);\n  if (!original.endsWith(parts[last])) return undefined;\n  let start = at[last] = original.length - parts[last].length;\n  for (let k = last - 1; k > 0; k--) {\n    const place = parts[k] ? indexBefore(original, parts[k], start - 1) : start - 1;\n    if (place < 0) return undefined;\n    at[k] = start = place;\n  }\n  if (start < parts[0].length + 1 || !original.startsWith(parts[0])) return undefined;\n  at[0] = 0;\n  return at;\n}\n/** Where each kept part of a read's output stands in `original`, each at its first place after the previous one (at least one hidden\n * character between two), the first at the start and the last at the end; undefined where there is no such place. */\nfunction placedParts(original: string, parts: readonly string[]): number[] | undefined {\n  const last = parts.length - 1, at: number[] = [0];\n  if (!original.startsWith(parts[0])) return undefined;\n  let end = parts[0].length;\n  for (let k = 1; k < last; k++) {\n    const next = parts[k] ? indexFrom(original, parts[k], end + 1) : end + 1;\n    if (next < 0 || next > original.length) return undefined;\n    at.push(next); end = next + parts[k].length;\n  }\n  const start = original.length - parts[last].length;\n  if (start < end + 1 || !original.endsWith(parts[last])) return undefined;\n  at.push(start);\n  return at;\n}\n/** Two reads of `original` that each only replace parts of it with placeholders (hiddenBy), decided by position: `first` where it hides\n * every character `second` hides and its own hidden characters are known exactly (each kept part has one place), `second` where the same\n * holds the other way, and otherwise `original` written once with the union of what either hides replaced (touching parts joined), a\n * character counted as shown by a read only where every way of reading its output shows it. So the result shows no character either read\n * hides. Undefined where a read wrote anything else. Linear. */\nfunction positionalUnion(original: string, first: string, second: string): string | undefined {\n  const bySecond = hiddenBy(original, second), byFirst = bySecond && hiddenBy(original, first);\n  if (!bySecond || !byFirst) return undefined;\n  const a = bySecond.hidden, b = byFirst.hidden;\n  let firstMore = true, secondMore = true;\n  for (let i = 0; i < original.length; i++) { if (a[i] > b[i]) firstMore = false; else if (b[i] > a[i]) secondMore = false; }\n  if (firstMore && byFirst.exact) return first;\n  if (secondMore && bySecond.exact) return second;\n  // Neither read is known to hide all that the other hides: one rewrite of the original with what either hides replaced.\n  let out = \"\";\n  for (let i = 0, j; i < original.length; i = j) {\n    const hide = a[i] | b[i];\n    for (j = i + 1; j < original.length && (a[j] | b[j]) === hide; j++);\n    out += hide ? replacement : original.slice(i, j);\n  }\n  return out;\n}\n/** `value` written once with every occurrence of each known secret as `value` spells it replaced, overlapping and touching occurrences\n * joined into one placeholder. One Knuth-Morris-Pratt pass per secret the text holds (one native search passes over the others) and one\n * pass to write: linear in the text and the secrets. */\nfunction occurrencesHidden(value: string, secrets: readonly string[]): string {\n  const n = value.length, cover = new Int32Array(n + 1);\n  for (const secret of prepareSecrets(secrets)) {\n    // A secret the text does not hold is passed over with one native search, as knownSecretText does.\n    if (typeof secret !== \"string\" || !secret || value.indexOf(secret) < 0) continue;\n    const size = secret.length, border = new Int32Array(size);\n    for (let i = 1, k = 0; i < size; i++) {\n      while (k > 0 && secret.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];\n      if (secret.charCodeAt(i) === secret.charCodeAt(k)) k++;\n      border[i] = k;\n    }\n    for (let i = 0, k = 0; i < n; i++) {\n      while (k > 0 && value.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];\n      if (value.charCodeAt(i) === secret.charCodeAt(k)) k++;\n      if (k === size) { cover[i + 1 - size]++; cover[i + 1]--; k = border[k - 1]; }\n    }\n  }\n  let out = \"\", depth = 0;\n  for (let i = 0, j; i < n; i = j) {\n    depth += cover[i];\n    const hide = depth > 0;\n    for (j = i + 1; j < n && (depth + cover[j] > 0) === hide; j++) depth += cover[j];\n    out += hide ? replacement : value.slice(i, j);\n  }\n  return out;\n}\n/** occurrencesHidden for the joins that hide whole every registered value a quoted root holds, within a work bound: where the\n * registered values the text holds times its length pass 8 times its length plus 2^20 (the passes beyond 8 would read more than 2^20\n * characters), every character counts as\n * hidden, so the attribute alone fails closed as the quoted placeholder. One native search per registered value, then as\n * occurrencesHidden. */\nfunction heldOccurrences(value: string, secrets: readonly string[]): string {\n  let held = 0;\n  for (const secret of prepareSecrets(secrets)) if (typeof secret === \"string\" && secret && value.indexOf(secret) >= 0 && ++held * value.length > 8 * value.length + (1 << 20)) return replacement;\n  return occurrencesHidden(value, secrets);\n}\n/** Where each character of the string a quoted root encodes is spelled in the root (`value`, a JSON text that is one string with only JSON\n * whitespace around it): `start[i]` is the first raw index of character i and `start[i + 1]` the end of its spelling (one character, or an\n * escape of 2 or 6); `open` and `close` are the indexes of the root's quotes. Undefined where `value` is not of that form. Linear. */\nfunction rootSpelling(value: string, text: string): { start: Int32Array; open: number; close: number } | undefined {\n  let open = 0, close = value.length - 1;\n  while (open < value.length && value[open] !== '\"') open++;\n  while (close > open && value[close] !== '\"') close--;\n  if (close <= open) return undefined;\n  const start = new Int32Array(text.length + 1);\n  let k = 0;\n  for (let i = open + 1; i < close; k++) {\n    if (k >= text.length) return undefined;\n    start[k] = i;\n    i += value[i] !== \"\\\\\" ? 1 : value[i + 1] === \"u\" ? 6 : 2;\n  }\n  if (k !== text.length) return undefined;\n  start[k] = close;\n  return { start, open, close };\n}\n/** Which characters of the string a quoted root encodes are hidden where `hidden` marks raw characters of the root (hiddenBy): those whose\n * spelling holds a marked character, so an escape a read hides any part of is hidden whole. Linear. */\nfunction spelledHidden(spelling: { start: Int32Array }, size: number, hidden: Uint8Array): Uint8Array {\n  const mask = new Uint8Array(size);\n  for (let i = 0; i < size; i++) for (let r = spelling.start[i]; r < spelling.start[i + 1]; r++) if (hidden[r]) { mask[i] = 1; break; }\n  return mask;\n}\n/** The characters 0..mask.length written once, runs `mask` marks replaced (touching ones joined) and the others written by `write`. */\nfunction writeMasked(mask: Uint8Array, write: (from: number, to: number) => string): string {\n  let out = \"\";\n  for (let i = 0, j; i < mask.length; i = j) {\n    for (j = i + 1; j < mask.length && mask[j] === mask[i]; j++);\n    out += mask[i] ? replacement : write(i, j);\n  }\n  return out;\n}\n/** Reads of a quoted root decided on the string it encodes (`text`), where they cannot be placed in the root's raw bytes: each read that\n * is a JSON string is read as the string it encodes, each of `spelled` (reads that only replace parts of the raw bytes, a placeholder\n * written with the quotes of what it replaces, SPELLED_PLACEHOLDERS) by where its hidden bytes spell characters of `text`, and the union\n * of what all of them hide by position (positionalUnion) is written once as a JSON string, with the whitespace the first read writes\n * around it (the first read as it is, where it hides all the others hide). Undefined where a read is neither, or cannot be placed in\n * `text`. Linear in the root and the reads. */\nfunction decodedUnion(value: string, text: string, reads: readonly string[], spelled: readonly string[]): string | undefined {\n  let union: string | undefined, first: string | undefined;\n  const join = (read: string) => { union = union === undefined ? hiddenBy(text, read) && read : positionalUnion(text, union, read); return union !== undefined; };\n  for (const read of reads) {\n    let node: unknown;\n    try { node = JSON.parse(read); } catch { return undefined; }\n    if (typeof node !== \"string\" || !join(node)) return undefined;\n    first ??= node;\n  }\n  if (spelled.length) {\n    const spelling = rootSpelling(value, text);\n    if (!spelling) return undefined;\n    for (const read of spelled) {\n      const by = hiddenBy(value, read, SPELLED_PLACEHOLDERS);\n      if (!by || !join(writeMasked(spelledHidden(spelling, text.length, by.hidden), (i, j) => text.slice(i, j)))) return undefined;\n    }\n  }\n  if (union === undefined) return undefined;\n  // The first read is kept as it is written where it hides all the others hide; otherwise the union is written with its whitespace.\n  if (union === first) return reads[0];\n  const space = /^[ \\t\\r\\n]*/.exec(reads[0])![0], after = /[ \\t\\r\\n]*$/.exec(reads[0])![0];\n  return space + JSON.stringify(union) + after;\n}\n/** A union of reads of a quoted root, written on its raw bytes, kept one JSON string: where it is not one (it took the root's opening or\n * closing quote into a placeholder, or a part of an escape), it is written again with the root's quotes and the whitespace around them\n * shown, and every escape it hides any part of hidden whole. Undefined where it cannot be placed in the root. Linear. */\nfunction rootString(value: string, text: string, union: string): string | undefined {\n  let string = false;\n  try { string = typeof JSON.parse(union) === \"string\"; } catch { /* Written again below. */ }\n  const spelling = rootSpelling(value, text), by = spelling && hiddenBy(value, union);\n  if (!spelling || !by) return string ? union : undefined;\n  // A union that is one JSON string is kept as it is unless it hides part of an escape (the backslash of \\u0050 and not its digits).\n  if (string && !partEscapeHidden(spelling, text.length, by.hidden)) return union;\n  const mask = spelledHidden(spelling, text.length, by.hidden), start = spelling.start;\n  return value.slice(0, spelling.open + 1) + writeMasked(mask, (i, j) => value.slice(start[i], start[j])) + value.slice(spelling.close);\n}\n/** Whether `hidden` (raw characters of a quoted root, hiddenBy) marks some but not all of the characters of one escape's spelling. Linear. */\nfunction partEscapeHidden(spelling: { start: Int32Array }, size: number, hidden: Uint8Array): boolean {\n  for (let i = 0; i < size; i++) {\n    const from = spelling.start[i], to = spelling.start[i + 1];\n    if (to - from < 2) continue;\n    let marked = 0;\n    for (let r = from; r < to; r++) marked += hidden[r];\n    if (marked && marked < to - from) return true;\n  }\n  return false;\n}\n/** A registered value only adds redaction. A text whose registered values take out the context of a shape or a pair (plainRuns) is read\n * twice: with what the shapes and the pair rule hide on the catalog's text joined into the union, and in the reference's order alone.\n * Where both reads only replace parts of the original text with placeholders (hiddenBy), what each hides is known by position: a\n * read is kept where it hides every character the other hides and its own hidden characters are known exactly (each kept part has one\n * place), and otherwise the original text is written once with the union of both (touching parts joined) replaced, a character counted\n * as shown by a read only where every way of reading its output shows it. So neither read can show a character the other hides. Where a read wrote anything else, the reference's order is kept where it hides at\n * least every character the joined read hides and shows no 4 characters together that the joined read does not show\n * (windowsWithin), the joined read where the same holds the other way, and otherwise the whole text is the placeholder. The alignment,\n * the subsequence tests and the window tests are linear in the text and both reads; they and the second read are not charged to the privacy budget. The joined read charges the budget; the other reads a copy of what is left. A budget the joined\n * read runs out of fails closed for this text alone: the text is read in the reference's order on the budget as it was (so the budget is\n * charged, and a text that order cannot read is refused, as the reference does) and the whole text becomes the placeholder; never the\n * reference's order with less hidden. A read inside another is not decided again. */\nfunction onlyAdds(original: string, budget: PrivacyBudget | undefined, read: (budget: PrivacyBudget | undefined) => string): string {\n  if (deciding) return read(budget);\n  deciding = true; forcing = true; forcedCount = 0;\n  try {\n    // What is left of a budget held in plain fields (an accessor is not read here).\n    const plain = budget && \"value\" in (Object.getOwnPropertyDescriptor(budget, \"nodes\") ?? {}) && \"value\" in (Object.getOwnPropertyDescriptor(budget, \"text\") ?? {}) ? { nodes: budget.nodes, text: budget.text } : undefined;\n    let joined: string;\n    try { joined = read(budget); }\n    catch (error) {\n      // A budget the joined read runs out of, where the reference's order may not: the text fails closed. It is read in that order\n      // alone, on the budget as it was, so the budget is charged as the reference charges it (and refuses what it refuses); then the\n      // whole text is the placeholder, since what the joined read would hide is not known.\n      if (!forcedCount || !(error instanceof Error && /^Privacy .*budget$/.test(error.message))) throw error;\n      if (plain && budget) { budget.nodes = plain.nodes; budget.text = plain.text; }\n      forcing = false;\n      read(budget);\n      return replacement;\n    }\n    if (!forcedCount) return joined;\n    forcing = false;\n    // The budget is charged by the joined read; the reference's order is read on a copy of what is left of it.\n    let ordered: string;\n    // A budget the reference's order runs out of leaves the joined read, which hides more.\n    try { ordered = read(budget && { nodes: budget.nodes, text: budget.text, seen: budget.seen }); }\n    catch (error) { if (error instanceof Error && /^Privacy .*budget$/.test(error.message)) return joined; throw error; }\n    if (ordered === joined) return ordered;\n    const union = positionalUnion(original, ordered, joined);\n    if (union !== undefined) return union;\n    // A read that wrote more than placeholders cannot be placed in the original text, so what each hides is not known by position: a\n    // read is kept only where it hides at least every character the other hides and shows no 4 characters together that the other does\n    // not show (the subsequence test alone could match a character the other read hides to one it shows elsewhere). Otherwise the\n    // whole text is the placeholder.\n    if (hidesAtLeast(ordered, joined) && windowsWithin(ordered, joined)) return ordered;\n    if (hidesAtLeast(joined, ordered) && windowsWithin(joined, ordered)) return joined;\n    return replacement;\n  } finally { deciding = false; forcing = true; }\n}\nfunction embeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false, nested = false): string {\n  if (depth > catalog.limits.max_depth) throw new Error(\"Privacy structure budget\");\n  if (wholeSecret(value, secrets)) return replacement;\n  // A whole data URI or URL owns its delimiters, including brackets and quotes.\n  if (framed) { const uri = mediaReplacement(value); if (uri !== undefined) return uri; if (isURLValue(value)) return text(value, secrets, true, incomplete || incompleteURLs); }\n  // The URL and data URI catalog reads the text first, as the reference does: a known secret is looked for in the catalog's output,\n  // so it can neither consume a credential name, a delimiter or a userinfo '@' the pair rule needs, nor hide a data URI from\n  // the catalog. A complete JSON text is read leaf by leaf instead, and each leaf goes through the same order.\n  let catalogued = false;\n  // The spans the catalog wrote in the text, by position, while the text is the catalog's output as edited below.\n  let owned: Span[] | undefined;\n  if (!nested && !wholeContainer(value)) { [value, owned] = catalogText(value, incomplete || incompleteURLs, secrets); catalogued = true; }\n  // A union placeholder that holds a forced span is one the scrub wrote: the scan is not charged for it (see knownSecretRanges).\n  const ranges = scanned(budget, () => knownSecretRanges(value, secrets, depth, budget, owned?.filter(span => span.forced) ?? []));\n  // A text that opens with a bracket and is not one JSON document: when a known secret takes out part of a credential name, so that a\n  // value the pair rule reads on the text with the secrets in place is neither a secret nor read once they are replaced, the whole\n  // text is the placeholder.\n  if (catalogued && ranges.length && /^\\s*[\\[{]/.test(value) && secretCutsName(value, ranges)) return replacement;\n  // Owners are found on the text they frame; a pass that rewrites the text finds them again (once, below).\n  let owners: TextOwner[] | undefined;\n  const edited: SourceRange[] = [];\n  if (ranges.length) {\n    const found: TextOwner[] = owners = !nested && !catalogued ? scanned(budget, () => wholeOwner(value, depth, budget)) : scanned(budget, () => textOwners(value, depth, budget));\n    // Where the first range the whole-text read redacts (with the known secrets replaced, as the reference reads) starts; read once, when\n    // a secret inside brackets asks for it.\n    let firstRead: number | undefined;\n    const readBefore = nested ? undefined : (position: number) => (firstRead ??= referenceRedactions(value, ranges).reduce((low, range) => Math.min(low, range.start), Infinity)) < position;\n    // Whether four characters are a run of a known secret (of any length, as the secrets were given). Fewer than four, at an end of the\n    // text inside brackets, are read on with the text around the brackets, so they are a run where any secret holds them.\n    let runs: ReadonlySet<string> | undefined;\n    const secretRun = (run: string) => {\n      if (run.length < 4 && !nested) return false;\n      return (runs ??= secretRuns(secrets)).has(run);\n    };\n    let edits = scanned(budget, () => crossOwnerEdits(value, ranges, found, depth, budget, undefined, readBefore, secretRun));\n    // An edit that is not one known secret took an owner out whole. The reference reads its pair rule over the text with only the\n    // secrets replaced, so what that read redacts is taken out too: the owner's name must not be what ends a later pair's reading.\n    const rawKeys = new Set(ranges.map(range => range.start + \":\" + range.end));\n    // A given secret that is one whole JSON object or array keeps its owner's quotes around the placeholder, where the reference reads the\n    // placeholder alone: what the reference redacts on past its closing bracket (the rest of a pair's value) is taken out with the secret.\n    const quoted = edits.filter(edit => edit.text !== undefined && rawKeys.has(edit.start + \":\" + edit.end));\n    if (!nested && (quoted.length || edits.some(edit => !rawKeys.has(edit.start + \":\" + edit.end)))) {\n      // Only what touches an owner or an edit can differ from what the pair rule reads later; a range inside one JSON string belongs to\n      // that owner's own read.\n      // What the reference redacts outside every owner, or inside one owner that is read as text, or whose own string no quote, backslash or\n      // control character in the range can break, is replaced where it stands: the name that decides it may be in what an edit took out, so the owner does\n      // not read it. Only a range that crosses an owner goes through the cross-owner edits.\n      const outer: SourceRange[] = [], plain: SourceRange[] = [];\n      const taken = edits.filter(edit => !rawKeys.has(edit.start + \":\" + edit.end));\n      const reads = joinRanges(referenceRedactions(value, ranges));\n      for (const edit of quoted) { const range = firstEnding(reads, edit.end); if (range && range.start < edit.end) outer.push({ start: Math.max(range.start, edit.start), end: range.end }); }\n      for (const range of reads) {\n        // What an edit takes out changes how the quotes and names after it pair up, to the end of the text, so a range that overlaps it,\n        // or follows it at any distance, can be read differently once the edit is made.\n        let low = 0, high = taken.length;\n        while (low < high) { const mid = (low + high) >> 1; if (taken[mid].start < range.end) low = mid + 1; else high = mid; }\n        if (!low) continue;\n        const owner = firstEnding(found, range.start);\n        (!owner || owner.start >= range.end || owner.start <= range.start && owner.end >= range.end && (owner.kind === \"tail\" || owner.kind === \"wrapper\" || !/[\"\\\\\\u0000-\\u001f]/.test(value.slice(range.start, range.end))) ? plain : outer).push(range);\n      }\n      edits = withPlainRanges(scanned(budget, () => crossOwnerEdits(value, joinRanges([...ranges, ...outer]), found, depth, budget, { budget: workBudget(), secrets: rawKeys }, readBefore, secretRun)), plain);\n    }\n    const matched = replaceEdits(value, edits);\n    // Where each edit's text stands in the replaced text: a secret that the neighbouring characters form with an edit is not one of\n    // the text the reference reads, which replaces its known secrets once.\n    if (!nested) { let shift = 0; for (const edit of edits) { const length = (edit.text ?? replacement).length; edited.push({ start: edit.start + shift, end: edit.start + shift + length }); shift += length - (edit.end - edit.start); } }\n    // A nested owner's text was read by the whole-text pass with its known secrets in place, so a value shape beside one was cut\n    // off by a word character; the reference replaces known secrets first, so read the shapes again on the replaced text.\n    if (matched !== value) { if (owned) owned = movedSpans(owned, edits); value = nested ? credentialPairs(credentialText(matched, [])) : matched; owners = undefined; }\n  }\n  let whole = false;\n  // A catalogued text is not one JSON document, and replacing its known secrets does not make it one: a secret in a wrapper or tail\n  // that opens the text takes it out as a bare placeholder or is kept, and a JSON owner's string becomes a string, with the text\n  // beside it left as it was. So only a text that was one JSON document is read as a whole container.\n  if (!nested && !(whole = !catalogued && wholeContainer(value))) {\n    // The credential pair rule reads the whole text once, after the URL catalog, known secrets and value shapes, before any\n    // owner below rewrites a name or a value it must read. Known secrets outside every owner were replaced just above; the\n    // owners replace their own (a JSON owner keeps its encoding), so this pass reads none.\n    if (!catalogued) [value, owned] = catalogText(value, incomplete || incompleteURLs, secrets);\n    // A known secret left to an owner is still in the text. The reference replaces every known secret before it reads a value shape or\n    // a pair; when what those read differs with the secrets in place (a shape run or a pair that ends at, starts at or runs through a\n    // secret, so the owner would find a secret changed or gone), the text is read in the reference's order instead.\n    // The placeholders this scrub wrote are not charged by this second scan; one forward pass over the sorted edits keeps the secrets\n    // that no edit touches.\n    let edit = 0;\n    const left = ranges.length ? scanned(budget, () => knownSecretRanges(value, secrets, depth, budget, edited)).filter(range => {\n      while (edit < edited.length && edited[edit].end <= range.start) edit++;\n      return !(edit < edited.length && edited[edit].start < range.end);\n    }) : [];\n    let ordered = false;\n    const plainRead = catalogPairs(value, [], owned);\n    if (left.length) {\n      const view = joinRanges(referenceRedactions(value, left, true)), plain = joinRanges(referenceRedactions(value, [], true));\n      // A bare media token read here is one the reference reads only after the secrets are replaced (a secret in it takes it apart).\n      ordered = view.length !== plain.length || view.some((range, k) => range.start !== plain[k].start || range.end !== plain[k].end) || mediaTokens(plainRead) !== plainRead;\n    }\n    const paired = mediaTokens(ordered ? catalogPairs(value, secrets, owned) : plainRead);\n    if (paired !== value) { value = paired; owners = undefined; }\n  }\n  owners ??= whole ? scanned(budget, () => wholeOwner(value, depth, budget)) : scanned(budget, () => textOwners(value, depth, budget));\n  let end = 0, out = \"\";\n  // Only a container goes one level deeper than the text that holds it; a quoted string in the text is read as text, at the text's depth.\n  const below = (parsed: unknown) => parsed !== null && typeof parsed === \"object\" ? depth + 1 : depth;\n  for (const owner of owners) {\n    const raw = value.slice(owner.start, owner.end);\n    out += bareGaps(value.slice(end, owner.start));\n    if (owner.kind === \"json\") {\n      // JSON inside a text that is not the value's own JSON document is text to the reference, which gives it no depth: one that reaches past\n      // the depth limit is left to the reads of the whole text above, as brackets that enclose no JSON are.\n      if (wholeSecret(raw, secrets)) { out += JSON.stringify(replacement); end = owner.end; continue; }\n      let read = raw, parsed = owner.parsed;\n      // A JSON string literal in a text is raw text to the reference, which replaces the text's known secrets there, longest first, on the\n      // escaped bytes: a longer secret that only the decoded string holds must not take part of a shorter one first and leave the rest of\n      // it readable. The literal is read with those secrets replaced; one whose replacement is not one JSON string is the placeholder.\n      // Where the escaped bytes and the string they encode have the same known secrets replaced, the string is read as it is: a number\n      // in a JSON text that the string holds is replaced by a quoted placeholder, so that text is still JSON. That read goes one level\n      // into the JSON text and is bounded by a node budget of its own, as a value of its own would be: the literal is charged as one\n      // node, as the reference charges a string, so its siblings are read with the same budget whatever their order. Where that read\n      // reaches past the depth limit or its own node budget, the literal is read on its escaped bytes instead, with the budget as it was.\n      if (typeof parsed === \"string\" && (read = knownSecretText(raw, secrets)) !== raw) {\n        try { parsed = JSON.parse(read); } catch { parsed = undefined; }\n        if (typeof parsed !== \"string\") { out += JSON.stringify(replacement); end = owner.end; continue; }\n        if (parsed === knownSecretText(owner.parsed as string, secrets)) {\n          const trial: PrivacyBudget = { nodes: catalog.limits.max_nodes, text: budget.text, seen: budget.seen };\n          let clean: string | undefined;\n          try { clean = scrubJSON(raw, owner.parsed, secrets, depth, trial, false, false, incomplete, incompleteURLs, payloadRole); }\n          catch (error) { if (!(error instanceof Error) || error.message !== \"Privacy structure budget\") throw error; }\n          // The read of the string it encodes replaces the known secrets of each string inside it, longest first, as that string spells\n          // them; where a longer secret is spelled as given only there (it holds a quote, a backslash or the text of an escape), it can\n          // take part of a shorter one that the escaped bytes hide whole. So that read is taken only where no reading of it shows a run of\n          // a known secret more often than the read on the escaped bytes does, at any layer; otherwise the escaped-bytes read is taken.\n          // The escaped-bytes read is made on a node budget of its own too; one that reaches past it is no fallback, so the decoded read is\n          // not taken and the literal is read below as it is without one. Where the escaped-bytes read is taken, the budget is charged\n          // the nodes it read.\n          if (clean !== undefined) {\n            const bytes: PrivacyBudget = { nodes: catalog.limits.max_nodes, text: budget.text, seen: budget.seen };\n            let escaped: string | undefined, kept = false;\n            const escapedRead = () => {\n              try { return escaped ??= (catalogued || nested) && pastDepth(parsed, below(parsed)) ? read : scrubJSON(read, parsed, secrets, below(parsed), bytes, false, false, incomplete, incompleteURLs, payloadRole); }\n              catch (error) { throw error instanceof Error && error.message === \"Privacy structure budget\" ? escapedRead : error; }\n            };\n            try { kept = lessShown(clean, secrets, escapedRead); } catch (error) { if (error !== escapedRead) throw error; }\n            if (kept) { budget.text = trial.text; charge(budget, depth); out += clean; end = owner.end; continue; }\n            if (escaped !== undefined) {\n              budget.text = bytes.text;\n              for (let nodes = catalog.limits.max_nodes - bytes.nodes; nodes > 0; nodes--) charge(budget, depth);\n              out += escaped; end = owner.end; continue;\n            }\n          }\n        }\n      }\n      if ((catalogued || nested) && pastDepth(parsed, below(parsed))) { out += read; end = owner.end; continue; }\n      let replaced: string;\n      if (!whole && read === raw && parsed !== null && typeof parsed === \"object\" && (replaced = knownSecretText(raw, secrets)) !== raw) {\n        // A JSON object or array in a text that is not one JSON document is read leaf by leaf, each string with its known secrets replaced as it spells them; the\n        // reference replaces them on the raw bytes, longest first. Where a longer secret is spelled as given only in a decoded string, it\n        // can take part of a shorter one the raw bytes hide whole, so the leaf read is taken only where no reading of it shows a run of a\n        // known secret more often than the read of the raw bytes with their known secrets replaced does: in that read the known secrets are\n        // replaced on the raw bytes first; where those are then not one JSON text, the object is the quoted placeholder. An object whose\n        // raw bytes hold no known secret has nothing the reference hides in it to keep.\n        const leaves: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };\n        const clean = scrubJSON(raw, parsed, secrets, below(parsed), leaves, false, false, incomplete, incompleteURLs, payloadRole);\n        const rest: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };\n        let fallback: string | undefined;\n        const bytesRead = () => {\n          if (fallback !== undefined) return fallback;\n          let bytes: unknown;\n          try { bytes = JSON.parse(replaced); } catch { bytes = undefined; }\n          return fallback = bytes !== null && typeof bytes === \"object\" ? scrubJSON(replaced, bytes, secrets, below(bytes), rest, false, false, incomplete, incompleteURLs, payloadRole) : JSON.stringify(replacement);\n        };\n        // The fallback is read before the budget is written back, so it is charged also where the decoded read's readings passed their bound.\n        const kept = lessShown(clean, secrets, bytesRead), read = kept ? clean : bytesRead();\n        const chosen = kept ? leaves : rest;\n        budget.nodes = chosen.nodes; budget.text = chosen.text;\n        out += read;\n        end = owner.end; continue;\n      }\n      out += scrubJSON(read, parsed, secrets, below(parsed), budget, false, false, incomplete, incompleteURLs, payloadRole);\n    } else if (owner.kind === \"field\") {\n      const readField = (source: string, key: string, keyEnd: number, valueStart: number, parsedValue: unknown, fieldBudget: PrivacyBudget) => {\n        const cleanKey = text(key, secrets, false, incompleteURLs);\n        const urlContext: JsonNumbers | undefined = incompleteURLs || payloadRole ? { input: new WeakMap(), output: new WeakMap(), incompleteURLs, payloadRole } : undefined;\n        const encoded = scrubJSON(source.slice(valueStart), parsedValue, secrets, depth, fieldBudget, false, false, incomplete, incompleteURLs, payloadRole);\n        const clean = walkValue(JSON.parse(encoded), secrets, key, depth, false, fieldBudget, noHints, true, false, incomplete, true, undefined, false, urlContext);\n        return (cleanKey === key ? source.slice(0, keyEnd) : JSON.stringify(cleanKey)) + source.slice(keyEnd, valueStart)\n          + (clean === parsedValue ? source.slice(valueStart) : JSON.stringify(clean));\n      };\n      const replaced = knownSecretText(raw, secrets);\n      if (replaced === raw) { out += readField(raw, owner.key!, owner.keyEnd! - owner.start, owner.valueStart! - owner.start, owner.parsed, budget); end = owner.end; continue; }\n      // Its name and value are read as the strings they encode; the reference replaces known secrets on the raw bytes, longest first. Where\n      // that read shows a run of a known secret more often than the read of the pair with its known secrets replaced on its raw bytes does\n      // (a longer secret spelled as given only once decoded took part of a shorter one), that read is taken; where the pair is then not one\n      // name and JSON value, it is the quoted placeholder.\n      const decoded: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen }, rest: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };\n      const field = readField(raw, owner.key!, owner.keyEnd! - owner.start, owner.valueStart! - owner.start, owner.parsed, decoded);\n      let fallback: string | undefined;\n      const bytesRead = () => {\n        if (fallback !== undefined) return fallback;\n        const pair = /^(\"(?:[^\"\\\\]|\\\\.)*\")(\\s*:\\s*)([\\s\\S]*)$/.exec(replaced);\n        let key: unknown, parsedValue: unknown;\n        try { if (pair) { key = JSON.parse(pair[1]); parsedValue = JSON.parse(pair[3]); } } catch { key = undefined; }\n        return fallback = pair && typeof key === \"string\" ? readField(replaced, key, pair[1].length, pair[1].length + pair[2].length, parsedValue, rest) : JSON.stringify(replacement);\n      };\n      const kept = lessShown(field, secrets, bytesRead), read = kept ? field : bytesRead(), chosen = kept ? decoded : rest;\n      budget.nodes = chosen.nodes; budget.text = chosen.text;\n      out += read;\n    } else if (owner.kind === \"wrapper\") {\n      // Brackets that enclose no JSON are read as text, as the reference reads them: past the depth limit the text is left to the reads made on the\n      // whole text above instead of being read again one level deeper.\n      out += depth + 1 > catalog.limits.max_depth ? raw : raw[0] + scrubEmbeddedText(raw.slice(1, -1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole, true) + raw.at(-1);\n    } else {\n      // A truncated JSON object cannot safely retain structural media context.\n      const fields: [string, unknown][] = [];\n      for (const [key, field] of stringPairs(raw)) {\n        try { fields.push([JSON.parse(key), JSON.parse(field)]); } catch { /* malformed */ }\n      }\n      const hints = indexPrivacyHints(fields);\n      const mediaFragment = [...hints.values()].some(h => h[0] || h[1]) || [...raw.matchAll(/\"([^\"\\\\]+)\"\\s*:/g)].some(m => incompleteMediaPath(m[1]));\n      const propertyOnly = /^(\\{\\s*)(\"(?:\\\\.|[^\"\\\\])*\")(\\s*:\\s*)$/.exec(raw);\n      const jsonTail = !propertyOnly && /^(?:\\{\\s*\"|\\[\\s*(?:[\\[{\"\\d-]|true\\b|false\\b|null\\b))/.test(raw);\n      // A truncated media fragment is omitted as incomplete audio; a truncated JSON text with no media field is omitted as what it is.\n      if (incomplete || !propertyOnly && raw[0] === \"{\" && mediaFragment) out += \"<omitted: incomplete audio>\";\n      else if (jsonTail) out += \"<omitted: incomplete JSON>\";\n      else if (propertyOnly) {\n        let key: unknown;\n        try { key = JSON.parse(propertyOnly[2]); } catch { out += text(raw, secrets, false, incomplete); end = owner.end; continue; }\n        const name = propertyOnly[2], replaced = knownSecretText(name, secrets);\n        if (replaced === name) {\n          const clean = scrubValue(key, secrets, \"\", depth + 1, false, budget, noHints, true, false, incomplete);\n          out += clean === key ? raw : propertyOnly[1] + JSON.stringify(clean) + propertyOnly[3];\n          end = owner.end; continue;\n        }\n        // The name is read as the string it encodes; the reference replaces known secrets on the raw bytes, longest first. Where that read\n        // shows a run of a known secret more often than the read of the name with its known secrets replaced on its raw bytes does, that\n        // read is taken; where the name is then not one JSON string, it is the quoted placeholder.\n        const decoded: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen }, rest: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: budget.seen };\n        const readName = (source: string, parsedName: unknown, nameBudget: PrivacyBudget) => {\n          const clean = scrubValue(parsedName, secrets, \"\", depth + 1, false, nameBudget, noHints, true, false, incomplete);\n          return clean === parsedName ? source : JSON.stringify(clean);\n        };\n        const field = readName(name, key, decoded);\n        let fallback: string | undefined;\n        const bytesRead = () => {\n          if (fallback !== undefined) return fallback;\n          let parsedName: unknown;\n          try { parsedName = JSON.parse(replaced); } catch { parsedName = undefined; }\n          return fallback = typeof parsedName === \"string\" ? readName(replaced, parsedName, rest) : JSON.stringify(replacement);\n        };\n        const kept = lessShown(field, secrets, bytesRead), read = kept ? field : bytesRead(), chosen = kept ? decoded : rest;\n        budget.nodes = chosen.nodes; budget.text = chosen.text;\n        out += read === name ? raw : propertyOnly[1] + read + propertyOnly[3];\n      } else out += depth + 1 > catalog.limits.max_depth ? raw : raw[0] + scrubEmbeddedText(raw.slice(1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole, true);\n    }\n    end = owner.end;\n  }\n  // Only unowned gaps use the prose policy. Repaired JSON/quoted layers must\n  // never return through URL or bare-media scanning as one blob.\n  out += bareGaps(value.slice(end));\n  const result = whole && /^\\s*[\\[{]/.test(value) ? structuredText(out, {}, secrets, framed, incomplete || incompleteURLs) : out;\n  // A secret that sat inside an owner was left to it; when a read of the text changed that owner, the secret is read again here.\n  return ranges.length && !nested && !whole ? knownSecretText(result, secrets) : result;\n}",
   "function scrubEmbeddedText(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, framed: boolean, incomplete = false, incompleteURLs = false, payloadRole = false): string {\n  if (depth > catalog.limits.max_depth) throw new Error(\"Privacy structure budget\");\n  if (wholeSecret(value, secrets)) return replacement;\n  const ranges = knownSecretRanges(value, secrets, depth, budget);\n  // A whole data URI or URL owns its delimiters, including brackets and quotes.\n  if (framed) { const uri = mediaReplacement(value); if (uri !== undefined) return uri; if (isURLValue(value)) return text(value, secrets, true, incomplete || incompleteURLs); }\n  let owners = textOwners(value, depth, budget);\n  const matched = redactCrossOwnerSecrets(value, ranges, owners, depth, budget);\n  if (matched !== value) { value = matched; owners = textOwners(value, depth, budget); }\n  const assigned = scrubAssignments(value, owners);\n  if (assigned !== value) { value = assigned; owners = textOwners(value, depth, budget); }\n  let end = 0, out = \"\";\n  for (const owner of owners) {\n    const raw = value.slice(owner.start, owner.end);\n    out += text(value.slice(end, owner.start), secrets, false, incomplete || incompleteURLs);\n    if (owner.kind === \"json\") {\n      out += wholeSecret(raw, secrets) ? JSON.stringify(replacement) : scrubJSON(raw, owner.parsed, secrets, depth + 1, budget, false, false, incomplete, incompleteURLs, payloadRole);\n    } else if (owner.kind === \"field\") {\n      const key = owner.key!, cleanKey = text(key, secrets, false, incompleteURLs);\n      const urlContext: JsonNumbers | undefined = incompleteURLs || payloadRole ? { input: new WeakMap(), output: new WeakMap(), incompleteURLs, payloadRole } : undefined;\n      const encoded = scrubJSON(value.slice(owner.valueStart, owner.end), owner.parsed, secrets, depth + 1, budget, false, false, incomplete, incompleteURLs, payloadRole);\n      const clean = walkValue(JSON.parse(encoded), secrets, key, depth + 1, false, budget, noHints, true, false, incomplete, true, undefined, false, urlContext);\n      out += (cleanKey === key ? value.slice(owner.start, owner.keyEnd) : JSON.stringify(cleanKey))\n        + value.slice(owner.keyEnd, owner.valueStart)\n        + (clean === owner.parsed ? value.slice(owner.valueStart, owner.end) : JSON.stringify(clean));\n    } else if (owner.kind === \"wrapper\") {\n      out += raw[0] + scrubEmbeddedText(raw.slice(1, -1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole) + raw.at(-1);\n    } else {\n      // A truncated JSON object cannot safely retain structural media context.\n      const fields: [string, unknown][] = [];\n      for (const match of raw.matchAll(/(\"(?:\\\\.|[^\"\\\\])*\"\\s*:\\s*)(\"(?:\\\\.|[^\"\\\\])*\")/g)) {\n        try { fields.push([JSON.parse(match[1].replace(/\\s*:\\s*$/, \"\")), JSON.parse(match[2])]); } catch { /* malformed */ }\n      }\n      const hints = indexPrivacyHints(fields);\n      const mediaFragment = [...hints.values()].some(h => h[0] || h[1]) || [...raw.matchAll(/\"([^\"\\\\]+)\"\\s*:/g)].some(m => incompleteMediaPath(m[1]));\n      const propertyOnly = /^(\\{\\s*)(\"(?:\\\\.|[^\"\\\\])*\")(\\s*:\\s*)$/.exec(raw);\n      const jsonTail = !propertyOnly && /^(?:\\{\\s*\"|\\[\\s*(?:[\\[{\"\\d-]|true\\b|false\\b|null\\b))/.test(raw);\n      if (incomplete || !propertyOnly && raw[0] === \"{\" && mediaFragment || jsonTail) out += \"<omitted: incomplete audio>\";\n      else if (propertyOnly) {\n        let key: unknown;\n        try { key = JSON.parse(propertyOnly[2]); } catch { out += text(raw, secrets, false, incomplete); end = owner.end; continue; }\n        const clean = scrubValue(key, secrets, \"\", depth + 1, false, budget, noHints, true, false, incomplete);\n        out += clean === key ? raw : propertyOnly[1] + JSON.stringify(clean) + propertyOnly[3];\n      } else out += raw[0] + scrubEmbeddedText(raw.slice(1), secrets, depth + 1, budget, false, incomplete, incompleteURLs, payloadRole);\n    }\n    end = owner.end;\n  }\n  // Only unowned gaps use the prose policy. Repaired JSON/quoted layers must\n  // never return through assignment, URL or bare-media scanning as one blob.\n  return out + text(value.slice(end), secrets, false, incomplete || incompleteURLs);\n}"],
  ["scrubText",
   "export function scrubText(value: string, secrets: readonly string[] = [], depth = 0): string {\n  const budget = newBudget(); charge(budget, depth, value.length);\n  return pathBudgeted(() => {\n    const opening = jsonTextStart(value);\n    // A JSON text cut off at a quoted member's colon follows the value rule, not the prose fragment rule.\n    if (value[opening] === \"{\" || value[opening] === \"[\") {\n      let end = value.length;\n      while (end > opening && pySpace(value[end - 1])) end--;\n      if (value[end - 1] === \":\") {\n        end--;\n        while (end > opening && JSON_WHITESPACE.has(value[end - 1])) end--;\n        if (value[end - 1] === '\"' && unreadableJSON(value, opening, secrets, depth, budget, { media: false, audio: false }, false)) return replacement;\n      }\n    }\n    return scrubEmbeddedText(value, secrets, depth, budget, false);\n  });\n}\n",
   "export function scrubText(value: string, secrets: readonly string[] = [], depth = 0): string {\n  const budget = newBudget(); charge(budget, depth, value.length);\n  return scrubEmbeddedText(value, secrets, depth, budget, false);\n}\n"],
  ["scrubAttribute",
   "export function scrubAttribute(value: string, secrets: readonly string[] = [], incompleteURLs = false): string {\n  return pathBudgeted(() => attributeText(value, secrets, incompleteURLs));\n}\nfunction attributeText(value: string, secrets: readonly string[], incompleteURLs: boolean): string {\n",
   "export function scrubAttribute(value: string, secrets: readonly string[] = [], incompleteURLs = false): string {\n"],
  ["scrubProviderResponse",
   "export function scrubProviderResponse(value: unknown, secrets: readonly string[] = [], format: LogprobFormat = \"openai.chat\", textPolicy: (value: string) => unknown = value => value, incomplete = false): any {\n  return pathBudgeted(() => providerResponse(value, secrets, format, textPolicy, incomplete));\n}\nfunction providerResponse(value: unknown, secrets: readonly string[], format: LogprobFormat, textPolicy: (value: string) => unknown, incomplete: boolean): any {\n",
   "export function scrubProviderResponse(value: unknown, secrets: readonly string[] = [], format: LogprobFormat = \"openai.chat\", textPolicy: (value: string) => unknown = value => value, incomplete = false): any {\n"],
  ["text",
   "/** A run of overlapping occurrences of one known secret in the original text, and whether a catalog edit cuts one of them: overlaps\n * it and runs on past its end (what is left after the edit would no longer be found whole in the rewritten text). An inserted\n * placeholder cuts an occurrence it falls inside; the userinfo edit and a data URI's omission cut none (see Edit). */\ninterface OccurrenceRun { start: number; end: number; cut: boolean; /** A span the value shapes or the pair rule hide (plainRuns). */ force?: boolean }\n/** The runs of every occurrence of each known secret, overlapping occurrences included, sorted by start. Each secret is read in one\n * pass over the text (Knuth-Morris-Pratt), and each occurrence is checked against the edits (sorted, disjoint) by a cursor that only\n * moves forward, so a self-overlapping secret or a long one over many edits costs no more than any other. */\nfunction occurrenceRuns(value: string, secrets: readonly string[], edits: readonly Edit[]): OccurrenceRun[] {\n  const runs: OccurrenceRun[] = [], count = edits.length;\n  // The first edit at or after each index that can cut an occurrence.\n  const cutting = new Int32Array(count + 1);\n  cutting[count] = count;\n  for (let k = count - 1; k >= 0; k--) cutting[k] = edits[k].whole ? cutting[k + 1] : k;\n  for (const secret of prepareSecrets(secrets)) {\n    if (typeof secret !== \"string\" || value.indexOf(secret) < 0) continue;\n    const size = secret.length, border = new Int32Array(size);\n    for (let i = 1, k = 0; i < size; i++) {\n      while (k > 0 && secret.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];\n      if (secret.charCodeAt(i) === secret.charCodeAt(k)) k++;\n      border[i] = k;\n    }\n    let run: OccurrenceRun | undefined, j = 0;\n    for (let i = 0, k = 0; i < value.length; i++) {\n      while (k > 0 && value.charCodeAt(i) !== secret.charCodeAt(k)) k = border[k - 1];\n      if (value.charCodeAt(i) === secret.charCodeAt(k)) k++;\n      if (k < size) continue;\n      k = border[k - 1];\n      const start = i + 1 - size, end = i + 1;\n      // The first edit that ends after the occurrence starts.\n      while (j < count && edits[j].to <= start) j++;\n      let cut = false;\n      // Only an occurrence that runs on past the end of an edit it overlaps is cut: one that ends inside it is hidden there.\n      if (j < count && edits[j].from < end && edits[j].to < end) {\n        if (!edits[j].whole) cut = true;\n        else { const next = cutting[j + 1]; cut = next < count && edits[next].from < end && edits[next].to < end; }\n      }\n      if (run && start < run.end) { run.end = end; run.cut ||= cut; }\n      else { if (run) runs.push(run); run = { start, end, cut }; }\n    }\n    if (run) runs.push(run);\n  }\n  return runs.sort((a, b) => a.start - b.start);\n}\n/** Where the original position p (not inside an edit) stands in the rewrite; `after` places it after a placeholder inserted at p. */\nfunction rewritePosition(edits: readonly Edit[], p: number, after: boolean): number {\n  let low = 0, high = edits.length;\n  while (low < high) {\n    const mid = (low + high) >> 1, edit = edits[mid];\n    if (edit.to < p || edit.to === p && (after || edit.from < p)) low = mid + 1; else high = mid;\n  }\n  return low ? edits[low - 1].end + (p - edits[low - 1].to) : p;\n}\n/** The catalog's rewrite, the known secrets and what the value shapes and the pair rule hide as one union of spans on the ORIGINAL\n * text, applied in one rewrite. A registered secret occurrence that a catalog edit cuts is joined with every edit and occurrence that\n * overlaps it (and what overlaps those); from the first edit in that union to its end, the original becomes one placeholder, which the\n * catalog owns. So no part of an occurrence that runs on past a value the catalog replaced is left after it, while what stands before\n * that value (the name and delimiter the catalog and the pair rule read, as the reference keeps them) stays. A union that holds a span\n * the value shapes or the pair rule hide (`force`, see plainRuns), or two overlapping occurrences of which one is cut, starts where its\n * first interval starts: every overlapping occurrence is hidden whole with it, so a shorter one replaced first cannot leave part of a\n * longer one. Every other edit stays as the catalog wrote it, and every other occurrence is left whole for the known-secret pass.\n * `found` holds the runs of occurrences (occurrenceRuns) and the forced spans, by start; edits are sorted and disjoint. Returns the\n * rewritten text and its catalog spans. */\nfunction spanUnion(output: string, edits: readonly Edit[], spans: readonly Span[], found: readonly OccurrenceRun[], size: number): [string, Span[]] {\n  if (!found.length || !edits.length && !found.some(run => run.force)) return [output, spans as Span[]];\n  const unions: [start: number, end: number, inserted: boolean, forced: boolean][] = [];\n  // A union runs on to the end of the URL token of its last edit (`limit`; the end of the text for a whole URL value): the pair rule\n  // would read a credential name inside a cut occurrence to the end of its value, which in a URL token can run to the token's end,\n  // so taking the rest of the token keeps hidden all that the reference hides there.\n  let to = -1, first = -1, limit = -1, inserted = false, cut = false, force = false, low = -1, seen = -1, overlapped = false, e = 0, f = 0;\n  const close = () => {\n    if (!cut && !force) return;\n    const whole = force || overlapped, from = whole ? low : first, insert = whole ? false : inserted;\n    const last = unions[unions.length - 1];\n    if (last && last[1] >= from) { last[1] = Math.max(last[1], to); last[3] ||= force; }\n    else unions.push([from, to, insert, force]);\n  };\n  while (e < edits.length || f < found.length) {\n    // The next interval by start: an edit, or an occurrence. An inserted placeholder joins only the union it falls inside.\n    const edit = f >= found.length || e < edits.length && edits[e].from <= found[f].start;\n    const start = edit ? edits[e].from : found[f].start, end = edit ? edits[e].to : found[f].end;\n    const run = edit ? undefined : found[f], cuts = !!run && run.cut, bound = edit ? edits[e].limit ?? size : -1;\n    edit ? e++ : f++;\n    if (start === end) { if (start < to) { if (first < 0) { first = start; inserted = true; } limit = Math.max(limit, bound); if (cut && limit > to) to = limit; } continue; }\n    if (start < to) {\n      if (end > to) to = end; if (edit && first < 0) first = start; limit = Math.max(limit, bound); cut ||= cuts;\n      // Two occurrences overlap: the second starts before the first ends.\n      if (run?.force) force = true; else if (run) { overlapped ||= start < seen; seen = Math.max(seen, end); }\n      if (cut && limit > to) to = limit;\n      continue;\n    }\n    close();\n    to = end; first = edit ? start : -1; limit = bound; inserted = false; cut = cuts; low = start;\n    force = !!run?.force; seen = run && !run.force ? end : -1; overlapped = false;\n    if (cut && limit > to) to = limit;\n  }\n  close();\n  if (!unions.length) return [output, spans as Span[]];\n  const moved: Span[] = [], parts: string[] = [];\n  let copied = 0, shift = 0, k = 0;\n  for (const [start, end, insert, forced] of unions) {\n    const at = rewritePosition(edits, start, !insert), stop = rewritePosition(edits, end, false);\n    while (k < spans.length && spans[k].end <= at) { moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission }); k++; }\n    while (k < spans.length && spans[k].start < stop) k++;\n    parts.push(output.slice(copied, at), replacement);\n    moved.push(forced ? { start: at + shift, end: at + shift + replacement.length, omission: false, forced } : { start: at + shift, end: at + shift + replacement.length, omission: false });\n    shift += replacement.length - (stop - at);\n    copied = stop;\n  }\n  for (; k < spans.length; k++) moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission });\n  parts.push(output.slice(copied));\n  return [parts.join(\"\"), moved];\n}\n/** The first stage of the export text rules: the URL and data URI catalog over prose. Each URL token is rewritten by the catalog and\n * each data URI is omitted before any known secret or value shape is read, so a secret or shape never consumes a name, a delimiter\n * or a userinfo '@' that the catalog and the pair rule need. A private key that a known secret overlaps is left to the known-secret\n * pass, as the reference reads it only as a value shape after it. A bare image or audio token is omitted last (see mediaTokens).\n * Returns the text and the spans the catalog wrote in it (see spanUnion), by position. */\nfunction catalogText(value: string, incomplete: boolean, secrets: readonly string[], pairs = true): [string, Span[]] {\n  // A private key runs across URL-like tokens and lines, and a token read on its own (a leading '/' or '?') may be rewritten\n  // into a form that no longer holds the key's header: read it once over the whole prose first.\n  if (!secretIn(value, secrets)) value = privateKeyText(value);\n  let end = 0, result = \"\";\n  const spans: Span[] = [], edits: Edit[] = [];\n  // Each data URI is cut where its continuation lines stop being base64; the rest is read as text.\n  for (const [start, stop, data] of scanText(typeof value === \"string\" ? TEXT_SCAN : new RegExp(TEXT.source, TEXT.flags), value, 2)) {\n    const original = value.slice(start, stop);\n    result += value.slice(end, start);\n    if (!data) {\n      const closing = start > 0 && value[start - 1] === \"'\" && original.endsWith(\"'\") ? \"'\" : \"\";\n      const [rewritten, written, changed] = framedUrl(closing ? original.slice(0, -1) : original, incomplete);\n      const at = result.length;\n      for (const span of written) spans.push({ start: span.start + at, end: span.end + at, omission: span.omission });\n      const limit = start + original.length - closing.length;\n      for (const edit of changed) edits.push({ ...edit, from: edit.from + start, to: edit.to + start, start: edit.start + at, end: edit.end + at, limit });\n      result += rewritten + closing;\n    } else {\n      const clean = dataText(original);\n      if (clean !== original) {\n        spans.push({ start: result.length, end: result.length + clean.length, omission: true });\n        // A data URI's omission cuts no occurrence, as the reference reads a secret after it: what a secret runs on into stays.\n        edits.push({ from: start, to: stop, start: result.length, end: result.length + clean.length, whole: true, limit: stop });\n      }\n      result += clean;\n    }\n    end = stop;\n  }\n  // The occurrences are read only where a registered secret occurs; the shapes and the pair rule then read the catalog's text with\n  // none replaced, and what they hide where an occurrence overlaps their context joins the union.\n  const output = result + value.slice(end);\n  if (!secretIn(value, secrets)) return [output, spans];\n  const found = occurrenceRuns(value, secrets, edits);\n  return spanUnion(output, edits, spans, byStart(found, forcedRuns(value, output, spans, edits, found, pairs, false)), value.length);\n}\n/** Whether any known secret occurs in the text. */\nfunction secretIn(value: string, secrets: readonly string[]): boolean {\n  return secrets.length > 0 && prepareSecrets(secrets).some(secret => typeof secret === \"string\" && value.indexOf(secret) >= 0);\n}\n/** A complete bare JPEG/MP3 token (it can begin with '/') is omitted after the pair rule has read the names in the text: a name written\n * right against the token is part of it, and must still be read as a name. Check the WHOLE framed token, never a slash-prefixed\n * substring of an authority/path. */\nfunction mediaTokens(value: string): string {\n  let end = 0, result = \"\";\n  for (const [start, stop, data] of scanText(typeof value === \"string\" ? TEXT_SCAN : new RegExp(TEXT.source, TEXT.flags), value, 2)) {\n    if (data) continue;\n    const original = value.slice(start, stop);\n    const closing = start > 0 && value[start - 1] === \"'\" && original.endsWith(\"'\") ? \"'\" : \"\";\n    const media = mediaReplacement(closing ? original.slice(0, -1) : original);\n    if (media === undefined) continue;\n    result += value.slice(end, start) + media + closing;\n    end = stop;\n  }\n  return end === 0 ? value : result + value.slice(end);\n}\nfunction text(value: string, secrets: readonly string[], framed: boolean, incomplete = false, pairs = false, bare = true): string {\n  return typeof value === \"string\" && secretIn(value, secrets) ? onlyAdds(value, undefined, () => orderedText(value, secrets, framed, incomplete, pairs, bare)) : orderedText(value, secrets, framed, incomplete, pairs, bare);\n}\nfunction orderedText(value: string, secrets: readonly string[], framed: boolean, incomplete = false, pairs = false, bare = true): string {\n  // This function only receives prose, names or URL-owned values. The order is the reference's: the URL and data URI catalog,\n  // then known secrets (one at a time, longest first), then value shapes once over the assembled text, then the pair rule.\n  if (framed && isURLValue(value)) return wholeUrlText(value, secrets, incomplete);\n  // Known values, then shapes, once over the assembled text: a run beside a URL token is read whole. The catalog's spans move with them.\n  let [result, owned] = credentialRuns(...catalogText(value, incomplete, secrets, pairs), secrets);\n  // The pair rule runs after known secrets and value shapes, and before any media is omitted.\n  if (pairs) result = credentialPairs(result, [], undefined, owned);\n  // Bare media is read in the prose between the tokens, never in reconstructed URL syntax.\n  if (bare) result = bareGaps(result);\n  return mediaTokens(result);\n}\n",
   "function text(value: string, secrets: readonly string[], framed: boolean, incomplete = false): string {\n  // This function only receives prose gaps, names or URL-owned values. Match\n  // original credentials before URL query/userinfo rewrites can split them.\n  value = knownSecretText(value, secrets);\n  if (framed && isURLValue(value)) return credentialText(urlValue(value, incomplete), secrets);\n  let end = 0, result = \"\";\n  // matchAll clones its matcher; recursion never shares an active exec cursor.\n  TEXT.lastIndex = 0;\n  for (const match of value.matchAll(typeof value === \"string\" ? TEXT : new RegExp(TEXT.source, TEXT.flags))) {\n    const start = match.index!, original = match[0];\n    result += bareMedia(credentialText(value.slice(end, start), secrets));\n    if (match[1]) {\n      const closing = start > 0 && value[start - 1] === \"'\" && original.endsWith(\"'\") ? \"'\" : \"\";\n      const token = closing ? original.slice(0, -1) : original;\n      // A complete bare JPEG/MP3 can also begin with '/'. Check the WHOLE\n      // framed token, never a slash-prefixed substring of an authority/path.\n      result += (mediaReplacement(token) ?? urlValue(token, incomplete)) + closing;\n    } else result += dataText(original);\n    end = start + original.length;\n  }\n  result += bareMedia(credentialText(value.slice(end), secrets));\n  // Known values/shapes still apply to URI components and across text gaps.\n  // Do not run bare-media recognition again over the assembled syntax.\n  return credentialText(result, secrets);\n}\n"],
];
// The v4 oracle text(): the same stages as the runtime (the URL and data URI catalog, known secrets, value shapes once over the
// assembled text, bare media in the gaps, the pair rule), written with the per-call RegExp and the plain helpers of the pins.
const V4_ORACLE_TEXT = String.raw`interface OccurrenceRun { start: number; end: number; cut: boolean; force?: boolean }
function occurrenceRuns(value: string, secrets: readonly string[], edits: readonly Edit[]): OccurrenceRun[] {
  const runs: OccurrenceRun[] = [], count = edits.length;
  const cutting = new Int32Array(count + 1);
  cutting[count] = count;
  for (let k = count - 1; k >= 0; k--) cutting[k] = edits[k].whole ? cutting[k + 1] : k;
  for (const secret of [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))]) {
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
      while (j < count && edits[j].to <= start) j++;
      let cut = false;
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
function rewritePosition(edits: readonly Edit[], p: number, after: boolean): number {
  let low = 0, high = edits.length;
  while (low < high) {
    const mid = (low + high) >> 1, edit = edits[mid];
    if (edit.to < p || edit.to === p && (after || edit.from < p)) low = mid + 1; else high = mid;
  }
  return low ? edits[low - 1].end + (p - edits[low - 1].to) : p;
}
function spanUnion(output: string, edits: readonly Edit[], spans: readonly Span[], found: readonly OccurrenceRun[], size: number): [string, Span[]] {
  if (!found.length || !edits.length && !found.some(run => run.force)) return [output, spans as Span[]];
  const unions: [start: number, end: number, inserted: boolean][] = [];
  // A union runs on to the end of the URL token of its last edit ('limit'; the end of the text for a whole URL value): the pair rule
  // would read a credential name inside a cut occurrence to the end of its value, which in a URL token can run to the token's end,
  // so taking the rest of the token keeps hidden all that the reference hides there.
  let to = -1, first = -1, limit = -1, inserted = false, cut = false, force = false, low = -1, seen = -1, overlapped = false, e = 0, f = 0;
  const close = () => {
    if (!cut && !force) return;
    const whole = force || overlapped, from = whole ? low : first, insert = whole ? false : inserted;
    const last = unions[unions.length - 1];
    if (last && last[1] >= from) last[1] = Math.max(last[1], to);
    else unions.push([from, to, insert]);
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
  for (const [start, end, insert] of unions) {
    const at = rewritePosition(edits, start, !insert), stop = rewritePosition(edits, end, false);
    while (k < spans.length && spans[k].end <= at) { moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission }); k++; }
    while (k < spans.length && spans[k].start < stop) k++;
    parts.push(output.slice(copied, at), replacement);
    moved.push({ start: at + shift, end: at + shift + replacement.length, omission: false });
    shift += replacement.length - (stop - at);
    copied = stop;
  }
  for (; k < spans.length; k++) moved.push({ start: spans[k].start + shift, end: spans[k].end + shift, omission: spans[k].omission });
  parts.push(output.slice(copied));
  return [parts.join(""), moved];
}
function catalogText(value: string, incomplete: boolean, secrets: readonly string[], pairs = true): [string, Span[]] {
  if (!secretIn(value, secrets)) value = privateKeyText(value);
  let end = 0, result = "";
  const spans: Span[] = [], edits: Edit[] = [];
  for (const [start, stop, data] of scanText(new RegExp(TEXT.source, TEXT.flags), value, 2)) {
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
        edits.push({ from: start, to: stop, start: result.length, end: result.length + clean.length, whole: true, limit: stop });
      }
      result += clean;
    }
    end = stop;
  }
  const output = result + value.slice(end);
  if (!secretIn(value, secrets)) return [output, spans];
  const found = occurrenceRuns(value, secrets, edits);
  return spanUnion(output, edits, spans, byStart(found, forcedRuns(value, output, spans, edits, found, pairs, false)), value.length);
}
function secretIn(value: string, secrets: readonly string[]): boolean {
  return secrets.length > 0 && [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))].some(secret => typeof secret === "string" && value.indexOf(secret) >= 0);
}
function mediaTokens(value: string): string {
  let end = 0, result = "";
  for (const [start, stop, data] of scanText(new RegExp(TEXT.source, TEXT.flags), value, 2)) {
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
  if (framed && isURLValue(value)) return wholeUrlText(value, secrets, incomplete);
  let [result, owned] = credentialRuns(...catalogText(value, incomplete, secrets, pairs), secrets);
  if (pairs) result = credentialPairs(result, [], undefined, owned);
  if (bare) result = bareGaps(result);
  return mediaTokens(result);
}`;
// Round-3 hot-path and order edits to the runtime. restoreV4 takes them out again so the pinned digest still holds; the oracle keeps
// the pinned scans (the quadratic quote scan and the regex tail scan), and reads the reference's known-secret order (see extendOracleV4).
const R3_RESTORE: [label: string, current: string, base: string][] = [
  ["code point order helpers",
   "/** Python's order of two strings: by code point (UTF-16 order differs where one side has a surrogate pair). */\nfunction compareCodePoints(a: string, b: string): number {\n  for (let i = 0, j = 0; i < a.length && j < b.length;) {\n    const x = a.codePointAt(i)!, y = b.codePointAt(j)!;\n    if (x !== y) return x < y ? -1 : 1;\n    i += x > 0xffff ? 2 : 1; j += y > 0xffff ? 2 : 1;\n  }\n  return 0;\n}\nexport const codePointLength = (value: string) => { let n = 0; for (let i = 0; i < value.length; i++) { n++; if (value.charCodeAt(i) >= 0xd800 && value.charCodeAt(i) <= 0xdbff && i + 1 < value.length && value.charCodeAt(i + 1) >= 0xdc00 && value.charCodeAt(i + 1) <= 0xdfff) i++; } return n; };\n/** The reference's minimum for a known secret: eight code points (a UTF-16 length of sixteen always holds them). */\nexport const longEnoughSecret = (value: string) => { const length = value.length; return length >= 16 || (length >= 8 && (typeof value !== \"string\" || codePointLength(value) >= 8)); };\n/** Known secrets in the reference's order: longest first (in code points), equal lengths by code point. Read once per call. */\nfunction longestFirst(secrets: string[]): string[] {\n  const lengths = new Map<string, number>();\n  for (const secret of secrets) if (typeof secret === \"string\") lengths.set(secret, codePointLength(secret));\n  return secrets.sort((a, b) => typeof a === \"string\" && typeof b === \"string\" ? lengths.get(b)! - lengths.get(a)! || compareCodePoints(a, b) : b.length - a.length);\n}\n",
   ""],
  ["known secret order, unbounded input",
   "    return longestFirst(input);\n",
   "    return input.sort((a, b) => b.length - a.length);\n"],
  ["known secret order, memoized input",
   "  const ordered = longestFirst(detached.slice());\n",
   "  const ordered = detached.slice().sort((a, b) => b.length - a.length);\n"],
  ["textOwners unclosed quote memo",
   "  // A quote with no closing quote after it leaves every later quote unclosed too: a scan from a later quote reads the same text in\n  // the same escape state as the scan that failed (each later quote was passed as an escaped one), so it is not repeated.\n  let unclosed = false;\n  const closing = (from: number): number => {\n    if (unclosed) return -1;\n    const stop = quotedEnd(value, from);\n    if (stop < 0) unclosed = true;\n    return stop;\n  };\n",
   ""],
  ["textOwners quote at top level",
   "const end = closing(i); if (end < 0) continue;\n",
   "const end = quotedEnd(value, i); if (end < 0) continue;\n"],
  ["textOwners value quote",
   "? closing(valueStart) : -1;",
   "? quotedEnd(value, valueStart) : -1;"],
  ["textOwners bracket scan quote",
   "const stop = closing(end); if (stop < 0) { end = value.length; break; }",
   "const stop = quotedEnd(value, end); if (stop < 0) { end = value.length; break; }"],
  ["linear string pair scan",
   "/** End (exclusive) of the quoted string that opens at `start`, read as /\"(?:\\\\.|[^\"\\\\])*\"/ reads it ('.' stops at a line terminator),\n * or a negative number when it does not close: -1 at the end of the text, or -(at + 2) for a backslash at `at` that no escape\n * can follow. */\nfunction stringEnd(text: string, start: number): number {\n  for (let i = start + 1; i < text.length; i++) {\n    const c = text.charCodeAt(i);\n    if (c === 92) {\n      const next = text.charCodeAt(i + 1);\n      if (i + 1 >= text.length) return -1;\n      if (next === 10 || next === 13 || next === 0x2028 || next === 0x2029) return -(i + 2);\n      i++;\n    } else if (c === 34) return i + 1;\n  }\n  return -1;\n}\n/** The key and value strings of every `\"key\" : \"value\"` pair in the text, as the non-overlapping matches of\n * /(\"(?:\\\\.|[^\"\\\\])*\"\\s*:\\s*)(\"(?:\\\\.|[^\"\\\\])*\")/g, in time linear in the text. A quote inside a string that was read is not tried as the\n * start of another (the scan from it reads the same text in the same escape state, so it ends where that scan ended), and a\n * string with no end ends the search. */\nfunction stringPairs(text: string): [string, string][] {\n  const pairs: [string, string][] = [];\n  for (let start = text.indexOf('\"'); start >= 0;) {\n    const keyEnd = stringEnd(text, start);\n    if (keyEnd === -1) break;\n    if (keyEnd < 0) { start = text.indexOf('\"', -keyEnd); continue; }\n    let at = keyEnd;\n    while (at < text.length && JS_SPACE.test(text[at])) at++;\n    if (text[at] === \":\") {\n      at++;\n      while (at < text.length && JS_SPACE.test(text[at])) at++;\n      if (text[at] === '\"') {\n        const valueEnd = stringEnd(text, at);\n        if (valueEnd > 0) { pairs.push([text.slice(start, keyEnd), text.slice(at, valueEnd)]); start = text.indexOf('\"', valueEnd); continue; }\n        if (valueEnd === -1) break;\n        start = text.indexOf('\"', -valueEnd); continue;\n      }\n    }\n    start = text.indexOf('\"', keyEnd - 1);\n  }\n  return pairs;\n}\nconst JS_SPACE = /\\s/;\n",
   ""],
  ["secret minimum in code points, known secrets",
   "  const input = [...new Set(secrets.filter(s => longEnoughSecret(s) && s !== replacement))];\n",
   "  const input = [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))];\n"],
  ["secret minimum in code points, whole secret",
   "const wholeSecret = (value: string, secrets: readonly string[]) => longEnoughSecret(value) && value !==",
   "const wholeSecret = (value: string, secrets: readonly string[]) => value.length >= 8 && value !=="],
  ["one read of each string token that holds known secrets",
   "  let tokenIndex = 0, crossingSecret = false, decodedToken: (SourceRange & { string: boolean; key?: boolean }) | undefined, decoded = \"\";\n  const spelled = new Map<string, boolean>();\n  for (const range of ranges) {\n    while (tokenIndex < tokens.length && tokens[tokenIndex].end <= range.start) tokenIndex++;\n    const token = tokens[tokenIndex];\n    if (!token || range.start < token.start || range.end > token.end) { crossingSecret = true; break; }\n    if (token.string) {\n      // A token is read once, however many secrets it holds: the ranges are in order, so the ones of a token follow each other.\n      if (decodedToken !== token) { decodedToken = token; decoded = JSON.parse(value.slice(token.start, token.end)); spelled.clear(); }\n      // The decoded walker owns ordinary matches, including repeatedly encoded\n      // JSON numbers. Plain substitution here would invalidate that inner JSON.\n      // Only a raw spelling absent from the decoded value needs a source edit.\n      const spelling = value.slice(range.start, range.end);\n      let found = spelled.get(spelling);\n      if (found === undefined) spelled.set(spelling, found = decoded.includes(spelling));\n      // A match that takes a quote of the string is not where the decoded string holds the same spelling, if it holds it at all: the\n      // decoded walker may replace that other place in another order, and leave this one to the reference's read of the text.\n      if (!found || range.start === token.start || range.end === token.end) {\n",
   "  let tokenIndex = 0, crossingSecret = false;\n  for (const range of ranges) {\n    while (tokenIndex < tokens.length && tokens[tokenIndex].end <= range.start) tokenIndex++;\n    const token = tokens[tokenIndex];\n    if (!token || range.start < token.start || range.end > token.end) { crossingSecret = true; break; }\n    if (token.string) {\n      const decoded: string = JSON.parse(value.slice(token.start, token.end));\n      // The decoded walker owns ordinary matches, including repeatedly encoded\n      // JSON numbers. Plain substitution here would invalidate that inner JSON.\n      // Only a raw spelling absent from the decoded value needs a source edit.\n      if (!decoded.includes(value.slice(range.start, range.end))) {\n"],
  ["secret minimum in code points, discovered",
   "if (!longEnoughSecret(value) || value === replacement) return; if (found.size >= 1024",
   "if (value.length < 8 || value === replacement) return; if (found.size >= 1024"],
  ["secret minimum in code points, credential headers",
   "filter(value => longEnoughSecret(value) && value !== replacement).sort(",
   "filter(value => value.length >= 8 && value !== replacement).sort("],
  ["secret scan after the scrub's own placeholders",
   "/** Match ORIGINAL bytes before partitioning or rewriting. Longest-first removal\n * splits the remaining source gaps, exactly like knownSecretText; inserted\n * sentinels and existing sentinel segments cannot become a new match.\n * `inserted` (sorted, disjoint) are the spans of the text that the scrub wrote itself, each holding at most one placeholder: such a\n * placeholder bounds a gap like any other, but neither it nor the gap it ends is charged: a scan is charged for the placeholders the\n * text brought, not for those the scrub wrote. A secret's next occurrence is looked for once past each match, not once per gap, so a\n * scan reads the text once per secret however many gaps it has. */\nfunction knownSecretRanges(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget, inserted: readonly SourceRange[] = []): SourceRange[] {\n  const matches: SourceRange[] = [];\n  let gaps: (SourceRange & { free?: boolean })[] = [], at = 0, k = 0;\n  for (let next = value.indexOf(replacement); next >= 0; next = value.indexOf(replacement, at)) {\n    while (k < inserted.length && inserted[k].end <= next) k++;\n    const free = k < inserted.length && inserted[k].start <= next && next + replacement.length <= inserted[k].end;\n    if (!free) charge(budget, depth);\n    if (next > at) gaps.push(free ? { start: at, end: next, free } : { start: at, end: next });\n    at = next + replacement.length;\n  }\n  if (at < value.length) gaps.push({ start: at, end: value.length });\n  // How many gaps each secret is charged for: every gap but those an inserted placeholder ends.\n  let charged = gaps.filter(gap => !gap.free).length;\n  for (const secret of prepareSecrets(secrets)) {\n    charge(budget, depth);\n    let remaining: typeof gaps = [], read = 0, kept = 0, matched = false;\n    // The first occurrence at or after where the last look started: the gaps are in order, so one that ends past a gap is the first\n    // occurrence of each later gap it is not before. A secret that is not a string is looked for as the scan always looked for it.\n    let found = -1, cached = false;\n    for (let g = 0; g < gaps.length; g++) {\n      if (cached && found < 0) {\n        // No occurrence is left: the other gaps stay as they are, and are charged as if each were read.\n        kept += charged - read;\n        for (; read < charged; read++) charge(budget, depth);\n        remaining = matched ? remaining.concat(gaps.slice(g)) : gaps;\n        break;\n      }\n      const gap = gaps[g];\n      if (!gap.free) { charge(budget, depth); read++; }\n      let start = gap.start;\n      if (!cached || found >= 0 && found < start) found = value.indexOf(secret, start);\n      for (; found >= 0 && found + secret.length <= gap.end; found = value.indexOf(secret, start)) {\n        charge(budget, depth);\n        matched = true;\n        if (found > start) { remaining.push({ start, end: found }); kept++; }\n        matches.push({ start: found, end: found + secret.length });\n        start = found + secret.length;\n      }\n      cached = typeof secret === \"string\";\n      if (start < gap.end) { remaining.push(gap.free ? { start, end: gap.end, free: true } : { start, end: gap.end }); if (!gap.free) kept++; }\n    }\n    gaps = remaining; charged = kept;\n  }\n  return matches.sort((a, b) => a.start - b.start);\n}",
   "/** Match ORIGINAL bytes before partitioning or rewriting. Longest-first removal\n * splits the remaining source gaps, exactly like knownSecretText; inserted\n * sentinels and existing sentinel segments cannot become a new match. */\nfunction knownSecretRanges(value: string, secrets: readonly string[], depth: number, budget: PrivacyBudget): SourceRange[] {\n  const matches: SourceRange[] = [];\n  let gaps: SourceRange[] = [], at = 0;\n  for (let next = value.indexOf(replacement); next >= 0; next = value.indexOf(replacement, at)) {\n    charge(budget, depth);\n    if (next > at) gaps.push({ start: at, end: next });\n    at = next + replacement.length;\n  }\n  if (at < value.length) gaps.push({ start: at, end: value.length });\n  for (const secret of prepareSecrets(secrets)) {\n    charge(budget, depth);\n    const remaining: SourceRange[] = [];\n    for (const gap of gaps) {\n      charge(budget, depth);\n      let start = gap.start;\n      for (let found = value.indexOf(secret, start); found >= 0 && found + secret.length <= gap.end; found = value.indexOf(secret, start)) {\n        charge(budget, depth);\n        if (found > start) remaining.push({ start, end: found });\n        matches.push({ start: found, end: found + secret.length });\n        start = found + secret.length;\n      }\n      if (start < gap.end) remaining.push({ start, end: gap.end });\n    }\n    gaps = remaining;\n  }\n  return matches.sort((a, b) => a.start - b.start);\n}"],
];
// Cross-owner and path-budget edits: the pair reads taken out with an owner a secret crosses, the path budget in the number walk, the
// whole JSON owner. restoreV4 takes them out again so the pinned digest holds; the oracle reads them (extendOracleV4), since they change
// what is read, not how fast.
const CROSS_OWNER_RESTORE: [label: string, current: string, base: string][] = [
  ["source range may name its replacement",
   "interface SourceRange { start: number; end: number; /** What replaces the range, when not the placeholder. */ text?: string }",
   "interface SourceRange { start: number; end: number }"],
  ["cross-owner edits header",
   "function crossOwnerEdits(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget, reads?: { budget: PrivacyBudget; secrets: ReadonlySet<string> }, readBefore?: (position: number) => boolean, secretRun?: (run: string) => boolean): SourceRange[] {\n  if (!ranges.length) return [];\n",
   "function redactCrossOwnerSecrets(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget): string {\n  if (!ranges.length) return value;\n"],
  ["a secret inside one JSON string is the owner's",
   "    // A secret inside a JSON string is replaced by the owner, in the string's own encoding. One that holds a quote or a control\n    // character, or a bracket, or starts or ends inside a backslash sequence, is not a piece of one string, so it takes the owner out whole, as the owner redacts itself, before any later read\n    // of the text changes the owner.\n    const enclosed = !!enclosing && enclosing.start <= range.start && enclosing.end >= range.end;\n    if (enclosed && enclosing.kind === \"json\" && (/[\"\\u0000-\\u001f[\\]{}]/.test(value.slice(range.start, range.end)) || splitsSequence(value, range))) {\n      const before = edits.at(-1);\n      const startRun = () => enclosing.start === range.start && !!secretRun?.(value.slice(Math.max(0, enclosing.start - 3), enclosing.start + 1));\n      const endRun = () => enclosing.end === range.end && !!secretRun?.(value.slice(enclosing.end - 1, enclosing.end + 3));\n      // A later secret merged into the owner's edit takes the owner's first or last character on the same reading as below.\n      if (before && enclosing.start < before.end) {\n        if (enclosing.end > before.end || startRun() || endRun()) delete before.text;\n        before.end = Math.max(before.end, enclosing.end);\n      }\n      // A JSON string that is the secret byte for byte is replaced as the reference replaces it, by the bare placeholder: the quotes\n      // go with it, so the pair rule reads a name around it as one name.\n      // A secret that takes the owner's first or last character (a quote or a bracket) where that character and the three outside it are\n      // a run of a known secret takes it with the owner: the placeholder is bare, so the run is not left beside the placeholder. The\n      // reference replaces the longer secret first, and the shorter one that holds the run is gone with it.\n      else edits.push(typeof enclosing.parsed === \"string\" && enclosing.start === range.start && enclosing.end === range.end\n        || startRun() || endRun()\n        ? { start: enclosing.start, end: enclosing.end } : { start: enclosing.start, end: enclosing.end, text: JSON.stringify(replacement) });\n      continue;\n    }\n    if (enclosed && (enclosing.kind === \"json\" || enclosing.kind === \"field\" && range.start >= enclosing.valueStart! && !/[\"\\u0000-\\u001f]/.test(value.slice(range.start, range.end)) && !splitsSequence(value, range)\n        || enclosing.kind === \"wrapper\" && enclosing.start < range.start && range.end < enclosing.end\n        || enclosing.kind === \"tail\" && enclosing.start < range.start)) {\n      // Brackets keep a secret that starts past their opener only while no edit comes before them: an edit before them can pair the\n      // quotes and brackets differently, so the text read again finds other owners, and a tail's opening run taken out as one\n      // placeholder would cut the secret. The secret is then replaced where it stands, as the reference replaces it. A pair or value\n      // shape the whole-text read redacts before the brackets is such an edit too: the brackets are read again once it is made.\n      if (enclosing.kind !== \"wrapper\" && enclosing.kind !== \"tail\" || !edits.length && !readBefore?.(enclosing.start)) continue;\n      const prior = edits.at(-1);\n      if (!prior || range.start >= prior.end) edits.push({ start: range.start, end: range.end });\n      continue;\n    }\n",
   "    if (enclosing && enclosing.start <= range.start && enclosing.end >= range.end\n      && (enclosing.kind === \"json\" || enclosing.kind === \"field\" && range.start >= enclosing.valueStart!\n        || enclosing.kind === \"wrapper\" && enclosing.start < range.start && range.end < enclosing.end\n        || enclosing.kind === \"tail\" && enclosing.start < range.start)) continue;\n"],
  ["pair reads are charged to a budget of their own",
   "    // A range the pair rule reads, which is not one known secret, is charged with the owners and URLs it crosses to a budget of its own,\n    // outside the scan: the reference charges nothing for what it reads in a text, so a text with many pairs is not refused for them.\n    const pay = reads && !reads.secrets.has(range.start + \":\" + range.end) ? () => chargeAside(reads.budget, depth) : () => charge(budget, depth);\n    pay();\n    while (ownerIndex < owners.length && owners[ownerIndex].end <= range.start) ownerIndex++;",
   "    charge(budget, depth);\n    while (ownerIndex < owners.length && owners[ownerIndex].end <= range.start) ownerIndex++;"],
  ["owners a pair read crosses are charged with it",
   "      pay();\n      start = Math.min(start, owners[i].start);",
   "      charge(budget, depth);\n      start = Math.min(start, owners[i].start);"],
  ["URLs a pair read crosses are charged with it",
   "      pay();\n      // A URL-contained credential",
   "      charge(budget, depth);\n      // A URL-contained credential"],
  ["pair reads charged aside and the depth of JSON read as text",
   "/** Charges a budget of its own outside the scan that runs, so the scan's tally does not count it. */\nfunction chargeAside(budget: PrivacyBudget, depth: number): void {\n  const tally = scanTally;\n  scanTally = undefined;\n  try { charge(budget, depth); } finally { scanTally = tally; }\n}\n/** Whether a parsed JSON tree whose root is read at `depth` (each child one level deeper) reaches past the depth limit. */\nfunction pastDepth(node: unknown, depth: number): boolean {\n  if (depth > catalog.limits.max_depth) return true;\n  return node !== null && typeof node === \"object\" && Object.values(node).some(child => pastDepth(child, depth + 1));\n}\n",
   ""],
  ["merged edit drops its own replacement",
   "    if (prior && start < prior.end) { if (end > prior.end) delete prior.text; prior.end = Math.max(prior.end, end); }\n",
   "    if (prior && start < prior.end) prior.end = Math.max(prior.end, end);\n"],
  ["cross-owner edits, reference redactions and joined ranges",
   "  return edits;\n}\nfunction redactCrossOwnerSecrets(value: string, ranges: SourceRange[], owners: TextOwner[], depth: number, budget: PrivacyBudget): string {\n  return replaceEdits(value, crossOwnerEdits(value, ranges, owners, depth, budget));\n}\nfunction replaceEdits(value: string, edits: readonly SourceRange[]): string {\n  let end = 0, out = \"\";\n  for (const edit of edits) { out += value.slice(end, edit.start) + (edit.text ?? replacement); end = edit.end; }\n  return out + value.slice(end);\n}\n/** A stage that replaced `runs` (sorted, disjoint, in the coordinates of the text it read): each run's position in the text it wrote. */\ninterface StageRuns { runs: SourceRange[]; from: number[] }\nfunction stageRuns(runs: SourceRange[]): StageRuns {\n  const from: number[] = [];\n  let shift = 0;\n  for (const run of runs) { from.push(run.start + shift); shift += replacement.length - (run.end - run.start); }\n  return { runs, from };\n}\n/** The position in the text a stage read of a position in the text it wrote: a position inside a placeholder moves to the start (or,\n * for the end of a range, the end) of the run the placeholder stands for. Binary search over the runs. */\nfunction beforeStage(position: number, stage: StageRuns, isEnd: boolean): number {\n  const { runs, from } = stage, L = replacement.length;\n  // The first run whose placeholder ends at or after the position (an end) or after it (a start).\n  let low = 0, high = runs.length;\n  while (low < high) {\n    const mid = (low + high) >> 1;\n    if (isEnd ? from[mid] + L >= position : from[mid] + L > position) high = mid; else low = mid + 1;\n  }\n  const shift = low === 0 ? 0 : from[low - 1] + L - runs[low - 1].end;\n  if (low < runs.length && (isEnd ? position > from[low] : position >= from[low])) return isEnd ? runs[low].end : runs[low].start;\n  return position - shift;\n}\n/** The ranges of the text, in its own coordinates, that the reference's value shapes and pair rule redact once known secrets (`known`)\n * are replaced: the reference reads the pair rule over the text with the secrets in place of their bytes, so a name beside a secret is\n * read there. A secret that crosses a JSON owner takes the whole owner out of the text, and with it the name the pair rule needs; these\n * ranges are taken out as well. With `empty`, a pair whose value is empty (a separator just before a line break) is kept, as an empty\n * range where the pair rule writes its placeholder: a secret that runs from the separator over the line break cuts that pair. */\nfunction referenceRedactions(value: string, known: readonly SourceRange[], empty = false): SourceRange[] {\n  const stages: StageRuns[] = [];\n  let current = value;\n  const stage = (runs: SourceRange[]) => { stages.push(stageRuns(runs)); current = replaceEdits(current, runs); };\n  stage(known.map(range => ({ ...range })));\n  for (const shape of valueShapes) {\n    const runs: SourceRange[] = (typeof shape === \"function\" ? jwtRuns(current) : [...current.matchAll(shape as RegExp)].map(match => [match.index!, match.index! + match[0].length] as [number, number]))\n      .filter(([start, end]) => end > start).map(([start, end]) => ({ start, end }));\n    if (runs.length) stage(runs);\n  }\n  const pairs: [number, number][] = [];\n  credentialPairs(current, [], pairs);\n  stages.push(stageRuns(pairs.map(([start, end]) => ({ start, end }))));\n  const found: SourceRange[] = [];\n  for (let s = 1; s < stages.length; s++) {\n    for (const run of stages[s].runs) {\n      let start = run.start, end = run.end;\n      for (let k = s - 1; k >= 0; k--) { start = beforeStage(start, stages[k], false); end = beforeStage(end, stages[k], true); }\n      if (end > start || empty && s === stages.length - 1) found.push({ start, end });\n    }\n  }\n  return found;\n}\n/** The first of the sorted, disjoint ranges that ends after the position (binary search). */\nfunction firstEnding<T extends SourceRange>(sorted: readonly T[], position: number): T | undefined {\n  let low = 0, high = sorted.length;\n  while (low < high) { const mid = (low + high) >> 1; if (sorted[mid].end > position) high = mid; else low = mid + 1; }\n  return sorted[low];\n}\n/** Ranges sorted by start, the overlapping ones joined. */\nfunction joinRanges(ranges: readonly SourceRange[]): SourceRange[] {\n  const out: SourceRange[] = [];\n  for (const range of [...ranges].sort((a, b) => a.start - b.start || a.end - b.end)) {\n    const last = out.at(-1);\n    if (last && range.start < last.end) last.end = Math.max(last.end, range.end);\n    else out.push({ ...range });\n  }\n  return out;\n}\n",
   "  let end = 0, out = \"\";\n  for (const edit of edits) { out += value.slice(end, edit.start) + replacement; end = edit.end; }\n  return out + value.slice(end);\n}\n"],
  ["json number walk reads the name path budget first",
   "    // A name of more than max_depth components exports the placeholder before the depth budget is read.\n    pathBudget(path); charge(budget, level); space(); parent?.delete(key);\n",
   "    charge(budget, level); space(); parent?.delete(key);\n"],
  ["text owners read the text length once",
   "function textOwners(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {\n  // The text was charged where it was read as a value (walkValue, scrubText), once, as the reference does.\n  charge(budget, depth);",
   "function textOwners(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {\n  charge(budget, depth, value.length);"],
  ["JSON source pass reads its own budget",
   "  // The reference charges a complete JSON text once for its own length and once per node of the tree it parses. The passes below that\n  // read the same text again (its source spans, the audit of repeated numbers, the two renderings) read it against a node budget\n  // of their own and no text budget: they bound their own work and leave the charge to the one walk of the parsed tree.\n  const extra = workBudget;\n  const source = jsonNumbers(value, parsed, secrets, depth, extra());",
   "  const source = jsonNumbers(value, parsed, secrets, depth, budget);"],
  ["JSON number audit reads its own budget",
   "scalar.level, inheritedAudio, extra(), scalar.hints,",
   "scalar.level, inheritedAudio, budget, scalar.hints,"],
  ["JSON rebuilt numbers read their own budget",
   "jsonNumbers(value, parsed, [], depth, extra())",
   "jsonNumbers(value, parsed, [], depth, budget)"],
  ["JSON renderings read their own budget",
   "  const rendered = renderJSON(clean, numbers.output, extra(), depth, [], numbers.root);\n  const numericOnly = renderJSON(parsed, numbers.input, extra(), depth, secrets, numbers.root);",
   "  const rendered = renderJSON(clean, numbers.output, budget, depth, [], numbers.root);\n  const numericOnly = renderJSON(parsed, numbers.input, budget, depth, secrets, numbers.root);"],
  ["scans of a text read a budget of their own",
   "/** A budget for the passes that read a text again (its source spans, its owners, the known secrets in it, the two renderings of a tree):\n * they bound their own work and leave the node and text charge to the one walk that the reference charges. Its limit is a multiple of\n * the node limit, so a text the reference accepts is never refused by a scan of it. */\nconst workBudget = (): PrivacyBudget => ({ nodes: catalog.limits.max_nodes * 4, text: Infinity, seen: new WeakSet() });\n/** While a scan of a text runs (see scanned), the nodes it charges are tallied here and the node limit less the one node of the value\n * that holds the text, not what is left of the budget, bounds it. */\nlet scanTally: { nodes: number; last: number } | undefined;\nfunction charge(budget: PrivacyBudget, depth: number, length = 0): void {\n  const left = --budget.nodes;\n  if (scanTally) scanTally.last = left;\n  if (scanTally ? ++scanTally.nodes >= catalog.limits.max_nodes || depth > catalog.limits.max_depth : left < 0 || depth > catalog.limits.max_depth) throw new Error(\"Privacy structure budget\");\n  if ((budget.text -= length) < 0) throw new Error(\"Privacy text budget\");\n}\n/** A scan of a text (its known secrets, its owners, the secrets that cross them) bounds its own work by the node limit and leaves the\n * node charge of the value to the one walk that the reference charges: what the scan charged is given back when it completes, so a\n * string is one node, as the reference counts it. A scan that throws keeps what it charged. */\nfunction scanned<T>(budget: PrivacyBudget, run: () => T): T {\n  const outer = scanTally, tally = { nodes: 0, last: 0 };\n  scanTally = tally;\n  let result: T;\n  try { result = run(); } finally { scanTally = outer; }\n  // Written from the last count the scan saw, so the budget is not read an extra time.\n  if (tally.nodes) budget.nodes = tally.last + tally.nodes;\n  return result;\n}\n",
   "function charge(budget: PrivacyBudget, depth: number, length = 0): void {\n  if (--budget.nodes < 0 || depth > catalog.limits.max_depth) throw new Error(\"Privacy structure budget\");\n  if ((budget.text -= length) < 0) throw new Error(\"Privacy text budget\");\n}\n"],
  ["whole JSON owner",
   "/** The one owner of a text that is a complete JSON document. A document is read by its own parse, whatever its nesting: the name path\n * budget and then the depth budget decide, in the order the reference reads them, instead of a scan of the brackets in the text. */\nfunction wholeOwner(value: string, depth: number, budget: PrivacyBudget): TextOwner[] {\n  // The text was charged where it was read as a value (walkValue, scrubText), once, as the reference does.\n  charge(budget, depth);\n  const start = value.search(/\\S/);\n  let end = value.length;\n  while (/\\s/.test(value[end - 1])) end--;\n  charge(budget, depth);\n  return [{ start, end, kind: \"json\", parsed: JSON.parse(value.slice(start, end)) }];\n}\n",
   ""],
  ["whole JSON object or array",
   "/** A text that is one complete JSON object or array. The reference reads a string literal, number or keyword as raw text, so those are not read leaf by leaf. */\nfunction wholeContainer(value: string): boolean {\n  return /^\\s*[\\[{]/.test(value) && wholeJSON(value);\n}\n",
   ""],
  ["textOwners bracket scan depth",
   "else if (next === \"{\" || next === \"[\") nesting++;",
   "else if (next === \"{\" || next === \"[\") { if (++nesting + depth > catalog.limits.max_depth) throw new Error(\"Privacy structure budget\"); }"],
  ["inner ranges added to the edits",
   "/** The edits with the ranges that no owner reads added: an edit that already covers a range keeps its own text. */\nfunction withPlainRanges(edits: SourceRange[], inner: SourceRange[]): SourceRange[] {\n  if (!inner.length) return edits;\n  const out: SourceRange[] = [];\n  for (const edit of [...edits, ...inner].sort((a, b) => a.start - b.start || a.end - b.end)) {\n    const last = out.at(-1);\n    if (last && edit.start < last.end) { if (edit.end > last.end) { delete last.text; last.end = edit.end; } }\n    else out.push({ ...edit });\n  }\n  return out;\n}\n",
   ""],
  ["a range that starts or ends inside a backslash sequence",
   "/** Whether the range starts or ends inside a backslash sequence of a JSON string: an odd run of backslashes ends the range or stands just before it. */\nfunction splitsSequence(value: string, range: SourceRange): boolean {\n  let before = 0, after = 0;\n  while (range.start - 1 - before >= 0 && value[range.start - 1 - before] === \"\\\\\") before++;\n  while (range.end - 1 - after >= range.start && value[range.end - 1 - after] === \"\\\\\") after++;\n  return before % 2 === 1 || after % 2 === 1;\n}\n",
   ""],
  ["a secret that cuts a credential name in a text that opens with a bracket",
   "/** Whether a range the pair rule and value shapes redact on the text with its known secrets in place is left out of what the reference\n * redacts once the secrets are replaced, together with the secrets themselves (sorted, joined ranges; one forward pass). */\nfunction secretCutsName(value: string, ranges: readonly SourceRange[]): boolean {\n  const secrets = joinRanges(ranges), cover = joinRanges([...ranges, ...referenceRedactions(value, ranges)]);\n  let k = 0, j = 0;\n  for (const range of joinRanges(referenceRedactions(value, []))) {\n    while (k < cover.length && cover[k].end <= range.start) k++;\n    while (j < secrets.length && secrets[j].end <= range.start) j++;\n    // A secret that takes out a letter or digit of the range takes out part of the value itself, not only of its name.\n    let inValue = false;\n    for (let i = j; i < secrets.length && secrets[i].start < range.end && !inValue; i++) inValue = /[\\p{L}\\p{N}]/u.test(value.slice(Math.max(range.start, secrets[i].start), Math.min(range.end, secrets[i].end)));\n    if (inValue) continue;\n    // What the range keeps readable once the secrets and the reference's own read are taken out: a letter or digit there is a value left.\n    let at = range.start;\n    for (let i = k; i < cover.length && cover[i].start < range.end; i++) {\n      if (cover[i].start > at && /[\\p{L}\\p{N}]/u.test(value.slice(at, cover[i].start))) return true;\n      at = Math.max(at, cover[i].end);\n    }\n    if (at < range.end && /[\\p{L}\\p{N}]/u.test(value.slice(at, range.end))) return true;\n  }\n  return false;\n}\n",
   ""],
  ["the runs of the known secrets, read once per secrets array",
   "/** The runs of one to four characters of each known secret, of any length, as the secrets were given. Read once per secrets array and\n * kept while the array holds the same secrets, so the texts of one value or span do not read the secrets again. */\nlet secretRunSets = new WeakMap<object, { input: readonly unknown[]; runs: ReadonlySet<string> }>();\nlet secretRunAdmissions = 0;\nfunction secretRuns(secrets: readonly string[]): ReadonlySet<string> {\n  const input = Array.from(secrets as readonly unknown[]);\n  const keyed = secrets !== null && (typeof secrets === \"object\" || typeof secrets === \"function\");\n  const cached = keyed ? secretRunSets.get(secrets) : undefined;\n  if (cached && cached.input.length === input.length && input.every((secret, i) => secret === cached.input[i])) return cached.runs;\n  const runs = new Set<string>();\n  for (const secret of input) {\n    if (typeof secret !== \"string\") continue;\n    for (let i = 0; i < secret.length; i++) for (let k = 1; k <= 4 && i + k <= secret.length; k++) runs.add(secret.slice(i, i + k));\n  }\n  if (keyed) {\n    if (!cached) {\n      if (secretRunAdmissions >= 64) { secretRunSets = new WeakMap(); secretRunAdmissions = 0; }\n      secretRunAdmissions++;\n    }\n    // Detached copies, as prepareSecrets keeps them: the cache does not hold slices of the caller's strings.\n    secretRunSets.set(secrets, { input: input.map(secret => typeof secret === \"string\" ? secret.split(\"\").join(\"\") : secret), runs });\n  }\n  return runs;\n}\n",
   ""],
  ["the runs of known secrets that a decoded read shows, compared with its raw bytes",
   "const isHighSurrogate = (c: number) => c >= 0xd800 && c <= 0xdbff;\nconst isLowSurrogate = (c: number) => c >= 0xdc00 && c <= 0xdfff;\n/** The quoted runs of a text that /\"(?:[^\"\\\\\\n]|\\\\.)*\"/g matches, in one pass: a quote whose run reaches a line break or the end of the text\n * without a closing quote is not one, and neither is any quote inside that run (each sees the same escapes), so the scan goes on after it. */\nfunction stringLiterals(text: string): [number, number][] {\n  const found: [number, number][] = [];\n  for (let at = text.indexOf('\"'); at >= 0;) {\n    let k = at + 1, closed = false;\n    while (k < text.length) {\n      const c = text.charCodeAt(k);\n      if (c === 34) { closed = true; break; }\n      if (c === 10) break;\n      if (c === 92) { if (k + 1 >= text.length || text.charCodeAt(k + 1) === 10) break; k += 2; } else k++;\n    }\n    if (closed) { found.push([at, k + 1]); at = text.indexOf('\"', k + 1); } else at = k < text.length ? text.indexOf('\"', k + 1) : -1;\n  }\n  return found;\n}\n/** The runs of four code points of each known secret, as the secrets were given (a reader reads code points, not code units). Read once per\n * secrets array and kept while it holds the same secrets. */\nlet codePointRunSets = new WeakMap<object, { input: readonly unknown[]; runs: ReadonlySet<string> }>();\nlet codePointRunAdmissions = 0;\nfunction codePointRuns(secrets: readonly string[]): ReadonlySet<string> {\n  const input = Array.from(secrets as readonly unknown[]);\n  const keyed = secrets !== null && (typeof secrets === \"object\" || typeof secrets === \"function\");\n  const cached = keyed ? codePointRunSets.get(secrets) : undefined;\n  if (cached && cached.input.length === input.length && input.every((secret, i) => secret === cached.input[i])) return cached.runs;\n  const runs = new Set<string>();\n  for (const secret of input) {\n    if (typeof secret !== \"string\") continue;\n    const points = Array.from(secret);\n    for (let i = 0; i + 4 <= points.length; i++) runs.add(points.slice(i, i + 4).join(\"\"));\n  }\n  if (keyed) {\n    if (!cached) {\n      if (codePointRunAdmissions >= 64) { codePointRunSets = new WeakMap(); codePointRunAdmissions = 0; }\n      codePointRunAdmissions++;\n    }\n    codePointRunSets.set(secrets, { input: input.map(secret => typeof secret === \"string\" ? secret.split(\"\").join(\"\") : secret), runs });\n  }\n  return runs;\n}\n/** The most times each run of four code points of a known secret is shown in one reading of a JSON string literal of the output: the literal\n * itself, the string it encodes, and each JSON text, JSON string literal, escape and percent-encoding read inside it, again and again to\n * the depth limit. Placeholders break runs. Undefined where the readings pass a bound of their own, a multiple of the literal's length. */\nfunction shownRuns(literal: string, runs: ReadonlySet<string>): Map<string, number> | undefined {\n  const shown = new Map<string, number>(), seen = new Set<string>(), starts = new Set<number>();\n  for (const run of runs) starts.add(run.charCodeAt(0));\n  let work = 16 * literal.length + 4096, layer = [literal];\n  for (let depth = 0; layer.length; depth++) {\n    if (depth > catalog.limits.max_depth) return undefined;\n    const next: string[] = [];\n    const add = (view: string) => { if (!seen.has(view)) { if ((work -= view.length + 1) < 0) throw shownRuns; next.push(view); } };\n    try {\n      for (const view of layer) {\n        if (seen.has(view)) continue;\n        seen.add(view);\n        const counts = new Map<string, number>();\n        for (const part of view.split(replacement)) for (let i = 0; i + 4 <= part.length; i++) {\n          // Runs of four code points: a character outside the basic plane is one code point of two code units.\n          const low = part.charCodeAt(i);\n          if (!starts.has(low) || low >= 0xdc00 && low <= 0xdfff && i > 0 && isHighSurrogate(part.charCodeAt(i - 1))) continue;\n          let end = i;\n          for (let k = 0; k < 4 && end < part.length; k++) end += isHighSurrogate(part.charCodeAt(end)) && end + 1 < part.length && isLowSurrogate(part.charCodeAt(end + 1)) ? 2 : 1;\n          const run = part.slice(i, end);\n          if (runs.has(run)) counts.set(run, (counts.get(run) ?? 0) + 1);\n        }\n        for (const [run, count] of counts) if (count > (shown.get(run) ?? 0)) shown.set(run, count);\n        if (/^\\s*[[{\"]/.test(view)) {\n          let parsed: unknown;\n          try { parsed = JSON.parse(view); } catch { parsed = undefined; }\n          const walk = (node: unknown, level: number): void => {\n            if (level > catalog.limits.max_depth) throw shownRuns;\n            if (typeof node === \"string\") add(node);\n            else if (node !== null && typeof node === \"object\") for (const [key, child] of Object.entries(node)) { add(key); walk(child, level + 1); }\n          };\n          walk(parsed, 0);\n        }\n        for (const [start, end] of stringLiterals(view)) {\n          let literal: unknown;\n          try { literal = JSON.parse(view.slice(start, end)); } catch { continue; }\n          if (typeof literal === \"string\") add(literal);\n        }\n        if (view.includes(\"\\\\\")) add(view.replace(/\\\\(u[0-9a-fA-F]{4}|[\"\\\\/bfnrt])/g, (_, escape: string) => escape[0] === \"u\" ? String.fromCharCode(parseInt(escape.slice(1), 16)) : ({ b: \"\\b\", f: \"\\f\", n: \"\\n\", r: \"\\r\", t: \"\\t\" } as Record<string, string>)[escape] ?? escape));\n        if (/%[0-9A-Fa-f]{2}/.test(view)) add(unquotePlus(view));\n      }\n    } catch (error) { if (error === shownRuns) return undefined; throw error; }\n    layer = next;\n  }\n  return shown;\n}\n/** Whether no reading of `clean` shows a run of a known secret more often than a reading of the fallback read's output does (both within\n * their bounds). The fallback is read only where `clean` shows a run. */\nfunction lessShown(clean: string, secrets: readonly string[], fallback: () => string): boolean {\n  const runs = codePointRuns(secrets), shown = shownRuns(clean, runs);\n  if (shown === undefined) return false;\n  if (!shown.size) return true;\n  const hidden = shownRuns(fallback(), runs);\n  return hidden !== undefined && [...shown].every(([run, count]) => count <= (hidden.get(run) ?? 0));\n}\n",
   ""],
  ["a text that begins as a JSON object or array and is not one JSON value",
   "// ---- Shared privacy spec v6 round 3: a text that begins as a JSON object or array and is not one JSON value. ----\n/** Where a text begins as the reference reads it (Python's str.lstrip, then byte order marks, then str.lstrip again). */\nfunction jsonTextStart(value: string): number {\n  let i = 0;\n  while (i < value.length && pySpace(value[i])) i++;\n  while (value[i] === \"\\ufeff\") i++;\n  while (i < value.length && pySpace(value[i])) i++;\n  return i;\n}\nconst JSON_WHITESPACE = new Set(\" \\t\\n\\r\");\nconst JSON_CONSTANTS = [\"NaN\", \"Infinity\", \"-Infinity\"] as const;\nconst JSON_NUMBER_AT = /-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;\n/**\n * One iterative pass of the JSON reader of the reference over text from `start`: the end of the JSON value read there (or -1 when the\n * text stops being JSON before a value is complete), the positions of NaN, Infinity and -Infinity read as values, and whether an\n * object read to its end repeats a member name (names compared as JSON reads them; an object that never closes is not counted).\n */\nfunction readJSONPrefix(text: string, start: number): { end: number; repeated: boolean; constants: [number, number][] } {\n  const stack: ({ names: Set<string>; repeated: boolean } | undefined)[] = [], constants: [number, number][] = [];\n  let i = start, repeated = false;\n  const space = () => { while (i < text.length && JSON_WHITESPACE.has(text[i])) i++; };\n  const string = (): string | undefined => {\n    // A string: no control character below U+0020, each escape one of the JSON forms.\n    const from = i++;\n    while (i < text.length) {\n      const c = text.charCodeAt(i);\n      if (c === 34) { i++; try { return JSON.parse(text.slice(from, i)); } catch { return undefined; } }\n      if (c < 0x20) return undefined;\n      if (c === 92) {\n        const n = text[i + 1];\n        if (n === \"u\") { if (!/^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6))) return undefined; i += 6; }\n        else if (n !== undefined && '\"\\\\/bfnrt'.includes(n)) i += 2;\n        else return undefined;\n      } else i++;\n    }\n    return undefined;\n  };\n  const fail = () => ({ end: -1, repeated, constants });\n  // expect: 0 a value, 1 a member name or '}', 2 ',' or the close, 3 a member name (after ',')\n  let expect = 0;\n  for (;;) {\n    if (expect === 0) {\n      if (stack.length) space();\n      const c = text[i];\n      if (c === \"{\") { stack.push({ names: new Set(), repeated: false }); i++; space(); if (text[i] === \"}\") { i++; stack.pop(); expect = 2; } else expect = 3; }\n      else if (c === \"[\") { stack.push(undefined); i++; space(); if (text[i] === \"]\") { i++; stack.pop(); expect = 2; } else expect = 0; continue; }\n      else if (c === '\"') { if (string() === undefined) return fail(); expect = 2; }\n      else {\n        const constant = JSON_CONSTANTS.find(name => text.startsWith(name, i));\n        if (constant) { constants.push([i, i + constant.length]); i += constant.length; expect = 2; }\n        else if (text.startsWith(\"true\", i)) { i += 4; expect = 2; }\n        else if (text.startsWith(\"false\", i)) { i += 5; expect = 2; }\n        else if (text.startsWith(\"null\", i)) { i += 4; expect = 2; }\n        else { JSON_NUMBER_AT.lastIndex = i; const m = JSON_NUMBER_AT.exec(text); if (!m) return fail(); i += m[0].length; expect = 2; }\n      }\n      if (expect === 3) continue;\n    }\n    if (expect === 3) {\n      if (text[i] !== '\"') return fail();\n      const name = string();\n      if (name === undefined) return fail();\n      const object = stack[stack.length - 1]!;\n      if (object.names.has(name)) object.repeated = true; else object.names.add(name);\n      space();\n      if (text[i] !== \":\") return fail();\n      i++; space(); expect = 0; continue;\n    }\n    // expect === 2: after a value\n    if (!stack.length) return { end: i, repeated, constants };\n    space();\n    const top = stack[stack.length - 1], c = text[i];\n    if (c === \",\") { i++; space(); expect = top ? 3 : 0; continue; }\n    if (top && c === \"}\") { if (top.repeated) repeated = true; stack.pop(); i++; expect = 2; continue; }\n    if (!top && c === \"]\") { stack.pop(); i++; expect = 2; continue; }\n    return fail();\n  }\n}\n/** The text with each JSON string form (\\uXXXX and the short forms) replaced by what it spells. */\nconst JSON_SPELLING = /\\\\(?:u([0-9a-fA-F]{4})|([\"\\\\/bfnrt]))/g;\nconst JSON_SIMPLE: Record<string, string> = { '\"': '\"', \"\\\\\": \"\\\\\", \"/\": \"/\", b: \"\\b\", f: \"\\f\", n: \"\\n\", r: \"\\r\", t: \"\\t\" };\nfunction unspell(text: string): string {\n  return text.includes(\"\\\\\") ? text.replace(JSON_SPELLING, (_, hex: string | undefined, simple: string | undefined) => hex ? String.fromCharCode(parseInt(hex, 16)) : JSON_SIMPLE[simple!]) : text;\n}\n/** The quoted names that stand before a colon, in one pass (a string runs to its closing quote or to the end of its line). */\nfunction memberNames(text: string): string[] {\n  const names: string[] = [], length = text.length;\n  for (let i = 0; ;) {\n    i = text.indexOf('\"', i);\n    if (i < 0) return names;\n    const start = ++i;\n    while (i < length) {\n      const c = text[i];\n      if (c === \"\\\\\") i += 2;\n      else if (c === '\"' || c === \"\\n\") break;\n      else i++;\n    }\n    if (i < length && text[i] === '\"') {\n      const end = i++;\n      while (i < length && JSON_WHITESPACE.has(text[i])) i++;\n      if (i < length && text[i] === \":\") names.push(text.slice(start, end));\n    }\n  }\n}\n/** The one reading every name site of the reference uses (version 5): the name as written, else read once. Where `excluded` is given and\n * the reading is an exclusion name, the reading alone decides. */\nfunction nameDecision(raw: string, credential: (name: string) => boolean, excluded?: (name: string) => boolean): boolean {\n  if (!raw.includes(\"%\")) return credential(raw);\n  const [read, undecided] = readName(raw);\n  if (!(excluded !== undefined && excluded(read)) && credential(raw)) return true;\n  return undecided || credential(read) || (read.includes(\"%\") && credential(read.replace(/%/g, \"\")));\n}\nconst countMarkers = (text: string) => text.split(replacement).length - 1;\n/**\n * Whether a text that begins as a JSON object or array and is not one JSON value is the placeholder (version 6, round 3): a text\n * that holds NaN, Infinity or -Infinity and is one JSON value otherwise, when that reading repeats a member name or its export would\n * change it; any other text, when it repeats a member name in an object read to its end before it stops being JSON, or holds a\n * credential name as a quoted member name (read as JSON reads names) or as a pair the pair rule reads only once its JSON string\n * forms are spelled out.\n */\n/** The decisions of the NaN and infinity reading, per list of secrets: a nested JSON string that holds one is walked once, however\n * many layers above it read it again. A text the export would change is the placeholder wherever it is read again; a text it leaves\n * unchanged is read so again at the same depth or nearer the root, where the depth limit is further away. Kept to a bound of\n * characters, then started again. */\nconst constantsDecisions = new WeakMap<readonly string[], { chars: number; decided: Map<string, { changed: boolean; depth: number }> }>();\nconst CONSTANTS_MEMO_CHARS = 1 << 22;\nfunction unreadableJSON(value: string, start: number, secrets: readonly string[], depth: number, budget: PrivacyBudget, ctx: { media: boolean; audio: boolean }, incomplete: boolean): boolean {\n  let prefix: ReturnType<typeof readJSONPrefix>;\n  try { prefix = readJSONPrefix(value, start); } catch { return true; }\n  if (prefix.repeated) return true;\n  const leading = value.slice(0, start);\n  const constants = prefix.end >= 0 && prefix.constants.length > 0 && [...leading].every(c => JSON_WHITESPACE.has(c)) && [...value.slice(prefix.end)].every(c => JSON_WHITESPACE.has(c));\n  // A quoted member name that is a credential name, read as JSON reads names and as a name with percent forms is read once. In a\n  // text that is one JSON value apart from its constants, a name of more than max_depth components exports the whole value as the\n  // placeholder, as the walk of that value does; in any other such text, this text.\n  try {\n    const seen = new Set<string>();\n    for (const name of memberNames(value)) {\n      if (seen.has(name)) continue;\n      seen.add(name);\n      if (nameDecision(unspell(name), credentialName)) return true;\n    }\n  } catch (error) { if (constants && error instanceof PrivacyPathBudget) throw error; return true; }\n  if (constants) {\n    // One JSON value once NaN and the infinities are read as numbers: the placeholder when its export would change it.\n    let memo = constantsDecisions.get(secrets);\n    if (!memo || memo.chars > CONSTANTS_MEMO_CHARS) { memo = { chars: 0, decided: new Map() }; constantsDecisions.set(secrets, memo); }\n    const key = `${+ctx.media}${+ctx.audio}${+incomplete}\\u0000${value}`;\n    const known = memo.decided.get(key);\n    if (known && (known.changed || depth <= known.depth)) return known.changed;\n    let text = \"\", at = 0;\n    for (const [from, to] of prefix.constants) { text += value.slice(at, from) + \"0\"; at = to; }\n    text += value.slice(at);\n    let changed: boolean;\n    try {\n      const parsed: unknown = JSON.parse(text);\n      const copy: PrivacyBudget = { nodes: budget.nodes, text: budget.text, seen: new WeakSet() };\n      const clean = walkValue(parsed, secrets, \"\", depth + 1, ctx.audio, copy, noHints, true, ctx.media, incomplete, true);\n      changed = JSON.stringify(clean) !== JSON.stringify(parsed);\n    } catch (error) {\n      // A name of more than max_depth components exports the whole value as the placeholder; any other failure, this text.\n      if (error instanceof PrivacyPathBudget) throw error;\n      changed = true;\n    }\n    memo.decided.set(key, { changed, depth });\n    memo.chars += key.length;\n    return changed;\n  }\n  const plain = unspell(value);\n  if (plain === value) return false;\n  return countMarkers(credentialPairs(plain)) - countMarkers(plain) > countMarkers(credentialPairs(value)) - countMarkers(value);\n}\n",
   ""],
];
function swapOnce(input: string, from: string, to: string): string {
  if (input.split(from).length !== 2) throw new Error("Privacy oracle source boundary drift: " + JSON.stringify(from.slice(0, 90)));
  return input.replace(from, () => to);
}
function restoreV4(input: string): string {
  let out = replaceSection(input, V4_START, "export interface PrivacyBudget", "");
  for (const [, current, base] of V4_SWAPS) out = swapOnce(out, current, base);
  for (const [, current, base] of PY_SWAPS) out = swapOnce(out, current, base);
  for (const [, current, base] of R3_RESTORE) out = swapOnce(out, current, base);
  for (const [, current, base] of CROSS_OWNER_RESTORE) out = swapOnce(out, current, base);
  return out;
}
const v4Section = source.slice(source.indexOf(V4_START), source.indexOf(V4_END) + V4_END.length);
// Python's sorted(secrets, key=lambda s: (-len(s), s)): longest first in code points, equal lengths by code point. Written with
// code point arrays, apart from the runtime's own comparator.
const ORACLE_SECRET_ORDER = `function referenceSecretOrder(a: string, b: string): number {
  const p = Array.from(a, c => c.codePointAt(0)!), q = Array.from(b, c => c.codePointAt(0)!);
  if (p.length !== q.length) return q.length - p.length;
  for (let i = 0; i < p.length; i++) if (p[i] !== q[i]) return p[i] - q[i];
  return 0;
}
`;
// Only the known-secret loops read the order; the sorts that order discovered values are not part of it.
const ORIGINAL_SECRET_SORT = "s !== replacement))].sort((a, b) => b.length - a.length)";
const ORACLE_SECRET_SORT = "s !== replacement))].sort((a, b) => typeof a === \"string\" && typeof b === \"string\" ? referenceSecretOrder(a, b) : b.length - a.length)";
// The tail owner's key/value strings are read by the pinned regex in the oracle.
const TAIL_PAIRS_RUNTIME = "      for (const [key, field] of stringPairs(raw)) {\n        try { fields.push([JSON.parse(key), JSON.parse(field)]); } catch { /* malformed */ }\n      }";
const TAIL_PAIRS_PINNED = "      for (const match of raw.matchAll(/(\"(?:\\\\.|[^\"\\\\])*\"\\s*:\\s*)(\"(?:\\\\.|[^\"\\\\])*\")/g)) {\n        try { fields.push([JSON.parse(match[1].replace(/\\s*:\\s*$/, \"\")), JSON.parse(match[2])]); } catch { /* malformed */ }\n      }";
function extendOracleV4(input: string): string {
  // The oracle has no prepared-secret memo (a later hot-path change): the section's own known-secret loop
  // reads the ORIGINAL expression that prepareSecrets replaced, so the oracle stays free of that memo.
  const section = swapOnce(v4Section, "for (const secret of prepareSecrets(secrets)) {",
    "for (const secret of [...new Set(secrets.filter(s => s.length >= 8 && s !== replacement))].sort((a, b) => b.length - a.length)) {");
  let out = swapOnce(input, "export interface PrivacyBudget", ORACLE_SECRET_ORDER + section + "export interface PrivacyBudget");
  // ORIGINAL_TEXT is a verbatim copy that differs from the base text(); the oracle reads the v4 text() below instead.
  out = swapOnce(out, ORIGINAL_TEXT, V4_ORACLE_TEXT);
  for (const [label, current, base] of V4_SWAPS) if (label !== "text") out = swapOnce(out, base, current);
  for (const [, current, base] of CROSS_OWNER_RESTORE) {
    if (base) out = swapOnce(out, base, current);
    else out = swapOnce(out, "export interface PrivacyBudget", current + "export interface PrivacyBudget");
  }
  out = swapOnce(out, TAIL_PAIRS_RUNTIME, TAIL_PAIRS_PINNED);
  if (out.split(ORIGINAL_SECRET_SORT).length < 2) throw new Error("Privacy oracle source boundary drift: known-secret order");
  out = out.split(ORIGINAL_SECRET_SORT).join(ORACLE_SECRET_SORT);
  // The oracle reads every value shape as Python does; only its jwt shape stays the plain, unbounded regex.
  const pyDefinitions = source.slice(source.indexOf("// Python 3 re, as the reference reads"), source.indexOf("// The jwt shape is scanned by replaceJwt"));
  out = swapOnce(out, "const valueShapes = catalog.credentials.value_shapes.map(item => new RegExp(item.regex, item.flags + \"g\"));",
    pyDefinitions + "const valueShapes = catalog.credentials.value_shapes.map(item => {\n  const known = pyShape(item);\n"
    + "  if (known) return new RegExp(known.lead + known.body, \"gu\");\n"
    + "  return new RegExp(item.id === \"jwt\" ? item.regex.replace(/^\\\\b/, PY_BEFORE) : item.regex, item.flags + (item.id === \"jwt\" ? \"gu\" : \"g\"));\n});");
  return out;
}
const baseRuntimeSource = restoreV4(source);
if (digest(baseRuntimeSource) !== BASE_V4_RUNTIME_SHA256) throw new Error("Privacy oracle no longer restores the pre-v4 runtime");
let releasedSource = replaceSection(baseRuntimeSource, "    video: {", "    audio: {", "");
releasedSource = replaceSection(releasedSource, "    image: {", "    audio: {", "");
// The pinned section also replaces the new video branch inside hint().
releasedSource = replaceSection(releasedSource, "// The third bit is a complete, non-media MIME discriminator.", "const zeroDetailDeferral:", RELEASED_IMAGE_HINTS);
releasedSource = replaceSection(releasedSource, "    if (declaredImagePayload(path, hints)", "    if (magic(value))", "");
// Linear-time prose-assignment scan (nextAssignment): restore the single-regex scan the pins describe.
function restoreAssignmentScan(input: string): string {
  let out = replaceSection(input, "/**\n * Next `name", "/** Text assignments use the same catalog predicate", "");
  const swaps: [string, string][] = [
    ["  for (let match: ReturnType<typeof nextAssignment>, next = 0; (match = nextAssignment(value, next));) {\n    next = match.lastIndex;", "  for (let match: RegExpExecArray | null; (match = assignment.exec(value));) {"],
    ["const name = match.name;", "const name = match[1];"],
    ["const start = match.lastIndex;", "const start = assignment.lastIndex;"],
    ["    next = end;\n", "    assignment.lastIndex = end;\n"],
    ["function scrubAssignments(value: string, owners: readonly TextOwner[] = []): string {\n", "function scrubAssignments(value: string, owners: readonly TextOwner[] = []): string {\n  const assignment = /\\b([A-Za-z_][A-Za-z0-9_.-]*)([\"']?)\\s*[:=]\\s*/g;\n"],
  ];
  for (const [from, to] of swaps) {
    if (input.split(from).length !== 2) throw new Error("Privacy oracle source boundary drift");
    out = out.replace(from, () => to);
  }
  return out;
}
releasedSource = restoreAssignmentScan(releasedSource);
// Linear-time jwt scan (replaceJwt): restore the plain per-shape regex loop the pins describe.
function restoreJwtScan(input: string): string {
  let out = replaceSection(input, "const isRunCode =", "function bareMedia(", "");
  out = replaceSection(out, "const JWT_SHAPE =", "const valueShapes", "");
  const swaps: [string, string][] = [
    ["const valueShapes: (RegExp | ((value: string) => string))[] = catalog.credentials.value_shapes.map(item =>\n  item.id === \"jwt\" && item.regex === JWT_SHAPE && item.flags === \"\" ? replaceJwt : new RegExp(item.regex, item.flags + \"g\"));", "const valueShapes = catalog.credentials.value_shapes.map(item => new RegExp(item.regex, item.flags + \"g\"));"],
    ["value = typeof shape === \"function\" ? shape(value) : value.replace(shape, replacement);", "value = value.replace(shape, replacement);"],
  ];
  for (const [from, to] of swaps) {
    if (out.split(from).length !== 2) throw new Error("Privacy oracle source boundary drift");
    out = out.replace(from, () => to);
  }
  return out;
}
releasedSource = restoreJwtScan(releasedSource);
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

// The pins above are intact. Now extend both oracles with the v4 rules (never strip them out).
const extendedOracleSource = extendOracleV4(oracleSource);
const extendedParentSource = extendOracleV4(parentSource);

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
const oracle = loadRuntime(extendedOracleSource);
const candidate = loadRuntime(source, true);
const parent = loadRuntime(extendedParentSource);

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
/** The seeded scrubAttribute cases whose output differs from the pinned implementation's, with the runtime's output. */
const ATTRIBUTE_RAW_READ: Record<number, string> = Object.fromEntries([
  ...[4255, 9799, 12871, 12991, 16903, 17455].map(i => [i, "\"{\\\"message\\\":\\\"<redacted>\""] as const),
  // A quoted root whose string holds no registered value: the read of its raw bytes with no registered value hides the rest of the text
  // after a pair whose name is separated from its `=` or `:` by an escape, as both References do.
  [4028, "\t\r\n\"F}P_<\u00a01a8I'a'd{\u2028\ufeff-&?GRp9z-kWjVpa[A\\u0000y>:2kx,t%T_k,\u03a3<W\\ud800TX'O\u2028J\u6f22#Jxc\ufeff\\n=B}3\u00e9\ud834\udd1endrm\\u0000\ud834\udd1e,P:kLFV\\ud800\u00a0=<redacted>\""],
  [13484, "\t\r\n\"rN\ud834\udd1e\u2028\u6f22\\r{\ufeffV\\r54-n#6r.\u0130AU!Sa\u2028n!\\udfff=b;Wm{\\\\kv\u00e9ag6\\udfff\u03a3W=\ufeff\u6f229%\\ud800Q<yh6\u00e9!\u20281B_\u00a0EvvDVt_t\ud834\udd1e[\u6f22\\\\urc;A\\ud8000B#Tm\u6f22\u03a3\\\\/\ufeff?\\nj\\ud800\u0130\\udfff\ufeff\u2028[\\ns?\u6f2254;\u03a3h':<redacted>\""],
  [18596, " \"!K37\u2028L\ud834\udd1eP\u03a3is4\\\"8_\u03a3u\ufeffW\\\\\u03a36\\\\Q\\ud800\u6f22>gL_JGzjp]\u20282\\t;\u2028Q\ufeffr\ufeff\u2028l#\u0130eF\ud800\udfffs\u00df=o\u00a0i\u00dfjW\u0130mM&\u03a3R\u00e9f\u00e9\\ud800lCG{p;hHE&\u03a3\u0130\\t}\u03a3\ud834\udd1e&\u00a0\ufeff\\udfff\\ud800b\\u0000#\u00df.:b\u00dfFI\u00e9\u00dfKS\\u0000\\udfff7\u00a0:<redacted>\""],
]);
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
          // A quoted root whose string holds a registered value is also joined with the read of its raw bytes with no registered value: in
          // these cases that read hides the rest of the text after a pair (` :` in the message), as both References do. A quoted root with no
          // registered value in its string is joined with that read too.
          if (ATTRIBUTE_RAW_READ[i] !== undefined) expect({ caseId, result: outcome(() => runtime.scrubAttribute(text, secrets, !!(i % 2))) }).toEqual({ caseId, result: { kind: "return", value: ATTRIBUTE_RAW_READ[i] } });
          else equal(() => runtime.scrubAttribute(text, secrets, !!(i % 2)), () => oracle.scrubAttribute(text, secrets, !!(i % 2)), caseId);
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
const secretOrder = (a: string, b: string): number => {
  const p = Array.from(a, c => c.codePointAt(0)!), q = Array.from(b, c => c.codePointAt(0)!);
  if (p.length !== q.length) return q.length - p.length;
  for (let i = 0; i < p.length; i++) if (p[i] !== q[i]) return p[i] - q[i];
  return 0;
};
const originalPreparation = (secrets: readonly string[]) => [...new Set(secrets.filter(s => s.length >= 8 && s !== catalog.credentials.replacement))]
  .sort((a, b) => typeof a === "string" && typeof b === "string" ? secretOrder(a, b) : b.length - a.length);

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
    // Equal lengths are ordered by code point, whatever the order given.
    const tied = ["abcdefgh", "bcdefghi"];
    expect(api.prepareSecrets!(tied)).toEqual(["abcdefgh", "bcdefghi"]);
    tied.reverse(); expect(api.prepareSecrets!(tied)).toEqual(["abcdefgh", "bcdefghi"]);
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
        observed: expectedStatics, reads: 8,
        // The scans before the eighth read are given back (see scanned), as the reference charges a string one node: only the one node of the value is held.
        nodes: catalog.limits.max_nodes - 1,
        // The reference charges a text once, where it is read as a value (the one line of the budget it takes for a string).
        text: MAX_TEXT - value.length,
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
    // A path of more than 32 components exports the placeholder before the depth budget is read, so the depth budget is
    // exhausted here by the starting depth, which no path carries.
    const depth = kind === "depth" ? 33 : 0;
    equal(() => runtime.scrubValue(value, [], "", depth), () => oracle.scrubValue(value, [], "", depth), "public-budget/" + kind);
    expect(() => runtime.scrubValue(value, [], "", depth)).toThrow(/Privacy (structure|text) budget/);
  });
  test("a nested value whose path is over the name budget exports the placeholder, not a budget error", () => {
    let value: any = "ordinary";
    for (let i = 0; i < 34; i++) value = [value];
    equal(() => runtime.scrubValue(value), () => oracle.scrubValue(value), "public-budget/path-before-depth");
    expect(runtime.scrubValue(value)).toBe("<redacted>");
  });
});
