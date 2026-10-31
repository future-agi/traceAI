import {
  minimize,
  opaqueId,
  sessionId,
  usageId,
  planId,
  validNativeId,
  type CanonicalFact,
  type WorkKind,
} from "@traceai/agent-core";
import type { Capability, NormalizeContext } from "../../adapters.js";
import { AgentError } from "../../security/files.js";

export const CLAUDE_SCHEMA = "claude-code-otel-2.1.316";
export const CLAUDE_IDENTITY = "claude-code-native-v1";
const models = [
  "claude-opus-5-5",
  "claude-opus-5",
  "claude-sonnet-5-5",
  "claude-sonnet-5",
  "claude-haiku-5-5",
  "claude-opus-4-8",
  "claude-opus-4-7",
  "claude-opus-4-6",
  "claude-sonnet-4-6",
  "claude-haiku-4-5",
];
const tools = [
  "Read",
  "Write",
  "Edit",
  "Bash",
  "Glob",
  "Grep",
  "Agent",
  "Task",
  "Skill",
  "WebFetch",
  "WebSearch",
  "NotebookEdit",
  "mcp_other",
];
export const claudeCapability: Capability = {
  contract: 1,
  agent: "claude-code",
  surface: "cli",
  versions: ["2.1.316"],
  schemas: [CLAUDE_SCHEMA],
  identityProfiles: [CLAUDE_IDENTITY],
  models,
  providers: [],
  tools,
  platforms: ["darwin"],
  transcript: false,
  native: true,
  neutralResponse: "",
};

/** Only scalar fields are inspected; source objects, dynamic names and content are never copied. */
export function attributes(value: unknown): Record<string, unknown> {
  if (!Array.isArray(value) || value.length > 128)
    throw new AgentError("INVALID_NATIVE_ATTRIBUTES", 2);
  const result: Record<string, unknown> = Object.create(null);
  for (const item of value) {
    if (
      !item ||
      typeof item.key !== "string" ||
      Object.hasOwn(result, item.key)
    )
      throw new AgentError("INVALID_NATIVE_ATTRIBUTES", 2);
    const v = item.value;
    if (!v || typeof v !== "object") continue;
    if (typeof v.stringValue === "string") result[item.key] = v.stringValue;
    else if (typeof v.boolValue === "boolean") result[item.key] = v.boolValue;
    else if (v.intValue !== undefined) result[item.key] = v.intValue;
    else if (v.doubleValue !== undefined) result[item.key] = v.doubleValue;
  }
  return result;
}
export function nativeId(value: unknown, bytes: number): string {
  if (typeof value !== "string") throw new AgentError("INVALID_NATIVE_ID", 2);
  const id =
    /^[a-fA-F0-9]+$/.test(value) && value.length === bytes * 2
      ? value.toLowerCase()
      : Buffer.from(value, "base64").toString("hex");
  if (!validNativeId(id, bytes)) throw new AgentError("INVALID_NATIVE_ID", 2);
  return id;
}
function counter(value: unknown): number | undefined {
  if (value === undefined) return undefined;
  if (
    typeof value !== "number" &&
    !(typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value))
  )
    throw new AgentError("INVALID_NATIVE_USAGE", 2);
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n < 0)
    throw new AgentError("INVALID_NATIVE_USAGE", 2);
  return n;
}
const units: Record<string, WorkKind> = {
  "claude_code.interaction": "turn",
  "claude_code.llm_request": "request",
  "claude_code.tool": "tool",
  "claude_code.tool.execution": "activity",
  "claude_code.tool.blocked_on_user": "activity",
};
/** Native trace/span IDs are the transport identity. No transcript/source-work alias is asserted. */
export function normalizeClaudeSpan(
  input: unknown,
  resource: unknown,
  scope: unknown,
  context: NormalizeContext,
): CanonicalFact {
  const r = attributes((resource as any)?.attributes);
  if (
    context.sourceSchema !== CLAUDE_SCHEMA ||
    r["service.name"] !== "claude-code" ||
    r["service.version"] !== "2.1.316" ||
    (scope as any)?.name !== "com.anthropic.claude_code.tracing" ||
    (scope as any)?.version !== "1.0.0"
  )
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  const span = input as any;
  if (!span || typeof span !== "object")
    throw new AgentError("INVALID_NATIVE_SPAN", 2);
  const a = attributes(span.attributes);
  const unit = units[span.name];
  // New span kinds cannot silently acquire model/accounting semantics.
  if (!unit) throw new AgentError("UNKNOWN_NATIVE_OPERATION", 5);
  const traceId = nativeId(span.traceId, 16),
    spanId = nativeId(span.spanId, 8);
  const session = a["session.id"];
  if (typeof session !== "string" || !/^[a-f0-9-]{36}$/i.test(session))
    throw new AgentError("HELD_NATIVE_SESSION", 3);
  if (
    a["gen_ai.conversation.id"] !== undefined &&
    a["gen_ai.conversation.id"] !== session
  )
    throw new AgentError("NATIVE_IDENTITY_CONFLICT", 3);
  const key = context.identityKey;
  const sourceWorkId = opaqueId(
    key,
    "claude-code.native-work-v1",
    traceId + ":" + spanId,
  );
  const turnId = opaqueId(key, "claude-code.native-trace-v1", traceId);
  const error =
    span.status?.code === 2 ||
    span.status?.code === "STATUS_CODE_ERROR" ||
    a.success === false;
  const cancelled = a.decision === "cancelled";
  const fact: CanonicalFact = {
    schemaVersion: 1,
    sourceWorkId,
    identityProfile: CLAUDE_IDENTITY,
    sessionId: sessionId(key, "claude-code", session),
    turnId,
    traceId,
    spanId,
    ...(span.parentSpanId
      ? { parentSpanId: nativeId(span.parentSpanId, 8) }
      : {}),
    agent: "claude-code",
    surface: "cli",
    agentVersion: "2.1.316",
    source: "native",
    sourceSchema: CLAUDE_SCHEMA,
    reconstructionVersion: "1",
    policyRevision: context.policyRevision,
    unit,
    timing: unit === "request" ? "observed_request" : "unknown",
    startNs: span.startTimeUnixNano,
    endNs: span.endTimeUnixNano,
    outcome: error
      ? "error"
      : cancelled
        ? "cancelled"
        : a.decision === "reject"
          ? "permission_denied"
          : // In this pinned profile interaction/tool wrappers export on completion,
            // without request/execution success flags. Child failures stay on their
            // own spans; absence of a wrapper flag is not a missing end event.
            a.success === true ||
              a.decision === "accept" ||
              span.status?.code === 1 ||
              span.status?.code === "STATUS_CODE_OK" ||
              unit === "turn" ||
              unit === "tool"
            ? "success"
            : "incomplete",
    inputCompleteness: "unavailable",
    outputCompleteness: "unavailable",
  };
  // Neither a display model nor gen_ai.system establishes the actual provider behind a proxy.
  if (
    unit === "request" &&
    models.includes(a["gen_ai.request.model"] as string)
  )
    fact.model = a["gen_ai.request.model"] as string;
  if (unit === "tool")
    fact.tool = tools.includes(a.tool_name_safe as string)
      ? (a.tool_name_safe as string)
      : "mcp_other";
  if (unit === "request") {
    const input = counter(a.input_tokens),
      output = counter(a.output_tokens);
    const cacheRead = counter(a.cache_read_tokens),
      cacheWrite = counter(a.cache_creation_tokens);
    // Anthropic native input excludes cache. Normalize only when every input component is present.
    const inclusiveInput =
      input !== undefined && cacheRead !== undefined && cacheWrite !== undefined
        ? input + cacheRead + cacheWrite
        : undefined;
    const counters = { input: inclusiveInput, output, cacheRead, cacheWrite };
    const observed = Object.entries(counters).filter(
      ([, value]) => value !== undefined,
    );
    if (observed.length)
      fact.usage = {
        scope: "request",
        contribution: "request",
        planId: planId(turnId),
        factIds: observed.map(([metric]) =>
          usageId(key, sourceWorkId, "request", metric, "native-counter-v1"),
        ),
        ...counters,
        total:
          inclusiveInput !== undefined && output !== undefined
            ? inclusiveInput + output
            : undefined,
        inclusions: "normalized",
        complete: inclusiveInput !== undefined && output !== undefined,
      };
  }
  // Links retain only valid native IDs. Event bodies/status messages/raw usage summaries are dropped.
  if (
    Array.isArray(span.links) &&
    span.links.length > 0 &&
    span.links.length <= 32
  )
    fact.links = span.links.map((l: any) => ({
      traceId: nativeId(l.traceId, 16),
      spanId: nativeId(l.spanId, 8),
    }));
  if (fact.outcome === "incomplete") fact.incomplete = true;
  return minimize(fact, {
    revision: context.policyRevision,
    categories: [],
    ceiling: [],
  });
}
