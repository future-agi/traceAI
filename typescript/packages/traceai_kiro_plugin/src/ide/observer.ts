import { collectionEnabled, createTurnTracer, updateJson, deliveryStatePath, type DeliveryState, type ResolvedConfig } from "@traceai/agent-core";
import { parseIdeEvent, type IdeEventName } from "./events.js";
import { appendIdeEvent } from "./state.js";

const notification: Partial<Record<IdeEventName, string>> = {
  PostFileCreate: "file.created", PostFileSave: "file.saved", PostFileDelete: "file.deleted",
  PreTaskExec: "task.started", PostTaskExec: "task.finished",
  PreCompact: "compaction.started", PostCompact: "compaction.finished",
};
async function dropped(config: ResolvedConfig, code: string): Promise<void> {
  await updateJson<DeliveryState>(deliveryStatePath(config), old => ({
    pending: 0, exported: old?.exported ?? 0, dropped: (old?.dropped ?? 0) + 1,
    lastAcceptedAt: old?.lastAcceptedAt, activatedAt: old?.activatedAt, lastError: code,
  })).catch(() => {});
}
/** Internal candidate processor. Only the qualified host entry point may call this in production. */
export async function observeIdeEvent(config: ResolvedConfig, trigger: string, raw: unknown, userPrompt?: string): Promise<void> {
  if (!collectionEnabled(config)) return;
  try {
    const event = parseIdeEvent(trigger, raw, config, userPrompt);
    if (!event) { await dropped(config, "IDE_PAYLOAD_UNQUALIFIED"); return; }
    const snapshot = await appendIdeEvent(config, event);
    if (!snapshot || !collectionEnabled(config)) return;
    const prompt = snapshot.events.find(e => e.event === "UserPromptSubmit");
    const stop = snapshot.events.find(e => e.event === "Stop");
    if (!prompt || !stop) { await dropped(config, "MISSING_TURN_BOUNDARY"); return; }
    const tracer = createTurnTracer({ config });
    try {
      const turn = tracer.startTurn({ sessionId: prompt.session, turnId: snapshot.id, input: prompt.input,
        startTime: prompt.time, timing: "observed-event", usagePlan: "unknown" });
      let n = 0;
      for (const e of snapshot.events) {
        if (e.event === "PostToolUse") {
          // A post-tool notification is evidence of a tool invocation, not its duration or ancestry.
          turn.child({ id: `observed-${++n}`, kind: "tool", name: e.tool, input: e.input,
            startTime: e.time, timing: "observed-event", fidelity: "unknown" })
            .end({ output: e.output, outcome: e.failed === true ? "error" : e.failed === false ? "success" : "incomplete", endTime: e.time });
        } else if (notification[e.event]) turn.event({
          name: e.event === "PostTaskExec" && e.failed ? "task.failed" : notification[e.event]!, input: e.input, time: e.time });
      }
      if (snapshot.gap) turn.event({ name: `gap.${snapshot.gap}`, time: stop.time });
      // Stop proves a terminal hook, but its success/interruption outcome isn't pinned for IDE yet.
      turn.end({ output: stop.output, outcome: "incomplete", endTime: stop.time });
      await tracer.flush();
    } finally { await tracer.shutdown(); }
  } catch { await dropped(config, "IDE_EVENT_DROPPED"); }
}
