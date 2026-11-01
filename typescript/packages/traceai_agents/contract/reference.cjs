"use strict";
// Synthetic fixture adapter; not exported, packed, or registered by fi-agent.
const c = require("@traceai/agent-core");
const capability = {
  contract: 1,
  agent: "reference",
  surface: "fixture",
  versions: ["1"],
  schemas: ["fixture-v1"],
  identityProfiles: ["reference-v1"],
  models: ["fixture-model"],
  providers: ["fixture"],
  tools: ["read_file"],
  platforms: ["darwin", "linux"],
  transcript: true,
  native: false,
  neutralResponse: "",
};
function normalize(raw, context) {
  if (
    !raw ||
    raw.schema !== "fixture-v1" ||
    typeof raw.session !== "string" ||
    typeof raw.turn !== "string" ||
    typeof raw.id !== "string"
  )
    throw Error("UNKNOWN_SCHEMA");
  const key = context.identityKey,
    ns = "futureagi.reference/fixture";
  const session = [raw.session],
    turn = ["thread", raw.turn];
  const turnWork = c.workId(key, ns, "turn", session, turn);
  const work = c.workId(key, ns, raw.unit, session, turn, [
    "thread",
    raw.turn,
    raw.id,
  ]);
  const fact = {
    schemaVersion: 1,
    sourceWorkId: work,
    identityProfile: "reference-v1",
    sessionId: c.sessionId(key, ns, session),
    turnId: turnWork,
    traceId: c.traceId(turnWork),
    spanId: c.spanId(work),
    ...(raw.unit !== "turn" ? { parentSpanId: c.spanId(turnWork) } : {}),
    agent: "reference",
    surface: "fixture",
    agentVersion: "1",
    source: "transcript",
    sourceSchema: "fixture-v1",
    reconstructionVersion: "1",
    policyRevision: context.policyRevision,
    unit: raw.unit,
    timing: "message_interval",
    startNs: "1000000000",
    endNs: "2000000000",
    outcome: "success",
    inputCompleteness: "unavailable",
    outputCompleteness: "unavailable",
    ...(raw.unit === "request"
      ? {
          model: "fixture-model",
          provider: "fixture",
          usage: {
            scope: "request",
            contribution: "request",
            planId: c.planId(turnWork),
            factIds: [c.usageId(key, work, "request", "input", "counter-0")],
            input: 100,
            output: 20,
            total: 120,
            cacheRead: 40,
            reasoning: 8,
            inclusions: "inclusive",
            complete: true,
          },
        }
      : {}),
    ...(raw.unit === "tool" ? { tool: "read_file" } : {}),
    content: { prompts: raw.prompt ?? "", responses: raw.response ?? "" },
  };
  return [c.minimize(fact)];
}
module.exports = {
  capability,
  normalize,
  import: (bytes, context) =>
    JSON.parse(bytes).flatMap((raw) => normalize(raw, context)),
  probe: async () => ({
    installed: true,
    trusted: true,
    activated: false,
    restartRequired: false,
  }),
  preflight: async () => ({
    commit: async () => ({
      installed: true,
      trusted: true,
      activated: false,
      restartRequired: false,
    }),
    rollback: async () => {},
  }),
};
