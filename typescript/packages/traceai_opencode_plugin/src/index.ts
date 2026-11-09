import { Plugin } from "@opencode/plugin";
import { createTurnTracer, resolveConfig } from "@traceai/agent-core";
import { Observer } from "./observer.js";

export const VERSION = "0.1.0"; // x-release-please-version
const KEY = Symbol.for("@traceai/opencode-plugin/v2/runtime");
type Runtime = { observer: Observer; refs: number };
const globals = globalThis as typeof globalThis & { [KEY]?: Map<string, Promise<Runtime>> };
const runtimes = globals[KEY] ??= new Map();

/** Qualified V2 contract; auto-updated patch/minor hosts must not silently stop tracing. */
export function supportedHost(version: string): boolean {
  const match = /^v?2\.(\d+)\.(\d+)$/.exec(version);
  return !!match && (Number(match[1]) > 2 || (Number(match[1]) === 2 && Number(match[2]) >= 5));
}

/** This is a real V2 definition, not a V1 hook-object export. */
export default Plugin.define({
  id: "futureagi.tracing",
  async setup(ctx) {
    // Version line plus capability shape, not equality to the qualification binary.
    if (!supportedHost(ctx.app?.version ?? "") || typeof ctx.session?.hook !== "function" ||
        typeof ctx.tool?.hook !== "function" || typeof ctx.event?.subscribe !== "function") return;
    const config = await resolveConfig({ agent: "opencode", projectDir: ctx.location.directory }).catch(() => undefined);
    if (!config?.enabled || !config.credentials || !config.destinationApproved) return;
    const key = JSON.stringify([ctx.location.directory, ctx.location.workspaceID, config.endpoint,
      config.projectName, config.generation, config.profile, config.captureContent]);
    let pending = runtimes.get(key);
    if (!pending) {
      pending = Promise.resolve({ observer: new Observer(createTurnTracer({ config }), config, ctx.location), refs: 0 });
      runtimes.set(key, pending);
    }
    const runtime = await pending;
    runtime.refs++;
    const observer = runtime.observer;
    const controller = new AbortController();
    const registrations: Array<{ dispose(): Promise<void> }> = [];
    const safe = (fn: () => void): void => { try { fn(); } catch { /* neutral fail-open hook */ } };
    const events = (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) observer.event(event);
      } catch { /* Host shutdown or subscription failure never changes an agent result. */ }
    })();
    let cleaned = false;
    const cleanup = async (): Promise<void> => {
      if (cleaned) return;
      cleaned = true;
      controller.abort();
      await Promise.allSettled(registrations.map(r => r.dispose()));
      await events;
      if (--runtime.refs === 0) {
        runtimes.delete(key);
        await observer.close();
      }
    };
    try {
      for (const name of ["context", "compaction"] as const)
        registrations.push(await ctx.session.hook(name, e => safe(() => observer.context(e.sessionID, e.messages, e.system, name === "compaction" ? "compaction" : "primary"))));
      registrations.push(await ctx.tool.hook("execute.before", e => safe(() => observer.toolBefore(e.sessionID, e.id, e.tool, e.input, e.messageID))));
      registrations.push(await ctx.tool.hook("execute.after", e => safe(() => observer.toolAfter(e.sessionID, e.id,
        e.status === "completed" ? e.result.content : e.error, e.status === "error"))));
      return cleanup;
    } catch {
      await cleanup();
      return;
    }
  },
});
