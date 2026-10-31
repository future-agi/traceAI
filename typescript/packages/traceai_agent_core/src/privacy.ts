import {
  CATEGORIES,
  LIMITS,
  STRUCTURE_POLICY,
  validateFact,
  type CanonicalFact,
  type Policy,
  type Usage,
} from "./schema.js";
/** Defense in depth for opted-in text only; structure-only is the strong default. */
export function redact(text: string): string {
  return text
    .replace(
      /(?:bearer\s+|(?:api[_-]?key|secret|password|token)\s*[:=]\s*)[^\s,;"']+/gi,
      "[REDACTED]",
    )
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
  if (f.native) {
    result.native = {
      kind: f.native.kind,
      statusCode: f.native.statusCode,
      operation: f.native.operation,
    };
    if (f.native.summary) {
      result.native.summary = {};
      for (const key of [
        "input",
        "output",
        "cacheRead",
        "cacheWrite",
        "reasoning",
      ] as const)
        if (f.native.summary[key] !== undefined)
          result.native.summary[key] = f.native.summary[key];
    }
    if (f.native.billing) {
      result.native.billing = {};
      for (const key of ["multiplier", "nanoAiu"] as const)
        if (f.native.billing[key] !== undefined)
          result.native.billing[key] = f.native.billing[key];
    }
  }
  for (const category of CATEGORIES) {
    if (
      !policy.categories.includes(category) ||
      !policy.ceiling.includes(category)
    )
      continue;
    const text = f.content?.[category];
    if (typeof text !== "string") continue;
    // Refuse excess before redaction work, and don't split UTF-8 code points.
    const bounded = text.slice(0, LIMITS.content);
    const clean = Buffer.from(redact(bounded));
    const bytes = clean.subarray(0, LIMITS.content);
    (result.content ??= {})[category] = bytes
      .toString("utf8")
      .replace(/�$/, "");
  }
  if (Buffer.byteLength(JSON.stringify(result)) > LIMITS.span)
    throw new Error("OVERSIZE_FACT");
  return result;
}
