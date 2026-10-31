import protobuf from "protobufjs";
import * as A from "@traceai/fi-semantic-conventions";
import type { CanonicalFact } from "@traceai/agent-core";
import type { Binding } from "./endpoint.js";
// OTLP wire field numbers; intentionally only traces, never logs or metrics.
const root = protobuf.parse(`syntax = "proto3";
message AnyValue { oneof value { string string_value=1; bool bool_value=2; int64 int_value=3; double double_value=4; ArrayValue array_value=5; KeyValueList kvlist_value=6; bytes bytes_value=7; } }
message ArrayValue { repeated AnyValue values=1; } message KeyValueList { repeated KeyValue values=1; }
message KeyValue { string key=1; AnyValue value=2; }
message Resource { repeated KeyValue attributes=1; uint32 dropped_attributes_count=2; }
message Scope { string name=1; string version=2; }
message Status { string message=2; int32 code=3; }
message Event { fixed64 time_unix_nano=1; string name=2; repeated KeyValue attributes=3; }
message Link { bytes trace_id=1; bytes span_id=2; string trace_state=3; repeated KeyValue attributes=4; }
message Span { bytes trace_id=1; bytes span_id=2; string trace_state=3; bytes parent_span_id=4; string name=5; int32 kind=6; fixed64 start_time_unix_nano=7; fixed64 end_time_unix_nano=8; repeated KeyValue attributes=9; repeated Event events=11; repeated Link links=13; Status status=15; }
message ScopeSpans { Scope scope=1; repeated Span spans=2; string schema_url=3; }
message ResourceSpans { Resource resource=1; repeated ScopeSpans scope_spans=2; string schema_url=3; }
message ExportTraceServiceRequest { repeated ResourceSpans resource_spans=1; }
message PartialSuccess { int64 rejected_spans=1; string error_message=2; }
message ExportTraceServiceResponse { PartialSuccess partial_success=1; }
`).root;
const request = root.lookupType("ExportTraceServiceRequest");
const response = root.lookupType("ExportTraceServiceResponse");
export const decodeTraces = (bytes: Uint8Array): any =>
  request.toObject(request.decode(bytes), { longs: String, bytes: String });
export function encodeTraceObject(value: object): Buffer {
  return Buffer.from(request.encode(request.fromObject(value)).finish());
}
export type NativeIdEncoding = "hex" | "base64";
/** JSON uses hex IDs; protobufjs decodes bytes to base64. Never guess the codec. */
export function decodeNativeId(
  value: unknown,
  bytes: 8 | 16,
  encoding: NativeIdEncoding,
): string {
  if (typeof value !== "string") throw new Error("INVALID_NATIVE_ID");
  let hex: string;
  if (
    encoding === "hex" &&
    new RegExp(`^[a-fA-F0-9]{${bytes * 2}}$`).test(value)
  )
    hex = value.toLowerCase();
  else if (encoding === "base64" && value.length === Math.ceil(bytes / 3) * 4) {
    const decoded = Buffer.from(value, "base64");
    if (decoded.length !== bytes || decoded.toString("base64") !== value)
      throw new Error("INVALID_NATIVE_ID");
    hex = decoded.toString("hex");
  } else throw new Error("INVALID_NATIVE_ID");
  if (/^0+$/.test(hex)) throw new Error("INVALID_NATIVE_ID");
  return hex;
}
export function decodeRejections(bytes: Uint8Array): number {
  const value = response.toObject(response.decode(bytes), { longs: String });
  const count = Number(value.partialSuccess?.rejectedSpans ?? 0);
  if (!Number.isSafeInteger(count) || count < 0)
    throw new Error("INVALID_RESPONSE");
  return count;
}
export const encodeResponse = (rejected = 0): Buffer =>
  Buffer.from(
    response
      .encode(
        response.fromObject({ partialSuccess: { rejectedSpans: rejected } }),
      )
      .finish(),
  );
function attrs(
  values: Record<string, string | number | boolean | undefined>,
): object[] {
  return Object.entries(values)
    .filter(([, v]) => v !== undefined)
    .map(([key, value]) => ({
      key,
      value:
        typeof value === "boolean"
          ? { boolValue: value }
          : typeof value === "number"
            ? { doubleValue: value }
            : { stringValue: value },
    }));
}
export function spanObject(f: CanonicalFact): object {
  const kind = {
    turn: "AGENT",
    request: "LLM",
    attempt: "LLM",
    tool: "TOOL",
    activity: "CHAIN",
    compaction: "CHAIN",
  }[f.unit];
  const values: Record<string, string | number | boolean | undefined> = {
    "session.id": f.sessionId,
    "fi.span.kind": kind,
    [A.FI_AGENT_NAME]: f.agent,
    [A.FI_AGENT_SURFACE]: f.surface,
    [A.FI_AGENT_VERSION]: f.agentVersion,
    [A.FI_AGENT_TURN_ID]: f.turnId,
    [A.FI_AGENT_OUTCOME]: f.outcome,
    [A.FI_CAPTURE_SOURCE]: f.source,
    [A.FI_CAPTURE_SCHEMA_VERSION]: f.sourceSchema,
    [A.FI_CAPTURE_RECONSTRUCTION_VERSION]: f.reconstructionVersion,
    [A.FI_CAPTURE_TIMING]: f.timing,
    [A.FI_CAPTURE_INPUT_COMPLETENESS]: f.inputCompleteness,
    [A.FI_CAPTURE_OUTPUT_COMPLETENESS]: f.outputCompleteness,
    [A.FI_CAPTURE_CONTENT_POLICY_VERSION]: f.policyRevision,
    [A.FI_CAPTURE_SOURCE_WORK_ID]: f.sourceWorkId,
    [A.FI_CAPTURE_IDENTITY_PROFILE]: f.identityProfile,
    [A.FI_CAPTURE_OBSERVATION_UNIT]: f.unit,
    [A.FI_CAPTURE_PARTIAL]: f.incomplete,
    [A.FI_CAPTURE_LATE_CAPTURE]: f.lateCapture,
    [A.FI_AGENT_CAUSED_BY_WORK_ID]: f.causedBy,
    [A.FI_USAGE_CONTRIBUTION]: f.usage?.contribution ?? "none",
    [A.FI_SOURCE_OPERATION]: f.native?.operation,
    [A.FI_SOURCE_SUMMARY_INPUT_TOKENS]: f.native?.summary?.input,
    [A.FI_SOURCE_SUMMARY_OUTPUT_TOKENS]: f.native?.summary?.output,
    [A.FI_SOURCE_SUMMARY_CACHE_READ_TOKENS]: f.native?.summary?.cacheRead,
    [A.FI_SOURCE_SUMMARY_CACHE_WRITE_TOKENS]: f.native?.summary?.cacheWrite,
    [A.FI_SOURCE_SUMMARY_REASONING_TOKENS]: f.native?.summary?.reasoning,
    [A.FI_SOURCE_BILLING_MULTIPLIER]: f.native?.billing?.multiplier,
    [A.FI_SOURCE_BILLING_NANO_AIU]: f.native?.billing?.nanoAiu,
    "gen_ai.operation.name":
      f.native?.operation ??
      (kind === "LLM"
        ? "chat"
        : kind === "TOOL"
          ? "execute_tool"
          : kind === "AGENT"
            ? "invoke_agent"
            : undefined),
    "gen_ai.request.model": f.model,
    "gen_ai.provider.name": f.provider,
    "tool.name": f.tool,
    [A.FI_CAPTURE_MISSING_REASON]: f.content ? undefined : "disabled_by_policy",
  };
  const u = f.usage;
  if (u)
    Object.assign(values, {
      [A.FI_USAGE_SCOPE]: u.scope,
      [A.FI_USAGE_PLAN_ID]: u.planId,
      [A.FI_USAGE_FACT_ID]: JSON.stringify(u.factIds),
      [A.FI_USAGE_INCLUSIONS]: u.inclusions,
      [A.FI_USAGE_COMPLETE]: u.complete,
    });
  if (u && u.contribution !== "none")
    Object.assign(values, {
      "gen_ai.usage.input_tokens": u.input,
      "gen_ai.usage.output_tokens": u.output,
      "gen_ai.usage.total_tokens": u.total,
      "gen_ai.usage.cache_read_tokens": u.cacheRead,
      "gen_ai.usage.cache_write_tokens": u.cacheWrite,
      "gen_ai.usage.output_tokens.reasoning": u.reasoning,
      "gen_ai.cost.total": u.costUsd,
      [A.FI_COST_KIND]:
        u.costUsd === undefined ? "unknown" : "provider_reported",
      [A.FI_COST_CURRENCY]: u.costUsd === undefined ? undefined : "USD",
    });
  // Explicit categories remain separate: a turn prompt is not full model context.
  for (const [category, text] of Object.entries(f.content ?? {}))
    values["fi.capture.content." + category] = text;
  return {
    traceId: Buffer.from(f.traceId, "hex"),
    spanId: Buffer.from(f.spanId, "hex"),
    ...(f.parentSpanId
      ? { parentSpanId: Buffer.from(f.parentSpanId, "hex") }
      : {}),
    name:
      f.unit === "tool"
        ? "tool." + (f.tool ?? "unknown")
        : f.unit === "request" || f.unit === "attempt"
          ? "model.request"
          : "agent." + f.unit,
    kind: f.native?.kind ?? 1,
    startTimeUnixNano: f.startNs,
    endTimeUnixNano: f.endNs,
    attributes: attrs(values),
    status: { code: f.native?.statusCode ?? (f.outcome === "error" ? 2 : 0) },
    links: (f.links ?? []).map((l) => ({
      traceId: Buffer.from(l.traceId, "hex"),
      spanId: Buffer.from(l.spanId, "hex"),
    })),
  };
}
export function encodeFacts(
  facts: CanonicalFact[],
  binding: Binding,
  runtimeVersion: string,
): Buffer {
  return encodeTraceObject({
    resourceSpans: facts.map((f) => ({
      resource: {
        attributes: attrs({
          project_name: binding.project,
          project_type: "observe",
          "service.name": "fi-agent/" + f.agent,
          "service.version": runtimeVersion,
        }),
      },
      scopeSpans: [
        {
          scope: { name: "@traceai/agents", version: runtimeVersion },
          spans: [spanObject(f)],
        },
      ],
    })),
  });
}
