import { deliveryStatePath } from "@traceai/agent-core/config";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import { resolveConfig, readJson, updateJson, collectionEnabled, createTurnTracer, type ResolvedConfig, type DeliveryState } from "@traceai/agent-core";
import type { Registration } from "./adapter";
import { normalize } from "./events";
import { statePath, fresh, load, save, lockState } from "./state";

async function diagnostic(config: ResolvedConfig, reason?: string, dropped = 0): Promise<void> {
  try {
    await updateJson<DeliveryState>(deliveryStatePath(config), (old) => ({
      pending: old?.pending ?? 0, exported: old?.exported ?? 0, dropped: (old?.dropped ?? 0) + dropped,
      activatedAt: old?.activatedAt ?? new Date().toISOString(), lastAcceptedAt: old?.lastAcceptedAt,
      lastError: reason ?? old?.lastError,
    }));
  } catch { /* Never emit raw events, stack traces or credential-bearing errors. */ }
}
export async function handlePayload(payload: unknown, config: ResolvedConfig): Promise<void> {
  const event = normalize(payload, config);
  if (!event) return;
  const path = statePath(config, event.session);
  if (!collectionEnabled(config)) {
    await fs.unlink(path).catch(() => {});
    return;
  }
  let unlock: (() => Promise<void>) | undefined;
  try {
    unlock = await lockState(path);
    const state = await load(path, config);
    await diagnostic(config);
    if (event.event === "SessionStart") {
      // Resume must not destroy pending tools for this conversation.
      await save(path, state, config);
      return;
    }
    if (event.event === "PostToolUse") {
      state.tools.push(event.tool!);
      const previousDropped = state.dropped;
      await save(path, state, config);
      if (state.dropped > previousDropped) await diagnostic(config, "AUGMENT_STATE_LIMIT", state.dropped - previousDropped);
      return;
    }
    if (event.event === "SessionEnd" && !state.tools.length) {
      await fs.unlink(path).catch(() => {});
      return;
    }
    // Checkpoint before sending. No replay after an ambiguous result, and no destination rerouting.
    // Stop has no source turn/request id: this is explicitly a stop-summary, never an invented LLM call.
    await save(path, fresh(config), config);
    await unlock(); unlock = undefined;
    const tracer = createTurnTracer({ config });
    const turn = tracer.startTurn({ unit: "stop-summary", sessionId: event.session, turnId: state.id, input: event.input, usagePlan: "unknown" });
    for (const [index, tool] of state.tools.entries()) {
      const child = turn.child({ id: `observed-tool-${index}`, kind: "tool", name: tool.name, input: tool.input, fidelity: "unknown" });
      child.end({ output: tool.output, outcome: tool.outcome });
    }
    turn.end({ output: event.output, outcome: event.event === "SessionEnd" || state.dropped ? "incomplete" : event.outcome });
    await tracer.flush();
    await tracer.shutdown();
    if (event.event === "SessionEnd") await fs.unlink(path).catch(() => {});
  } catch {
    await diagnostic(config, "AUGMENT_HOOK_DROPPED", 1);
  } finally { await unlock?.(); }
}
async function input(): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const part of process.stdin) {
    const bytes = Buffer.from(part);
    size += bytes.length;
    if (size > 1024 * 1024) throw new Error("HOOK_TOO_LARGE");
    chunks.push(bytes);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
export async function main(): Promise<void> {
  const timeout = setTimeout(() => process.exit(0), 6000);
  try {
    const registration = await readJson<Registration>(process.argv[2]);
    if (!registration || typeof registration.agentDir !== "string" || typeof registration.configDir !== "string" || typeof registration.stateHome !== "string") return;
    const agent = registration.agent ?? "augment";
    if (agent !== "augment" && agent !== "augment-ide") return;
    const payload = await input();
    const config = await resolveConfig({ agent, agentDir: registration.agentDir,
      projectDir: process.env.AUGMENT_PROJECT_DIR || process.cwd(),
      env: { ...process.env, FI_AGENT_CONFIG_DIR: process.env.FI_AGENT_CONFIG_DIR || registration.configDir,
        XDG_STATE_HOME: process.env.XDG_STATE_HOME || registration.stateHome },
    });
    await handlePayload(payload, config);
  } catch { /* Auggie's neutral protocol is empty stdout, empty stderr, exit zero. */ }
  finally { clearTimeout(timeout); }
}
if (require.main === module) void main().then(() => process.exit(0), () => process.exit(0));
