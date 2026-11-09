/**
 * PII Redaction — lightweight scanning for common PII patterns.
 *
 * Replaces detected PII with <ENTITY_TYPE> tokens (e.g., <EMAIL_ADDRESS>).
 * Zero external dependencies.
 */

// ---------------------------------------------------------------------------
// Conservative admission, not a second copy of the redaction grammars. Every
// SSN/card/IP/phone match contains an ASCII digit; every key has this fixed prefix.
// Email's at-sign is checked separately below. False positives only trigger the
// retained scans; fixed-size alternatives keep this admission pass linear.
// ---------------------------------------------------------------------------
const QUICK_CHECK = /[0-9]|(?:sk|pk)[-_](?:live|test|prod)[-_]/;

function isAsciiLetter(code: number): boolean {
  return (code >= 65 && code <= 90) || (code >= 97 && code <= 122);
}

function isAsciiWord(code: number): boolean {
  return isAsciiLetter(code) || (code >= 48 && code <= 57) || code === 95;
}

function isEmailLocal(code: number): boolean {
  return isAsciiWord(code) || code === 46 || code === 37 || code === 43 || code === 45;
}

function isEmailDomain(code: number): boolean {
  return isAsciiLetter(code) || (code >= 48 && code <= 57) || code === 46 || code === 45;
}

/**
 * Match the legacy email grammar without retrying an unbounded regex at each
 * word boundary. At-signs delimit both local and domain character runs: each
 * code unit participates in at most one backward local scan and one forward
 * domain scan. Boundary checks are constant work, and output slices are disjoint.
 */
function redactEmailAddresses(text: string): string {
  const parts: string[] = [];
  let copiedUntil = 0;
  let searchFrom = 0;
  for (;;) {
    const at = text.indexOf("@", searchFrom);
    if (at === -1) break;
    searchFrom = at + 1;

    // Earliest original ASCII word boundary in this local run. The preceding
    // code unit may belong to a prior match; do not use the replacement token's
    // boundary or allow a new match to consume any of the prior match itself.
    let start = -1;
    for (let pos = at - 1; pos >= copiedUntil; pos--) {
      const code = text.charCodeAt(pos);
      if (!isEmailLocal(code)) break;
      if (isAsciiWord(code) !== isAsciiWord(text.charCodeAt(pos - 1))) {
        start = pos;
      }
    }
    if (start === -1) continue;

    // The greedy domain chooses the rightmost dot admitting a two-or-more ASCII
    // letter suffix and a word boundary. Remember every acceptable end while
    // traversing the domain once, including an earlier end if a later TLD fails.
    let end = -1;
    let dot = -1;
    let letters = 0;
    for (let pos = at + 1; pos < text.length; pos++) {
      const code = text.charCodeAt(pos);
      if (!isEmailDomain(code)) break;
      if (code === 46) {
        dot = pos;
        letters = 0;
      } else if (isAsciiLetter(code) && dot > at + 1) {
        letters++;
        if (letters >= 2 && !isAsciiWord(text.charCodeAt(pos + 1))) {
          end = pos + 1;
        }
      } else {
        dot = -1;
        letters = 0;
      }
    }
    if (end === -1) continue;

    parts.push(text.slice(copiedUntil, start), "<EMAIL_ADDRESS>");
    copiedUntil = end;
    // A scanned but unmatched domain suffix can be the next email's local part.
    searchFrom = end;
  }
  if (copiedUntil === 0) return text;
  parts.push(text.slice(copiedUntil));
  return parts.join("");
}

// ---------------------------------------------------------------------------
// Remaining PII patterns — email is handled first, then this order is preserved.
// ---------------------------------------------------------------------------
const PII_PATTERNS: Array<{ regex: RegExp; replacement: string }> = [
  {
    regex: /\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b/g,
    replacement: "<SSN>",
  },
  {
    regex: /\b(?:\d[ \-]*?){13,19}\b/g,
    replacement: "<CREDIT_CARD>",
  },
  {
    regex: /\b(?:sk|pk)[-_](?:live|test|prod)[-_][A-Za-z0-9]{20,}\b/g,
    replacement: "<API_KEY>",
  },
  {
    regex:
      /\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b/g,
    replacement: "<IP_ADDRESS>",
  },
  {
    regex: /(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b/g,
    replacement: "<PHONE_NUMBER>",
  },
];

/**
 * Scan `text` for PII patterns and replace each match with its entity token.
 */
export function redactPiiInString(text: string): string {
  if (!text || (!text.includes("@") && !QUICK_CHECK.test(text))) {
    return text;
  }
  let result = redactEmailAddresses(text);
  for (const { regex, replacement } of PII_PATTERNS) {
    regex.lastIndex = 0;
    result = result.replace(regex, replacement);
  }
  return result;
}

/**
 * Apply PII redaction to `value`.
 *
 * Handles:
 * - `string` — scanned directly.
 * - `string[]` — each element scanned.
 * - Anything else — returned as-is.
 */
export function redactPiiInValue<T>(value: T): T {
  if (typeof value === "string") {
    return redactPiiInString(value) as T;
  }
  if (Array.isArray(value)) {
    return value.map((item) =>
      typeof item === "string" ? redactPiiInString(item) : item,
    ) as T;
  }
  return value;
}
