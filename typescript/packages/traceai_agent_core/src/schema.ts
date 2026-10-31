import { validNativeId, type WorkKind } from "./identity.js";
export const LIMITS = Object.freeze({
  ingress: 1_048_576,
  span: 262_144,
  content: 32_768,
  attributes: 128,
  events: 64,
  links: 32,
  batch: 128,
});
export type Category =
  | "prompts"
  | "responses"
  | "model-context"
  | "tool-input"
  | "tool-output"
  | "system"
  | "tool-definitions"
  | "reasoning"
  | "error-text"
  | "workspace-identity";
export const CATEGORIES: readonly Category[] = [
  "prompts",
  "responses",
  "model-context",
  "tool-input",
  "tool-output",
  "system",
  "tool-definitions",
  "reasoning",
  "error-text",
  "workspace-identity",
];
export type Contribution = "request" | "attempt" | "turn" | "none";
export type Timing =
  | "observed_request"
  | "hook_interval"
  | "message_interval"
  | "estimated"
  | "unknown";
export type Outcome =
  | "success"
  | "error"
  | "cancelled"
  | "interrupted"
  | "permission_denied"
  | "incomplete"
  | "unknown";
export interface Usage {
  scope: "request" | "turn" | "session";
  contribution: Contribution;
  planId: string;
  factIds: string[];
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  reasoning?: number;
  total?: number;
  costUsd?: number;
  inclusions: "inclusive" | "normalized";
  complete: boolean;
}
/** No arbitrary attribute map. All identifiers here are opaque; native OTLP IDs pass through. */
export interface CanonicalFact {
  schemaVersion: 1;
  sourceWorkId: string;
  identityProfile: string;
  sessionId: string;
  turnId: string;
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  agent: string;
  surface: string;
  agentVersion?: string;
  source: "native" | "hook" | "extension" | "transcript";
  sourceSchema: string;
  reconstructionVersion: string;
  policyRevision: string;
  unit: WorkKind;
  timing: Timing;
  startNs: string;
  endNs: string;
  outcome: Outcome;
  inputCompleteness: "full_reported" | "partial" | "unavailable";
  outputCompleteness: "full_reported" | "partial" | "unavailable";
  model?: string;
  provider?: string;
  tool?: string;
  causedBy?: string;
  incomplete?: boolean;
  lateCapture?: boolean;
  usage?: Usage;
  content?: Partial<Record<Category, string>>;
  links?: { traceId: string; spanId: string }[];
}
export interface Policy {
  revision: string;
  categories: readonly Category[];
  ceiling: readonly Category[];
}
export const STRUCTURE_POLICY: Policy = Object.freeze({
  revision: "1",
  categories: [],
  ceiling: [],
});
export function token(value: unknown, max = 128): value is string {
  return (
    typeof value === "string" &&
    value.length <= max &&
    /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(value)
  );
}
const hex = (s: unknown): s is string =>
  typeof s === "string" && /^[a-f0-9]{64}$/.test(s);
export function validateFact(f: CanonicalFact): void {
  const fail = (): never => {
    throw new Error("INVALID_FACT");
  };
  if (
    !f ||
    f.schemaVersion !== 1 ||
    !hex(f.sourceWorkId) ||
    !hex(f.sessionId) ||
    !hex(f.turnId)
  )
    fail();
  if (
    ![
      f.identityProfile,
      f.agent,
      f.surface,
      f.sourceSchema,
      f.reconstructionVersion,
      f.policyRevision,
    ].every((v) => token(v))
  )
    fail();
  if (
    !validNativeId(f.traceId, 16) ||
    !validNativeId(f.spanId, 8) ||
    (f.parentSpanId !== undefined && !validNativeId(f.parentSpanId, 8))
  )
    fail();
  if (
    ![f.startNs, f.endNs].every(
      (v) => typeof v === "string" && /^(0|[1-9][0-9]{0,19})$/.test(v),
    ) ||
    BigInt(f.endNs) < BigInt(f.startNs) ||
    BigInt(f.endNs) > 18446744073709551615n
  )
    fail();
  if (
    !["turn", "request", "attempt", "tool", "activity", "compaction"].includes(
      f.unit,
    ) ||
    !["native", "hook", "extension", "transcript"].includes(f.source) ||
    ![
      "observed_request",
      "hook_interval",
      "message_interval",
      "estimated",
      "unknown",
    ].includes(f.timing)
  )
    fail();
  if (
    ![
      "success",
      "error",
      "cancelled",
      "interrupted",
      "permission_denied",
      "incomplete",
      "unknown",
    ].includes(f.outcome)
  )
    fail();
  if (
    ![f.inputCompleteness, f.outputCompleteness].every((v) =>
      ["full_reported", "partial", "unavailable"].includes(v),
    )
  )
    fail();
  if (f.causedBy !== undefined && !hex(f.causedBy)) fail();
  for (const v of [f.model, f.provider, f.tool, f.agentVersion])
    if (v !== undefined && !token(v)) fail();
  if (
    f.links &&
    (!Array.isArray(f.links) ||
      f.links.length > LIMITS.links ||
      f.links.some(
        (l) => !validNativeId(l.traceId, 16) || !validNativeId(l.spanId, 8),
      ))
  )
    fail();
  if (f.usage) {
    const u = f.usage;
    if (
      !hex(u.planId) ||
      !Array.isArray(u.factIds) ||
      u.factIds.length > 16 ||
      !u.factIds.every(hex) ||
      new Set(u.factIds).size !== u.factIds.length
    )
      fail();
    if (
      !["request", "attempt", "turn", "none"].includes(u.contribution) ||
      !["request", "turn", "session"].includes(u.scope) ||
      !["inclusive", "normalized"].includes(u.inclusions) ||
      typeof u.complete !== "boolean"
    )
      fail();
    if (
      u.contribution !== "none" &&
      (u.contribution !== f.unit ||
        !u.factIds.length ||
        u.scope !== (u.contribution === "turn" ? "turn" : "request"))
    )
      fail();
    for (const v of [
      u.input,
      u.output,
      u.cacheRead,
      u.cacheWrite,
      u.reasoning,
      u.total,
    ])
      if (v !== undefined && (!Number.isSafeInteger(v) || v < 0)) fail();
    if (
      u.total !== undefined &&
      u.input !== undefined &&
      u.output !== undefined &&
      u.total !== u.input + u.output
    )
      fail();
    if (
      u.costUsd !== undefined &&
      (!Number.isFinite(u.costUsd) || u.costUsd < 0)
    )
      fail();
  }
}
