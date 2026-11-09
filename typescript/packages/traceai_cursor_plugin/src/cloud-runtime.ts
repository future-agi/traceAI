import { join } from "node:path";
import {
  collectionEnabled, resolveConfig, updateJson, type ResolvedConfig,
} from "@traceai/agent-core";
import { CursorObserver } from "./observer.js";
import { routeId, type TurnSnapshot } from "./state.js";

/** Load the exporter only at the terminal boundary, and fence every HTTP batch
 * against current destination/privacy settings rather than a stale hook snapshot.
 */
export async function exportCloudSnapshot(config: ResolvedConfig, snapshot: TurnSnapshot): Promise<void> {
  const { createTurnTracer } = await import("@traceai/agent-core");
  const tracer = createTurnTracer({ config, beforeExport: async () => {
    const current = await resolveConfig({ agent: "cursor-cloud", projectDir: process.cwd() });
    return collectionEnabled(current) && routeId(current) === routeId(config);
  } });
  const observer = new CursorObserver(config, tracer, undefined, "cloud");
  let discard = false;
  try {
    const rank = (name: string) => name === "beforeSubmitPrompt" ? 0 : name === "stop" ? 2 : 1;
    snapshot.events.sort((a, b) => rank(a.event) - rank(b.event) || a.receivedAt - b.receivedAt);
    for (const item of snapshot.events) {
      if (!collectionEnabled(config)) { discard = true; return; }
      await observer.accept(item);
    }
    await updateJson(join(config.paths.stateDir, "cursor-cloud-diagnostics.json"), () => ({
      schemaVersion: 1,
      gaps: ["EARLY_READ_ONLY_ACTIVITY_UNAVAILABLE", "MODEL_REQUESTS_UNAVAILABLE", ...observer.gaps,
        ...(snapshot.gap ? [snapshot.gap] : [])],
      hostQualification: "CONTRACT_ONLY_NOT_CLOUD_VALIDATION",
      observedAt: new Date().toISOString(),
    }));
  } finally {
    await observer.close(discard);
  }
}
