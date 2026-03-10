/**
 * PII Redaction — lightweight regex-based scanning for common PII patterns.
 *
 * Replaces detected PII with <ENTITY_TYPE> tokens (e.g., <EMAIL_ADDRESS>).
 * Zero external dependencies.
 */

// ---------------------------------------------------------------------------
// Quick-check: a single regex that matches if the string *might* contain PII.
// If it doesn't match, we skip all 6 pattern scans entirely.
// ---------------------------------------------------------------------------
const QUICK_CHECK =
  /[A-Za-z0-9._%+\-]+@|\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b|\b(?:\d[ \-]*?){13,19}\b|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|(?:sk|pk)[-_](?:live|test|prod)[-_]|\(?\+?\d{1,4}\)?[\s\-.]\(?\d/;

// ---------------------------------------------------------------------------
// Individual PII patterns — order matters (more specific first).
// ---------------------------------------------------------------------------
const PII_PATTERNS: Array<{ regex: RegExp; replacement: string }> = [
  {
    regex: /\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b/g,
    replacement: "<EMAIL_ADDRESS>",
  },
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
  if (!text || !QUICK_CHECK.test(text)) {
    return text;
  }
  let result = text;
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
