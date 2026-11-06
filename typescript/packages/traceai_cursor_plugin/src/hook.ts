import { isAbsolute, join } from "node:path";
import {
  resolveConfig,
  collectionEnabled,
  updateJson,
} from "@traceai/agent-core/hook-state";
import { normalizeEvent, object } from "./events.js";
import { appendEvent, pendingTurnIds } from "./state.js";

/** Cursor's empty-output fail-open protocol: never emit a permission decision or follow-up text.
 * Nonterminal hooks persist only bounded encrypted policy-filtered state. The terminal hook
 * settles briefly; late transcript writes go to one detached, deadline-bound finisher.
 * No persistent daemon or receiver.
 */
export async function hook(args = process.argv.slice(2)): Promise<void> {
  const fields: Record<string, string> = {};
  if (args.length !== 6) return;
  for (let i = 0; i < args.length; i += 2) {
    if (
      !["--marker", "--config-dir", "--event"].includes(args[i]) ||
      fields[args[i]]
    )
      return;
    fields[args[i]] = args[i + 1];
  }
  const configDir = fields["--config-dir"],
    eventName = fields["--event"];
  if (
    fields["--marker"] !== "fi-cursor-standard-v1" ||
    !isAbsolute(configDir ?? "")
  )
    return;
  const timer = setTimeout(() => process.exit(0), 8000);
  try {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > 256 * 1024) return;
      chunks.push(Buffer.from(chunk));
    }
    const raw: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const roots = object(raw)?.workspace_roots;
    if (Array.isArray(roots) && roots.length > 1) return;
    const projectDir =
      Array.isArray(roots) &&
      roots.length === 1 &&
      typeof roots[0] === "string" &&
      isAbsolute(roots[0])
        ? roots[0]
        : undefined;
    const env = { ...process.env, FI_AGENT_CONFIG_DIR: configDir };
    const config = await resolveConfig({ agent: "cursor", env, projectDir });
    if (
      !collectionEnabled(config) ||
      !config.destinationApproved ||
      !config.credentials
    )
      return;
    let event;
    try {
      event = normalizeEvent(raw, eventName, config);
    } catch (error) {
      const message = (error as Error).message;
      const gap = /^[A-Z_]{1,64}$/.test(message)
        ? message
        : "INVALID_CURSOR_PAYLOAD";
      await updateJson(
        join(config.paths.stateDir, "cursor-diagnostics.json"),
        () => ({
          schemaVersion: 1,
          gaps: [gap, ...config.issues],
          observedAt: new Date().toISOString(),
        }),
      );
      return;
    }
    if (event.gap || config.issues.length) {
      await updateJson(
        join(config.paths.stateDir, "cursor-diagnostics.json"),
        () => ({
          schemaVersion: 1,
          gaps: [...(event.gap ? [event.gap] : []), ...config.issues],
          observedAt: new Date().toISOString(),
        }),
      ).catch(() => {});
    }
    try {
      await appendEvent(config, event, { defer: true, projectDir });
    } catch (error) {
      const gap =
        ((error as NodeJS.ErrnoException).code === "ELOCKED" || (error as Error).message === "CONCURRENT_SAVE")
          ? "CURSOR_STATE_CONTENTION"
          : "CURSOR_STATE_REJECTED";
      await updateJson(
        join(config.paths.stateDir, "cursor-diagnostics.json"),
        () => ({
          schemaVersion: 1,
          gaps: [gap, ...config.issues],
          observedAt: new Date().toISOString(),
        }),
      ).catch(() => {});
      return;
    }
    if (!collectionEnabled(config) || !(await pendingTurnIds(config)).length)
      return;
    const { processPending } =
      require("./runtime.js") as typeof import("./runtime.js");
    await processPending(config);
  } catch {
    /* Never print provider/filesystem errors or change the agent's exit status. */
  } finally {
    clearTimeout(timer);
  }
}
if (require.main === module)
  hook()
    .catch(() => {})
    .finally(() => {
      process.exitCode = 0;
    });
