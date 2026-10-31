import {
  minimize,
  opaqueId,
  planId,
  sessionId,
  usageId,
  type CanonicalFact,
  type SourceCounters,
} from "@traceai/agent-core";
import type { Adapter, Capability, NormalizeContext } from "../adapters.js";
import type { NativeSpanEnvelope } from "../runtime/receiver.js";
import { AgentError } from "../security/files.js";

export const COPILOT_SCHEMA = "copilot-cli-1.0.98-otel";
export const COPILOT_IDENTITY = "copilot-native-ids-v1";
const namespace = "github-copilot-native";
const models = ["gpt-6-luna"] as const;
const providers = ["github", "anthropic", "openai"] as const;
const tools = [
  "glob",
  "bash",
  "view",
  "readFile",
  "writeFile",
  "task",
] as const;
const operations = [
  "invoke_agent",
  "chat",
  "execute_tool",
  "execute_hook",
] as const;

/** Parse only OTLP scalar fields we explicitly use, never copy attribute maps. */
function attributes(value: unknown): Map<string, unknown> {
  if (!Array.isArray(value) || value.length > 128)
    throw new AgentError("INVALID_NATIVE_ATTRIBUTES", 2);
  const result = new Map<string, unknown>();
  for (const item of value) {
    if (!item || typeof item.key !== "string" || result.has(item.key))
      throw new AgentError("INVALID_NATIVE_ATTRIBUTES", 2);
    result.set(item.key, item.value);
  }
  return result;
}
function string(value: unknown): string | undefined {
  const v = value as { stringValue?: unknown } | undefined;
  if (typeof v?.stringValue !== "string" || v.stringValue.length > 4096)
    return undefined;
  return v.stringValue;
}
function counter(value: unknown, integral = true): number | undefined {
  if (value === undefined) return undefined;
  const v = value as { intValue?: unknown; doubleValue?: unknown };
  // Malformed counters are held, never coerced from booleans/null/empty strings.
  if (v?.intValue !== undefined && v?.doubleValue !== undefined)
    throw new AgentError("UNRESOLVED_USAGE", 3);
  let n: number;
  if (typeof v?.intValue === "string" && /^(0|[1-9][0-9]*)$/.test(v.intValue))
    n = Number(v.intValue);
  else if (typeof v?.intValue === "number") n = v.intValue;
  else if (typeof v?.doubleValue === "number") n = v.doubleValue;
  else throw new AgentError("UNRESOLVED_USAGE", 3);
  if (
    !Number.isFinite(n) ||
    n < 0 ||
    n > Number.MAX_SAFE_INTEGER ||
    (integral && !Number.isSafeInteger(n))
  )
    throw new AgentError("UNRESOLVED_USAGE", 3);
  return n;
}
function alias(
  a: Map<string, unknown>,
  keys: readonly string[],
): number | undefined {
  const values = keys
    .map((k) => counter(a.get(k)))
    .filter((v) => v !== undefined);
  if (values.some((v) => v !== values[0]))
    throw new AgentError("CONFLICTING_USAGE_ALIASES", 3);
  return values[0];
}
function counters(a: Map<string, unknown>): SourceCounters {
  // Copilot input/output already include cache/reasoning. Never add them again.
  const values = {
    input: counter(a.get("gen_ai.usage.input_tokens")),
    output: counter(a.get("gen_ai.usage.output_tokens")),
    cacheRead: alias(a, ["gen_ai.usage.cache_read.input_tokens"]),
    cacheWrite: alias(a, [
      "gen_ai.usage.cache_write.input_tokens",
      "gen_ai.usage.cache_creation.input_tokens",
    ]),
    reasoning: counter(a.get("gen_ai.usage.reasoning.output_tokens")),
  };
  return Object.fromEntries(
    Object.entries(values).filter(([, v]) => v !== undefined),
  );
}
function id(value: unknown, bytes: number): string {
  if (typeof value !== "string") throw new AgentError("INVALID_NATIVE_ID", 2);
  const hex = new RegExp(`^[a-fA-F0-9]{${bytes * 2}}$`).test(value)
    ? value.toLowerCase()
    : Buffer.from(value, "base64").toString("hex");
  if (
    (!new RegExp(`^[a-fA-F0-9]{${bytes * 2}}$`).test(value) &&
      Buffer.from(hex, "hex").toString("base64") !== value) ||
    !new RegExp(`^[a-f0-9]{${bytes * 2}}$`).test(hex) ||
    /^0+$/.test(hex)
  )
    throw new AgentError("INVALID_NATIVE_ID", 2);
  return hex;
}
function known(
  value: string | undefined,
  allowed: readonly string[],
): string | undefined {
  return value !== undefined && allowed.includes(value) ? value : undefined;
}

/**
 * CLI 1.0.98 native span profile. Resource AND scope must match: a model alias,
 * operation or service name alone is not proof that parent usage is a summary.
 * No transcript/hook join, guessed request identity, raw text or new topology.
 */
export function normalizeCopilotCli(
  input: unknown,
  context: NormalizeContext,
): CanonicalFact {
  const envelope = input as NativeSpanEnvelope;
  if (context.sourceSchema !== COPILOT_SCHEMA || !envelope?.span)
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  const resource = attributes(envelope.resource?.attributes);
  if (
    string(resource.get("service.name")) !== "github-copilot" ||
    string(resource.get("service.version")) !== "1.0.98" ||
    envelope.scope?.name !== "github.copilot" ||
    envelope.scope?.version !== "1.0.98"
  )
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  const span = envelope.span;
  const a = attributes(span.attributes);
  const operation = string(a.get("gen_ai.operation.name"));
  if (!operations.includes(operation as (typeof operations)[number]))
    throw new AgentError("UNKNOWN_SOURCE_OPERATION", 5);
  const conversation = string(a.get("gen_ai.conversation.id"));
  if (!conversation) throw new AgentError("HELD_NATIVE_SESSION_IDENTITY", 3);
  const trace = id(span.traceId, 16),
    sourceSpan = id(span.spanId, 8);
  const parent = span.parentSpanId ? id(span.parentSpanId, 8) : undefined;
  if (
    span.links !== undefined &&
    (!Array.isArray(span.links) || span.links.length > 32)
  )
    throw new AgentError("INVALID_NATIVE_LINKS", 2);
  const links = span.links?.map((link) => ({
    traceId: id(link.traceId, 16),
    spanId: id(link.spanId, 8),
  }));
  // OTLP trace context is the only anchor common to all spans in this version.
  // Native trace-scoped plan; never derive it from chat-only github.copilot.turn_id.
  const turn = opaqueId(context.identityKey, namespace + ".trace", trace);
  const work = opaqueId(
    context.identityKey,
    namespace + ".span",
    trace + "/" + sourceSpan,
  );
  const raw = counters(a);
  const request = operation === "chat";
  const statusCode = span.status?.code ?? 0;
  const expectedKind = request ? 3 : 1; // OTLP CLIENT vs INTERNAL, not JS SDK enum.
  if (
    span.kind !== expectedKind ||
    (statusCode !== 0 && statusCode !== 1 && statusCode !== 2)
  )
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  const native: NonNullable<CanonicalFact["native"]> = {
    kind: expectedKind,
    statusCode,
    operation: operation as (typeof operations)[number],
  };
  if (operation === "invoke_agent" && Object.keys(raw).length)
    native.summary = raw;
  const multiplier = counter(a.get("github.copilot.cost"), false);
  const nanoAiu = counter(a.get("github.copilot.nano_aiu"));
  if (multiplier !== undefined || nanoAiu !== undefined)
    native.billing = {
      ...(multiplier === undefined ? {} : { multiplier }),
      ...(nanoAiu === undefined ? {} : { nanoAiu }),
    };
  const fact: CanonicalFact = {
    schemaVersion: 1,
    sourceWorkId: work,
    identityProfile: COPILOT_IDENTITY,
    sessionId: sessionId(context.identityKey, namespace, conversation),
    turnId: turn,
    traceId: trace,
    spanId: sourceSpan,
    ...(parent ? { parentSpanId: parent } : {}),
    ...(links ? { links } : {}),
    agent: "copilot",
    surface: "cli",
    agentVersion: "1.0.98",
    source: "native",
    sourceSchema: COPILOT_SCHEMA,
    reconstructionVersion: "1",
    policyRevision: context.policyRevision,
    unit: request
      ? "request"
      : operation === "invoke_agent"
        ? "turn"
        : operation === "execute_tool"
          ? "tool"
          : "activity",
    timing: request ? "observed_request" : "unknown",
    startNs: span.startTimeUnixNano,
    endNs: span.endTimeUnixNano,
    outcome: statusCode === 2 ? "error" : "success",
    inputCompleteness: "unavailable",
    outputCompleteness: "unavailable",
    model: known(string(a.get("gen_ai.request.model")), models),
    provider: known(string(a.get("gen_ai.provider.name")), providers),
    tool:
      operation === "execute_tool"
        ? known(string(a.get("gen_ai.tool.name")), tools)
        : undefined,
    native,
    usage: {
      scope: request ? "request" : "turn",
      // Even a root-only export stays non-additive; absent children are not evidence
      // of turn-only billing. This rule is independent of export batch ordering.
      contribution: request ? "request" : "none",
      planId: planId(turn),
      factIds: request
        ? Object.keys(raw).map((metric) =>
            usageId(
              context.identityKey,
              work,
              "request",
              metric,
              "native-counter",
            ),
          )
        : [],
      inclusions: "inclusive",
      complete: request && raw.input !== undefined && raw.output !== undefined,
      ...(request ? raw : {}),
    },
  };
  // Missing usage is unknown, not zero. Still count an observed chat as a request
  // using its identity fact; no numeric usage or fake zero cost is manufactured.
  if (request && !fact.usage!.factIds.length)
    fact.usage!.factIds.push(
      usageId(
        context.identityKey,
        work,
        "request",
        "observed-call",
        "native-counter",
      ),
    );
  return minimize(fact, {
    revision: context.policyRevision,
    categories: [],
    ceiling: [],
  });
}

const capability: Capability = {
  contract: 1,
  agent: "copilot",
  surface: "cli",
  versions: ["1.0.98"],
  schemas: [COPILOT_SCHEMA],
  identityProfiles: [COPILOT_IDENTITY],
  models,
  providers,
  tools,
  platforms: ["darwin", "linux"],
  transcript: false,
  native: true,
  neutralResponse: "",
};
const inactive = {
  installed: false,
  trusted: "unknown" as const,
  activated: false,
  restartRequired: false,
};
export const copilotCliAdapter: Adapter = {
  capability,
  // CLI 1.0.98 documents one endpoint for traces AND metrics. The foundation
  // accepts traces only; we must not redirect/drop another signal or vendor.
  activationHolds: [
    "COPILOT_SIGNAL_ISOLATION_UNQUALIFIED",
    "COPILOT_EFFECTIVE_MANAGED_SETTINGS_UNQUALIFIED",
  ],
  async preflight(action) {
    if (action === "enable" || action === "update")
      throw new AgentError("COPILOT_SIGNAL_ISOLATION_UNQUALIFIED", 5);
    return { commit: async () => inactive, rollback: async () => {} };
  },
  async probe() {
    return inactive;
  },
  normalize(input, context) {
    return [normalizeCopilotCli(input, context)];
  },
};
export const copilotVSCodeAdapter: Adapter = {
  ...copilotCliAdapter,
  capability: { ...capability, surface: "vscode", versions: [], schemas: [] },
  activationHolds: [
    "COPILOT_VSCODE_HARNESS_UNQUALIFIED",
    "COPILOT_VSCODE_SETTINGS_TRANSACTION_UNQUALIFIED",
  ],
  async preflight(action) {
    if (action === "enable" || action === "update")
      throw new AgentError("COPILOT_VSCODE_HARNESS_UNQUALIFIED", 5);
    return { commit: async () => inactive, rollback: async () => {} };
  },
  normalize() {
    throw new AgentError("COPILOT_VSCODE_HARNESS_UNQUALIFIED", 5);
  },
};
