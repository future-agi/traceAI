import {
  CATEGORIES,
  LIMITS,
  STRUCTURE_POLICY,
  validateFact,
  type CanonicalFact,
  type Policy,
  type Usage,
} from "./schema.js";
/** Credential aliases share one policy at object, JSON and free-text boundaries. */
export function isSensitiveField(key: string): boolean {
  const normalized = key
    .replace(/\\u([a-f0-9]{4})/gi, (_match, hex: string) =>
      String.fromCharCode(parseInt(hex, 16)),
    )
    .replace(/[^a-z0-9]/gi, "");
  if (
    /^(?:inputtokens|outputtokens|totaltokens|tokencount|tokenizer|publickey|nonsecretkey)$/i.test(
      normalized,
    )
  )
    return false;
  return /secret|token|password|passwd|apikey|privatekey|accesskey|credential|authorization|cookie|signingkey|passphrase|^pwd$/i.test(
    normalized,
  );
}
export const isSensitiveKey = isSensitiveField;
export const redactValue = (value: unknown, depth = 0): unknown =>
  redactStructured(value, depth);
/** Defense in depth for content-on-by-default. Structure-only omits content entirely. */
export function redact(text: string): string {
  return redactStructured(text) as string;
}
/** Check normalized keys before serialization, including nested JSON-encoded strings. */
export function redactStructured(value: unknown, depth = 0): unknown {
  if (depth > 16) return "[REDACTED_DEPTH_LIMIT]";
  if (Array.isArray(value))
    return value.map((item) => redactStructured(item, depth + 1));
  if (value !== null && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        isSensitiveField(key)
          ? "[REDACTED]"
          : redactStructured(item, depth + 1),
      ]),
    );
  if (typeof value === "string") {
    // Inspect complete strings before any display truncation. Refuse oversized
    // content rather than treating a truncated JSON prefix as ordinary text.
    if (
      value.length > LIMITS.ingress ||
      Buffer.byteLength(value) > LIMITS.ingress
    )
      return "[REDACTED_SIZE_LIMIT]";
    if (/^\s*[\[{"]/.test(value)) {
      try {
        return JSON.stringify(redactStructured(JSON.parse(value), depth + 1));
      } catch {
        /* Opaque text still receives the free-text policy. */
      }
    }
    return redactText(value, depth);
  }
  return value;
}
/** Redact complete quoted values at their JSON escape depth. An escaped quote
 * inside a secret cannot be mistaken for the enclosing string delimiter. */
/** End offset of a YAML block scalar whose indicator starts at `valueStart`. */
function blockScalarEnd(
  text: string,
  keyAt: number,
  valueStart: number,
  block: RegExpExecArray,
): number {
  const escaped = block[1].startsWith("\\"),
    sep = escaped ? "\\n" : "\n";
  const previous = text.lastIndexOf(sep, keyAt);
  const lineStart = previous < 0 ? 0 : previous + sep.length;
  const indent = (line: string) => /^[ \t]*/.exec(line)![0].length;
  const keyIndent = indent(text.slice(lineStart, keyAt + 1));
  let end = valueStart + block[0].length - block[1].length,
    position = valueStart + block[0].length;
  while (position <= text.length) {
    const next = text.indexOf(sep, position),
      lineEnd = next < 0 ? text.length : next;
    const line = text
      .slice(position, lineEnd)
      .replace(escaped ? /\\r$/ : /\r$/, "");
    if (line.trim()) {
      if (indent(line) <= keyIndent) break;
      // Leave the complete raw/escaped line separator outside the scalar.
      end = position + line.length;
    }
    if (next < 0) break;
    position = next + sep.length;
  }
  return end;
}
function redactAssignments(text: string): string {
  // Backslashes belong to quote delimiters or complete Unicode escapes, not
  // an overlapping greedy key class that can backtrack over an entire payload.
  const assignment =
    /(^|[^a-z0-9_.\\-])((?:\\*["'])?((?:[a-z]|\\u[a-f0-9]{4})(?:[a-z0-9_.-]|\\u[a-f0-9]{4})*)(?:\\*["'])?\s*[:=]\s*)(\\*["'])?/gi;
  let result = "",
    offset = 0,
    match: RegExpExecArray | null;
  while ((match = assignment.exec(text))) {
    if (!isSensitiveField(match[3])) {
      // Give back whitespace after the separator so the next key keeps its
      // prefix (e.g. `service:\n  password: x`).
      assignment.lastIndex =
        match.index + match[1].length + match[2].trimEnd().length;
      continue;
    }
    let end = text.length;
    const delimiter = match[4];
    if (delimiter) {
      const quote = delimiter.slice(-1),
        depth = delimiter.length - 1;
      let slashes = 0;
      for (let i = assignment.lastIndex; i < text.length; i++) {
        const char = text[i];
        if (char === quote && slashes % (2 * (depth + 1)) === depth) {
          end = i + 1;
          break;
        }
        slashes = char === "\\" ? slashes + 1 : 0;
      }
    } else {
      const remainder = text.slice(assignment.lastIndex);
      const auth = /^(?:Basic|Bearer|Digest|Token)\s+[^\s,;"']+/i.exec(
        remainder,
      );
      // YAML block scalar (`key: |`, `key: >-`, ...): the secret is on the following
      // more-indented lines, raw or inside JSON-escaped text (`\n`).
      const block =
        /^[|>][0-9+-]{0,3}[ \t]*(?:#[^\r\n\\]*)?(\r?\n|(?:\\+r)?\\+n)/.exec(remainder);
      if (block) end = blockScalarEnd(text, match.index, assignment.lastIndex, block);
      else if (/^[{\[]/.test(remainder)) {
        // Unparseable structured credential values are ambiguous: omit the tail.
        end = text.length;
      } else if (/:\s*$/.test(match[2])) {
        // YAML plain scalars include the entire first line, even at EOF or
        // without a folded continuation. Shell '=' assignments remain separate.
        const firstLine = /^[^\r\n]*?(\r?\n)/.exec(remainder);
        end = firstLine
          ? blockScalarEnd(text, match.index, assignment.lastIndex, firstLine)
          : text.length;
      } else if (auth) end = assignment.lastIndex + auth[0].length;
      else {
        end =
          assignment.lastIndex +
          (/^[^\s,;"'}\]]*/.exec(remainder)?.[0].length ?? 0);
      }
    }
    result +=
      text.slice(offset, match.index) +
      match[1] +
      (delimiter ? match[2] + '"[REDACTED]"' : "[REDACTED]");
    offset = end;
    assignment.lastIndex = end;
  }
  return result + text.slice(offset);
}
function redactEmbeddedJson(text: string, depth: number): string {
  if (text.length > LIMITS.ingress || Buffer.byteLength(text) > LIMITS.ingress)
    return "[REDACTED_SIZE_LIMIT]";
  let start = -1,
    offset = 0,
    quoted = false,
    escaped = false,
    result = "";
  const stack: string[] = [];
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (start < 0) {
      if (char !== "{" && char !== "[") continue;
      start = i;
    }
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
      continue;
    }
    if (char === '"') {
      quoted = true;
      continue;
    }
    if (char === "{" || char === "[") {
      stack.push(char);
      if (stack.length > 32) return "[REDACTED_DEPTH_LIMIT]";
    } else if (char === "}" || char === "]") {
      if (stack.pop() !== (char === "}" ? "{" : "[")) {
        stack.length = 0;
        start = -1;
        continue;
      }
      if (!stack.length) {
        try {
          const parsed = JSON.parse(text.slice(start, i + 1));
          const clean = redactStructured(parsed, depth + 1);
          const sanitized = JSON.stringify(clean);
          // Do not rewrite harmless JSON-looking code or change prompt correlation.
          result += text.slice(offset, start) + (sanitized === JSON.stringify(parsed)
            ? text.slice(start, i + 1) : sanitized);
          offset = i + 1;
        } catch {
          // Code and other non-JSON fragments still receive free-text redaction.
        }
        start = -1;
      }
    }
  }
  return result + text.slice(offset);
}
function redactText(text: string, depth = 0): string {
  return redactAssignments(redactEmbeddedJson(text, depth))
    // URI userinfo is credential-bearing for every scheme, including local databases.
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s/"'<>]*@/gi, (uri) => uri.slice(0, uri.indexOf("://") + 3) + "[REDACTED]@")
    .replace(/\b(?:bearer|basic)\s+[^\s,;"']+/gi, "[REDACTED]")
    .replace(/\b(?:sk-|ghp_|github_pat_)[a-zA-Z0-9_-]{8,}/g, "[REDACTED]")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[REDACTED_EMAIL]")
    .replace(
      /(?:https?:\/\/|\/(?:Users|home|tmp)\/)[^\s"'<>]+/g,
      "[REDACTED_LOCATION]",
    );
}
function usage(u: Usage): Usage {
  return {
    scope: u.scope,
    contribution: u.contribution,
    planId: u.planId,
    factIds: [...u.factIds],
    inclusions: u.inclusions,
    complete: u.complete,
    ...(u.contribution === "none"
      ? {}
      : Object.fromEntries(
          [
            "input",
            "output",
            "cacheRead",
            "cacheWrite",
            "reasoning",
            "total",
            "costUsd",
          ]
            .filter((k) => (u as any)[k] !== undefined)
            .map((k) => [k, (u as any)[k]]),
        )),
  };
}
/** Rebuild, never spread source objects. Unknown fields, names, events, URLs and status text disappear. */
export function minimize(
  f: CanonicalFact,
  policy: Policy = STRUCTURE_POLICY,
): CanonicalFact {
  validateFact(f);
  const result: CanonicalFact = {
    schemaVersion: 1,
    sourceWorkId: f.sourceWorkId,
    identityProfile: f.identityProfile,
    sessionId: f.sessionId,
    turnId: f.turnId,
    traceId: f.traceId,
    spanId: f.spanId,
    agent: f.agent,
    surface: f.surface,
    source: f.source,
    sourceSchema: f.sourceSchema,
    reconstructionVersion: f.reconstructionVersion,
    policyRevision: policy.revision,
    unit: f.unit,
    timing: f.timing,
    startNs: f.startNs,
    endNs: f.endNs,
    outcome: f.outcome,
    inputCompleteness: f.inputCompleteness,
    outputCompleteness: f.outputCompleteness,
  };
  for (const key of [
    "parentSpanId",
    "agentVersion",
    "model",
    "provider",
    "tool",
    "causedBy",
  ] as const)
    if (f[key] !== undefined) result[key] = f[key];
  if (f.incomplete === true) result.incomplete = true;
  if (f.lateCapture === true) result.lateCapture = true;
  if (f.links)
    result.links = f.links.map((l) => ({
      traceId: l.traceId,
      spanId: l.spanId,
    }));
  if (f.usage) result.usage = usage(f.usage);
  for (const category of CATEGORIES) {
    if (
      !policy.categories.includes(category) ||
      !policy.ceiling.includes(category)
    )
      continue;
    const text = f.content?.[category];
    if (typeof text !== "string") continue;
    // Structural redaction has its own processing bound; truncate only sanitized
    // content, without splitting UTF-8 code points.
    const clean = Buffer.from(redact(text));
    const bytes = clean.subarray(0, LIMITS.content);
    (result.content ??= {})[category] = bytes
      .toString("utf8")
      .replace(/�$/, "");
  }
  if (Buffer.byteLength(JSON.stringify(result)) > LIMITS.span)
    throw new Error("OVERSIZE_FACT");
  return result;
}
