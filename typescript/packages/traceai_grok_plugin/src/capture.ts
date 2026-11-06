import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { join, dirname } from "node:path";
import {
  ensurePrivateDirectory,
  withFileLock,
  readJson,
  updateJson as coreUpdate,
} from "@traceai/agent-core/files";
import type { DeliveryState, ResolvedConfig } from "@traceai/agent-core";
import { deliveryStatePath } from "@traceai/agent-core/config";
import { GrokObserver } from "./observer.js";
import { TranscriptCursor } from "./transcript.js";
import { record, text, failedResult, type GrokEvent } from "./events.js";

async function updateJson<T>(
  path: string,
  update: (old: T | undefined) => T,
): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await coreUpdate(path, update, { maxBytes: 1024 * 1024 });
    } catch (error) {
      if (
        !(error instanceof Error) ||
        error.message !== "CONCURRENT_SAVE" ||
        attempt >= 3
      )
        throw error;
      await new Promise((resolve) => setTimeout(resolve, 10 * (attempt + 1)));
    }
  }
}
interface State {
  version: 1;
  updated: number;
  parent?: string;
  events: GrokEvent[];
  paths: Record<string, string>;
  claimed: string[];
  gap?: boolean;
}
const digest = (v: string): string =>
  createHash("sha256").update(v).digest("hex").slice(0, 32);
function directory(config: ResolvedConfig): string {
  return join(
    config.paths.configDir,
    "grok-state",
    digest(
      JSON.stringify([
        config.endpoint,
        config.projectName,
        config.profile,
        config.generation,
      ]),
    ),
  );
}
/** Persistent state contains structural event anchors only, never prompts, outputs or tool bodies. */
function structural(e: GrokEvent): GrokEvent {
  const {
    input: _input,
    output: _output,
    cwd: _cwd,
    transcriptPath: _path,
    ...meta
  } = e;
  return meta;
}
async function prune(dir: string): Promise<void> {
  const entries = (await fs.readdir(dir)).filter((p) =>
    /^[a-f0-9]{32}\.json$/.test(p),
  );
  for (const name of entries.slice(0, 256)) {
    const p = join(dir, name),
      st = await fs.lstat(p);
    if (
      st.isFile() &&
      !st.isSymbolicLink() &&
      st.mtimeMs < Date.now() - 24 * 60 * 60 * 1000
    )
      await fs.unlink(p);
  }
  if (entries.length >= 256) throw new Error("STATE_LIMIT");
}
export async function capture(
  event: GrokEvent,
  config: ResolvedConfig,
): Promise<void> {
  // resolveConfig has just read the persistent disable fence for this hook.
  if (!config.enabled || !config.destinationApproved) return;
  if (event.event === "SessionStart")
    await updateJson<DeliveryState>(
      deliveryStatePath(config),
      (old) => ({
        pending: 0,
        exported: 0,
        dropped: 0,
        ...old,
        binding: config.deliveryBinding,
        activatedAt: new Date().toISOString(),
      }),
    );
  const dir = directory(config);
  await ensurePrivateDirectory(dir);
  await prune(dir);
  let statePath = "", isChild = false;
  await withFileLock(join(dir, "routing"), async () => {
    const ownPath = join(dir, `${digest(event.sessionId)}.json`);
    let own = await readJson<State>(ownPath, true, 1024 * 1024);
    statePath = ownPath;
    for (let depth = 0; own?.parent && depth < 32; depth++) {
      isChild = true;
      statePath = join(dir, `${own.parent}.json`);
      own = await readJson<State>(statePath, true, 1024 * 1024);
    }
    let buffered: State | undefined;
    if (event.event === "SubagentStart" && event.subagentId) {
      const childPath = join(dir, `${digest(event.subagentId)}.json`);
      if (childPath !== statePath) {
        buffered = await readJson<State>(childPath, true, 1024 * 1024);
        // Every append re-resolves routing under this lease, including hooks that
        // started before association. No writer can still target the old journal.
        await updateJson<State>(childPath, () => ({
          version: 1, updated: Date.now(), events: [], paths: {}, claimed: [],
          parent: statePath.slice(dir.length + 1, -5),
        }));
      }
    }
    await updateJson<State>(statePath, old => {
      const state: State = old ?? { version: 1, updated: Date.now(), events: [], paths: {}, claimed: [] };
      state.updated = Date.now();
      state.paths = { ...state.paths, ...buffered?.paths };
      if (event.transcriptPath) state.paths[event.sessionId] = event.transcriptPath;
      const seen = new Set(state.events.map(e => JSON.stringify(e)));
      for (const meta of [structural(event), ...(buffered?.events ?? [])]) {
        const signature = JSON.stringify(meta);
        if (!seen.has(signature)) { state.events.push(meta); seen.add(signature); }
      }
      state.events.sort((a, b) => a.time - b.time);
      return state;
    });
  });
  if (
    ![
      "Stop",
      "StopFailure",
      "StopCancelled",
      "SessionEnd",
      "Notification",
      "UserPromptSubmit",
    ].includes(event.event) ||
    isChild
  )
    return;
  // Stop is a gate, not proof of completion: another hook can continue or cancel it.
  // The pinned host writes turn_completed after Stop returns. SessionEnd/idle notification
  // flush that turn without a service or requiring the next prompt.
  if (event.event === "SessionEnd")
    await new Promise((resolve) => setTimeout(resolve, 150));
  await exportCompleted(statePath, event, config);
}
async function exportCompleted(
  statePath: string,
  event: GrokEvent,
  config: ResolvedConfig,
): Promise<void> {
  {
    const state = await readJson<State>(statePath, true, 1024 * 1024);
    if (!state || !config.enabled) return;
    const rows: unknown[] = [];
    let reads = 0;
    for (const [sid, path] of Object.entries(state.paths).slice(0, 32)) {
      const home = dirname(dirname(config.paths.user));
      const cursor = new TranscriptCursor(sid, home);
      // Bound total source reads per hook (8 MiB), with partial-line/truncation detection.
      while (reads++ < 8 && rows.length < 10000) {
        await cursor
          .read(path, (row) => {
            if (rows.length < 10000) rows.push(row);
          })
          .catch(() => {
            cursor.gap = true;
          });
        if (cursor.lastReadBytes < 1024 * 1024 || cursor.gap) break;
      }
      state.gap ||= cursor.gap || reads > 8 || rows.length >= 10000;
    }
    const completed = new Set(
      rows
        .filter(
          (row) =>
            record(record(record(row).params).update).sessionUpdate ===
            "turn_completed",
        )
        .map((row) => record(record(record(row).params).update).prompt_id),
    );
    const prompts = state.events.filter(
      (e) =>
        e.event === "UserPromptSubmit" &&
        e.sessionId === event.sessionId &&
        e.promptId &&
        !state.claimed.includes(e.promptId),
    );
    const exportedEvents = new Set<string>();
    for (const prompt of prompts) {
      const turnId = prompt.promptId!;
      if (!completed.has(turnId) && event.event !== "SessionEnd") continue;
      // Load the telemetry SDK only when a finalized turn is ready, never on every tool hook.
      const { collectionEnabled, createTurnTracer } = await import(
        "@traceai/agent-core"
      );
      if (!collectionEnabled(config)) return;
      let acquired = false;
      // Atomic per-turn claim avoids duplicates across simultaneous SessionEnd and shutdown Stop.
      // Claim before work/send: a crash may lose telemetry, but never replay an ambiguous export.
      await updateJson<State>(statePath, (old) => {
        acquired = false;
        if (!old || old.claimed.includes(turnId)) return old ?? state;
        acquired = true;
        return { ...old, claimed: [...old.claimed, turnId].slice(-128) };
      });
      if (!acquired) continue;
      const next = state.events.find(
        (e) =>
          e.event === "UserPromptSubmit" &&
          e.sessionId === prompt.sessionId &&
          e.time > prompt.time,
      );
      const events = state.events.filter(
        (e) => e.time >= prompt.time && (!next || e.time < next.time),
      );
      const tracer = createTurnTracer({ config }),
        observer = new GrokObserver(tracer, config);
      const inputs = new Map<string, string>(),
        outputs = new Map<string, string>();
      let currentPrompt: string | undefined;
      for (const row of rows) {
        const p = record(record(row).params),
          u = record(p.update),
          m = record(p._meta);
        if (
          u.sessionUpdate === "hook_execution" &&
          u.event_name === "user_prompt_submit"
        )
          currentPrompt = u.prompt_id;
        if (u.sessionUpdate === "user_message_chunk" && currentPrompt) {
          const v = text(record(u.content).text, config);
          if (v !== undefined)
            inputs.set(currentPrompt, (inputs.get(currentPrompt) ?? "") + v);
        }
        if (u.toolCallId) {
          const k = `${p.sessionId}:${u.toolCallId}`;
          if (u.rawInput !== undefined) {
            const v = text(u.rawInput, config);
            if (v !== undefined) inputs.set(k, v);
          }
          if (u.rawOutput !== undefined || u.content !== undefined) {
            const v = text(
              u.rawOutput ?? u.content,
              config,
              true,
              u.status === "failed" || failedResult(u.rawOutput ?? u.content),
            );
            if (v !== undefined) outputs.set(k, v);
          }
        }
        if (u.sessionUpdate === "agent_message_chunk" && m.promptId) {
          const v = text(record(u.content).text, config, true);
          if (v !== undefined)
            outputs.set(
              m.promptId,
              ((outputs.get(m.promptId) ?? "") + v).slice(0, 20000),
            );
        }
      }
      const deferred = new Map<string, GrokEvent[]>();
      const replay = (e: GrokEvent): void => {
        if (e.sessionId !== event.sessionId && !observer.active.has(e.sessionId)) {
          deferred.set(e.sessionId, [...(deferred.get(e.sessionId) ?? []), e]);
          return;
        }
        observer.handle({
          ...e,
          input:
            e.event === "UserPromptSubmit"
              ? inputs.get(e.promptId!)
              : e.toolId
                ? inputs.get(`${e.sessionId}:${e.toolId}`)
                : undefined,
          output: e.toolId
            ? outputs.get(`${e.sessionId}:${e.toolId}`)
            : e.promptId
              ? outputs.get(e.promptId)
              : undefined,
        });
        if (e.event === "SubagentStart" && e.subagentId) {
          const waiting = deferred.get(e.subagentId) ?? [];
          deferred.delete(e.subagentId);
          for (const child of waiting) replay(child);
        }
      };
      for (const e of events) replay(e);
      for (const row of rows) {
        const p = record(record(row).params),
          u = record(p.update),
          m = record(p._meta);
        if (
          u.sessionUpdate === "tool_call_update" &&
          ["failed", "completed"].includes(u.status) &&
          m.promptId &&
          observer.active.get(p.sessionId)?.promptId === m.promptId
        ) {
          const pre = events.find(
            (e) => e.sessionId === p.sessionId && e.toolId === u.toolCallId,
          );
          if (pre)
            observer.handle({
              ...pre,
              event:
                u.status === "failed" ? "PostToolUseFailure" : "PostToolUse",
              time: Number.isSafeInteger(m.agentTimestampMs)
                ? m.agentTimestampMs
                : pre.time,
              outcome: u.status === "failed" || failedResult(u.rawOutput ?? u.content) ? "error" : pre.outcome,
              output: outputs.get(`${p.sessionId}:${u.toolCallId}`),
            });
        }
        observer.transcript(row);
      }
      // End children before the user root, using source terminal boundaries, never silence.
      for (const [sid, active] of observer.active)
        if (sid !== event.sessionId)
          observer.finish(
            sid,
            active.terminal ?? {
              ...event,
              sessionId: sid,
              outcome: "incomplete",
            },
          );
      const active = observer.active.get(event.sessionId);
      if (active)
        observer.finish(
          event.sessionId,
          state.gap
            ? { ...(active.terminal ?? event), outcome: "incomplete" }
            : (active.terminal ?? { ...event, outcome: "incomplete" }),
        );
      await tracer.flush();
      await tracer.shutdown();
      for (const e of events) exportedEvents.add(JSON.stringify(e));
    }
    // Remove only the snapshot actually claimed/exported. Concurrent new prompts
    // and tool events must survive cleanup; deduplication lives in claimed IDs.
    await updateJson<State>(statePath, (old) => {
      if (!old) return state;
      old.events = old.events.filter(e => !exportedEvents.has(JSON.stringify(e)));
      if (!old.events.length) old.gap = false;
      return old;
    });
  }
}
