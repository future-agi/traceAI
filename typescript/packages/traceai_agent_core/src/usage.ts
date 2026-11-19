import { createHash } from "node:crypto";
import { canonical } from "./identity.js";
export type UsagePlan = "request" | "attempt" | "turn" | "unknown";
export function planId(turnWorkId: string): string {
  if (!/^[0-9a-f]{64}$/.test(turnWorkId)) throw new Error("INVALID_WORK_ID");
  // Plan choice isn't part of identity; competing choices must conflict, not mint new plans.
  return createHash("sha256")
    .update(canonical(["fi-usage-plan-v1", turnWorkId]))
    .digest("hex");
}
export interface Counters {
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  reasoning?: number;
}
export function normalizeUsage(
  raw: Counters,
  rules: { cacheInInput: boolean; reasoningInOutput: boolean },
): Counters & { total?: number } {
  for (const v of Object.values(raw))
    if (!Number.isSafeInteger(v) || v < 0) throw new Error("UNRESOLVED_USAGE");
  const input =
    raw.input === undefined
      ? undefined
      : raw.input +
        (rules.cacheInInput ? 0 : (raw.cacheRead ?? 0) + (raw.cacheWrite ?? 0));
  const output =
    raw.output === undefined
      ? undefined
      : raw.output + (rules.reasoningInOutput ? 0 : (raw.reasoning ?? 0));
  const total =
    input === undefined || output === undefined ? undefined : input + output;
  if (
    [input, output, total].some(
      (v) => v !== undefined && !Number.isSafeInteger(v),
    )
  )
    throw new Error("UNRESOLVED_USAGE");
  return { ...raw, input, output, total };
}
export function cumulativeDelta(
  previous: number | undefined,
  next: number,
  continuous: boolean,
): number {
  if (
    !continuous ||
    previous === undefined ||
    !Number.isSafeInteger(previous) ||
    !Number.isSafeInteger(next) ||
    previous < 0 ||
    next < previous
  )
    throw new Error("UNRESOLVED_USAGE");
  return next - previous;
}
