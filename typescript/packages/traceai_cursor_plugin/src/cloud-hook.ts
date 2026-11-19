import { collectionEnabled, resolveConfig } from "@traceai/agent-core/hook-state";
import { normalizeEvent, EVENTS } from "./events.js";
import { appendEvent } from "./state.js";

/** An observing command hook, not a policy hook. Exit 1 with no output is Cursor's documented
 * fail-open path (exit 0 + malformed permission output can block; exit 2 explicitly denies).
 * Never emit permission decisions, follow-up messages or modified tool inputs.
 */
export async function cloudHook(args = process.argv.slice(2)): Promise<void> {
  if (process.env.FI_CURSOR_CLOUD !== "true") return;
  if (args.length !== 4 || args[0] !== "--marker" || args[1] !== "fi-cursor-cloud-v1" || args[2] !== "--event") return;
  const eventName = args[3];
  if (!EVENTS.includes(eventName as typeof EVENTS[number]) || eventName === "sessionEnd") return;
  const timer = setTimeout(() => process.exit(1), 8000);
  try {
    const config = await resolveConfig({ agent: "cursor-cloud", projectDir: process.cwd() });
    if (!collectionEnabled(config) || !config.credentials || !config.destinationApproved) return;
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > 256 * 1024) return;
      chunks.push(Buffer.from(chunk));
    }
    const event = normalizeEvent(JSON.parse(Buffer.concat(chunks).toString("utf8")), eventName, config, "cloud");
    // Cloud's hosted transcript path/schema is not qualified; never read it or persist filesystem identity.
    event.projectDir = undefined;
    const snapshot = await appendEvent(config, event);
    if (!snapshot || !collectionEnabled(config)) return;
    const { exportCloudSnapshot } = await import("./cloud-runtime.js");
    await exportCloudSnapshot(config, snapshot);
  } catch {
    // No payload, environment or provider errors escape into the agent's conversation or logs.
  } finally {
    clearTimeout(timer);
  }
}
if (require.main === module)
  cloudHook().catch(() => {}).finally(() => { process.exitCode = 1; });
