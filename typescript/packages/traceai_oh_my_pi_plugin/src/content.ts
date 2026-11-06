import { isSensitiveKey, redact, type UsageInput } from "@traceai/agent-core";
export { messageText } from "@traceai/pi-coding-agent/content";

/** Provider payloads are untrusted content, not configuration. Never serialize credentials,
 * image bytes, signatures or transport headers. The shared core redacts and applies policy. */
export function boundedJson(value: unknown): string | undefined {
  try {
    let budget = 20000;
    const visit = (v: unknown, depth = 0): unknown => {
      if (budget <= 0 || depth > 12) return "[truncated]";
      if (typeof v === "string") {
        const s = redact(v).slice(0, budget);
        budget -= s.length;
        return s;
      }
      if (v === null || typeof v === "boolean" || typeof v === "number")
        return v;
      if (Array.isArray(v))
        return v.slice(0, 128).map((x) => visit(x, depth + 1));
      if (v && typeof v === "object") {
        const o = v as Record<string, unknown>;
        if (
          ["image", "image_url", "input_image", "base64"].includes(
            String(o.type),
          )
        )
          return "[image]";
        return Object.fromEntries(
          Object.entries(o)
            .slice(0, 128)
            .filter(
              ([k]) =>
                !isSensitiveKey(k) &&
                !/^(headers|data|signature|(?:thinking|thought)[_-]?signature)$/i.test(
                  k,
                ),
            )
            .map(([k, x]) => [k.slice(0, 128), visit(x, depth + 1)]),
        );
      }
      return undefined;
    };
    return JSON.stringify(visit(value))?.slice(0, 20000);
  } catch {
    return undefined;
  }
}

/** OMP reports uncached input separately and prices from its model table, not an invoice. */
export function ompUsage(u: any): UsageInput | undefined {
  if (!u || typeof u !== "object") return undefined;
  const result: UsageInput = {
    cacheInInput: false,
    reasoningInOutput: true,
    costSource: "host-price-table",
  };
  let found = false;
  for (const k of ["input", "output", "cacheRead", "cacheWrite"] as const) {
    if (typeof u[k] === "number" && Number.isFinite(u[k]) && u[k] >= 0) {
      result[k] = u[k];
      found = true;
    }
  }
  if (
    typeof u.reasoningTokens === "number" &&
    Number.isFinite(u.reasoningTokens) &&
    u.reasoningTokens >= 0
  ) {
    result.reasoning = u.reasoningTokens;
    found = true;
  }
  if (
    typeof u.cost?.total === "number" &&
    Number.isFinite(u.cost.total) &&
    u.cost.total > 0
  ) {
    result.costUsd = u.cost.total;
    found = true;
  }
  return found ? result : undefined;
}
