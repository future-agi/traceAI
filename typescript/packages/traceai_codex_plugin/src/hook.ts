import {
  ensurePrivateDirectory,
  writeProtected,
} from "@traceai/agent-core/files";
import { resolveConfig } from "@traceai/agent-core/config";
import { collectionEnabled } from "@traceai/agent-core/collection";
import { join } from "node:path";
import { homedir } from "node:os";
async function diagnostic(code: string, gap = false): Promise<void> {
  if (!gap && process.env.FI_AGENT_DEBUG !== "true") return;
  const dir = join(
    process.env.XDG_STATE_HOME ?? join(homedir(), ".local", "state"),
    "futureagi",
    "agents",
  );
  try {
    await ensurePrivateDirectory(dir);
    await writeProtected(
      join(dir, gap ? "codex-gap.json" : "codex-hook.json"),
      JSON.stringify({ at: new Date().toISOString(), code }),
      undefined,
      { lockWaitMs: 0 },
    );
  } catch {
    // Concurrent parent/child hooks may contend for this optional diagnostic.
    // Debug logging must never suppress collection or alter the host's work.
  }
}
import { sanitizeEvent } from "./events";
import { EventStore } from "./state";
import { correlate } from "./ancestry";
import { RolloutReader } from "./rollout";
import type { Event } from "./events";
function finalEvents(events: Event[]): Event[] {
  const terminal = events
    .filter(
      (e) => e.hook_event_name === "Stop" || e.hook_event_name === "Interrupt",
    )
    .at(-1);
  if (!terminal) return events;
  return [
    ...events.filter(
      (e) => e.hook_event_name !== "Stop" && e.hook_event_name !== "Interrupt",
    ),
    { ...terminal, receivedAt: Math.max(...events.map((e) => e.receivedAt)) },
  ];
}
async function main(): Promise<void> {
  if (["false", "0"].includes(process.env.FI_AGENT_ENABLED ?? "")) return;
  let data = "";
  for await (const chunk of process.stdin) {
    data += chunk;
    if (Buffer.byteLength(data) > 262144) return;
  }
  const raw = JSON.parse(data);
  if (typeof raw.cwd !== "string") return;
  if (["SessionEnd", "Interrupt"].includes(raw.hook_event_name)) {
    clearTimeout(timer);
    timer = setTimeout(() => process.exit(0), 2500);
    timer.unref();
  }
  const config = await resolveConfig({ agent: "codex", projectDir: raw.cwd });
  if (
    !config.enabled ||
    !config.credentials ||
    config.issues.some((issue) => issue !== "PROJECT_CONFIG_IGNORED")
  ) {
    await diagnostic(
      "CONFIG_INACTIVE_" +
        config.issues.filter((v) => /^[A-Z_]+$/.test(v)).join("_"),
    );
    return;
  }
  await diagnostic("HOOK_ACTIVE");
  const clean = sanitizeEvent(raw, config);
  if (
    !clean ||
    (!["SessionStart", "SessionEnd"].includes(clean.hook_event_name) &&
      !clean.turn_id)
  ) {
    await diagnostic("UNKNOWN_HOOK_SHAPE", true);
    return;
  }
  const event = await correlate(clean, config);
  if (!event) return;
  const store = new EventStore(config);
  // Setup advances the core epoch. Also check effective repository privacy while
  // waiting for late rollout rows; do not keep reading with a revoked snapshot.
  const captureActive = async () =>
    collectionEnabled(config) &&
    (!config.revalidate || (await config.revalidate()));
  if (!(await captureActive())) return;
  if (event.hook_event_name === "SessionStart") {
    await store.expire();
    return;
  }
  if (event.hook_event_name === "SessionEnd") {
    const { CodexEngine } = await import("./engine");
    const settledReaders: RolloutReader[] = [];
    const engine = new CodexEngine(config, undefined, settledReaders);
    for (const state of await store.takeSession(
      event.session_id,
      async (pending) => {
        const stop = pending.events
          .filter((e) => ["Stop", "Interrupt"].includes(e.hook_event_name))
          .at(-1);
        if (
          stop?.hook_event_name !== "Stop" ||
          !stop.transcript_path ||
          !stop.turn_id
        )
          return true;
        // SessionEnd has a native 3s ceiling. It must not steal a live asynchronous
        // Stop's journal while that observer is waiting for the rollout to settle.
        const reader = new RolloutReader(
          stop.transcript_path,
          stop.session_id,
          config,
          stop.turn_id,
        );
        for (let i = 0; i < 20; i++) {
          if (!(await captureActive())) return false;
          await reader.read();
          if (reader.completed.has(stop.turn_id)) {
            settledReaders.push(reader);
            return true;
          }
          if (!reader.hasMore) break;
        }
        return false;
      },
    )) {
      if (state.gap && state.events.length) state.events[0].gap = true;
      const events = finalEvents(state.events),
        first = events[0];
      if (
        first &&
        !events.some((e) => ["Stop", "Interrupt"].includes(e.hook_event_name))
      ) {
        const path =
          events.find((e) => e.transcript_path && !e.source_session_id)
            ?.transcript_path ?? event.transcript_path;
        const reader = path
          ? new RolloutReader(path, first.session_id, config, first.turn_id)
          : undefined;
        if (reader)
          for (let i = 0; i < 5; i++) {
            if (!(await captureActive())) {
              await engine.tracer.discard();
              return;
            }
            await reader.read();
            if (reader.completed.has(first.turn_id!)) break;
            await new Promise((r) => setTimeout(r, 20));
          }
        const terminal = reader?.completed.get(first.turn_id!);
        if (reader) settledReaders.push(reader);
        events.push({
          ...first,
          hook_event_name: "Stop",
          prompt: undefined,
          receivedAt: terminal?.at ?? event.receivedAt,
          last_assistant_message: terminal?.output,
          gap: !terminal,
        });
      }
      if (!(await captureActive())) {
        await engine.tracer.discard();
        return;
      }
      for (const e of events) await engine.accept(e);
    }
    await engine.close();
    return;
  }
  if (!event.turn_id) return;
  await store.append(event);
  // Child prompt hooks may precede SubagentStart in 0.167. Move only the exact
  // child turn journals established by that native event into the parent turn.
  if (
    clean.hook_event_name === "SubagentStart" &&
    clean.agent_id &&
    clean.turn_id &&
    clean.turn_id !== event.turn_id
  ) {
    for (const session of new Set([clean.session_id, clean.agent_id])) {
      const pending = await store.take({ ...clean, session_id: session });
      for (const e of pending?.events ?? []) {
        await store.append({
          ...e,
          session_id: event.session_id,
          turn_id: event.turn_id,
          agent_id: clean.agent_id,
          source_session_id: clean.agent_id,
          source_turn_id: e.turn_id,
          parent_agent_id: event.parent_agent_id,
          parent_turn_id: event.parent_turn_id,
          hook_event_name: ["Stop", "Interrupt"].includes(e.hook_event_name)
            ? "SubagentStop"
            : e.hook_event_name,
        });
      }
    }
  }
  if (!["Stop", "Interrupt"].includes(event.hook_event_name)) return;
  let settledReader: RolloutReader | undefined;
  if (event.hook_event_name === "Stop") {
    // Stop can precede a queued subagent notification. The host schedules this
    // observer asynchronously so we can wait for the source task_complete,
    // without blocking Codex or inventing successful completion from silence.
    if (!event.transcript_path) return;
    const reader = new RolloutReader(
      event.transcript_path,
      event.session_id,
      config,
      event.turn_id,
    );
    const deadline = Date.now() + 6000;
    for (let i = 0; i < 160 && Date.now() < deadline; i++) {
      if (!(await captureActive())) {
        await store.take(event); // Delete the stale content without exporting it.
        return;
      }
      await reader.read();
      if (reader.completed.has(event.turn_id)) break;
      if (!reader.hasMore) await new Promise((r) => setTimeout(r, 50));
    }
    const terminal = reader.completed.get(event.turn_id);
    if (!terminal) {
      // Extremely large histories resume on the next hook rather than restarting
      // the same unread backlog forever. Successful turns checkpoint after export.
      // A pending turn must retain its content, not only its terminal marker.
      if (!reader.targetSeen) await reader.checkpoint();
      return;
    }
    settledReader = reader;
    event.receivedAt = Number.isFinite(terminal.at)
      ? Math.max(terminal.at, event.receivedAt)
      : event.receivedAt;
    if (terminal.output !== undefined)
      event.last_assistant_message = terminal.output;
    await store.append(event);
  }
  const state = await store.take(event);
  if (!state || !(await captureActive())) return;
  const { CodexEngine } = await import("./engine");
  const engine = new CodexEngine(
    config,
    undefined,
    settledReader ? [settledReader] : [],
  );
  if (state.gap && state.events.length)
    state.events[state.events.length - 1].gap = true;
  // Only sanitized events are persisted, encrypted under the validated profile's local key.
  // The terminal hook owns bounded export; no daemon, collector, or detached process.
  for (const e of finalEvents(state.events)) await engine.accept(e);
  await engine.close();
}
// A host-visible permission/control directive is never emitted, including on export failure.
let timer = setTimeout(() => process.exit(0), 8000);
timer.unref();
main()
  .catch(async (e) => {
    await diagnostic(
      typeof e?.message === "string" && /^[A-Z_]{1,80}$/.test(e.message)
        ? e.message
        : "HOOK_FAILED",
    ).catch(() => {});
  })
  .finally(() => {
    clearTimeout(timer);
    process.exitCode = 0;
  });
