#!/usr/bin/env node
import { promises as fs } from "node:fs";
import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import { join } from "node:path";
import {
  deliveryStatePath,
  resolveConfig,
  type ResolvedConfig,
} from "@traceai/agent-core/config";
import { collectionEnabled } from "@traceai/agent-core/collection";
import {
  contentHash,
  ensurePrivateDirectory,
  readJson,
  updateJson,
} from "@traceai/agent-core/files";
import {
  EVENTS,
  exportTranscriptTurn,
  readPendingTranscript,
  transcriptCheckpoint,
  isHumanPrompt,
  validId,
  type SessionState,
} from "./transcript.js";

interface HookEvent {
  hook_event_name?: string;
  session_id?: string;
  prompt_id?: string;
  transcript_path?: string;
  cwd?: string;
  tool_use_id?: string;
  tool_response?: { agentId?: unknown };
  agent_id?: string;
  agent_transcript_path?: string;
  last_assistant_message?: string;
  stop_hook_active?: boolean;
  reason?: string;
  background_tasks?: unknown[];
}
const empty = (): SessionState => ({
  schemaVersion: 1,
  offset: 0,
  tools: {},
  compactions: [],
  subagents: [],
  completed: [],
});
async function diagnostic(config: ResolvedConfig, code: string): Promise<void> {
  // Bounded metadata only. Never write errors, payloads, prompts or transcript text to diagnostics.
  await updateJson(join(config.paths.stateDir, "claude-hook.json"), () => ({
    code,
    at: new Date().toISOString(),
  })).catch(() => {});
  if (
    !["TURN_EXPORTED", "TURN_CHECKPOINTED", "EXPORT_DROPPED"].includes(code)
  ) {
    await updateJson<Record<string, unknown>>(
      deliveryStatePath(config),
      (old) => ({
        pending: 0,
        exported: 0,
        dropped: 0,
        ...old,
        lastError: `CAPTURE_${code}`,
      }),
    ).catch(() => {});
  }
}
export async function handleHook(event: HookEvent): Promise<void> {
  if (
    !EVENTS.includes(event.hook_event_name as (typeof EVENTS)[number]) ||
    !validId(event.session_id) ||
    typeof event.cwd !== "string"
  )
    return;
  const config = await resolveConfig({
    agent: "claude",
    projectDir: event.cwd,
  });
  if (
    [
      "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
      "ENABLE_ENHANCED_TELEMETRY_BETA",
      "ENABLE_BETA_TRACING_DETAILED",
    ].some((k) => ["1", "true"].includes(process.env[k] ?? ""))
  ) {
    await diagnostic(config, "NATIVE_TRACING_CONFLICT");
    return;
  }
  const directory = join(config.paths.configDir, "claude-sessions");
  await ensurePrivateDirectory(directory);
  // State contains no content and is bounded to 512 conversations / 24 hours.
  // Only our regular metadata files are eligible; never follow a link during cleanup.
  const entries = (await fs.readdir(directory)).filter((name) =>
    /^[a-f0-9]{64}\.json$/.test(name),
  );
  if (event.hook_event_name === "SessionStart") {
    const files = await Promise.all(
      entries.slice(0, 1024).map(async (name) => ({
        name,
        stat: await fs.lstat(join(directory, name)),
      })),
    );
    files.sort((a, b) => b.stat.mtimeMs - a.stat.mtimeMs);
    for (const [index, item] of files.entries())
      if (
        item.stat.isFile() &&
        !item.stat.isSymbolicLink() &&
        item.stat.nlink === 1 &&
        (!process.getuid || item.stat.uid === process.getuid()) &&
        (index >= 511 || item.stat.mtimeMs < Date.now() - 86400000)
      )
        await fs.unlink(join(directory, item.name)).catch(() => {});
  } else if (
    entries.length >= 512 &&
    !entries.includes(contentHash(event.session_id) + ".json")
  ) {
    await diagnostic(config, "STATE_LIMIT");
    return;
  }
  const file = join(directory, contentHash(event.session_id) + ".json");
  let lease: Awaited<ReturnType<typeof acquireWriteLock>>;
  const terminal = ["Stop", "StopFailure", "SessionEnd"].includes(
    event.hook_event_name!,
  );
  try {
    // Separate event and atomic-write leases; both use core's no-follow heartbeat.
    lease = await acquireWriteLock(file + ".event", {
      lockWaitMs: terminal ? 5000 : 3000,
    });
  } catch {
    await diagnostic(config, "HOOK_CONTENTION");
    return;
  }
  try {
    let state = (await readJson<SessionState>(file)) ?? empty();
    if (state.schemaVersion !== 1) return;
    const enabled = collectionEnabled(config),
      name = event.hook_event_name;
    const checkpoint = async (promptId?: string): Promise<number> => {
      if (typeof event.transcript_path !== "string")
        throw new Error("TRANSCRIPT_PATH_REQUIRED");
      try {
        return await transcriptCheckpoint(
          event.transcript_path,
          config,
          promptId,
        );
      } catch (error) {
        // New sessions can send their first hook before creating the transcript.
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return 0;
        throw error;
      }
    };
    const complete = (id: string) => {
      state.completed = [...new Set([...state.completed, id])].slice(-128);
    };
    const captureBinding = contentHash(
      JSON.stringify([
        config.endpoint,
        config.projectName,
        config.profile,
        config.generation,
      ]),
    );
    if (state.promptId && state.captureBinding !== captureBinding) {
      // A fresh resolver cannot retroactively authorize a pending old turn.
      // Fence source/history across setup, destination changes and legacy state.
      for (const id of [state.promptId, ...(state.pendingPrompts ?? [])])
        complete(id);
      const boundary = await checkpoint();
      state = {
        ...empty(),
        completed: state.completed,
        offset: boundary,
        contentFloor: boundary,
        privacyPending: true,
        ended: true,
        captureBinding,
      };
      await diagnostic(config, "PENDING_TURN_FENCED");
      await lease.assertHeld();
      await updateJson(file, () => state);
      if (name !== "UserPromptSubmit") return;
    }
    state.captureBinding = captureBinding;
    state.childCursors ??= {};
    state.agentParents ??= {};
    if (
      name === "PostToolUse" &&
      validId(event.tool_use_id) &&
      validId(event.tool_response?.agentId) &&
      Object.keys(state.agentParents).length < 128
    ) {
      state.agentParents[event.tool_response.agentId] = {
        toolId: event.tool_use_id,
        ...(validId(event.agent_id) ? { parentAgentId: event.agent_id } : {}),
      };
    }
    const privateNow =
      !config.captureContent || config.hideInputs || config.hideOutputs;
    if (name === "SubagentStart" && validId(event.agent_id)) {
      if (
        !state.childCursors[event.agent_id] &&
        Object.keys(state.childCursors).length < 128
      )
        state.childCursors[event.agent_id] = {
          offset: 0,
          // Resolve from the delegated source prompt, not hook execution time.
          contentFloor: -1,
        };
    }
    if (
      name === "SubagentStop" &&
      validId(event.agent_id) &&
      typeof event.agent_transcript_path === "string"
    ) {
      const cursor = state.childCursors[event.agent_id];
      if (cursor) cursor.path = event.agent_transcript_path;
      else if (Object.keys(state.childCursors).length < 128) {
        state.gap ??= "CHILD_CAPTURE_BOUNDARY_UNKNOWN";
        const boundary = await transcriptCheckpoint(
          event.agent_transcript_path,
          config,
        );
        state.childCursors[event.agent_id] = {
          path: event.agent_transcript_path,
          offset: boundary,
          contentFloor: boundary,
        };
      }
    }
    // Re-snapshot known children at privacy transitions too: background writes
    // may have completed after the last private terminal hook.
    if (!enabled || privateNow || state.privacyPending) {
      for (const cursor of Object.values(state.childCursors)) {
        if (!cursor.path) continue;
        try {
          cursor.contentFloor = await transcriptCheckpoint(cursor.path, config);
          if (!enabled) cursor.offset = cursor.contentFloor;
        } catch {
          cursor.contentFloor = Number.MAX_SAFE_INTEGER;
        }
      }
    }
    if (!enabled || privateNow) {
      state.contentFloor = await checkpoint();
      state.privacyPending = true;
      state.privateTurn = true;
    }
    if (!enabled) {
      // A persistent exclusion boundary also fences the backwards history read after re-enabling.
      if (validId(event.prompt_id)) complete(event.prompt_id);
      if (state.promptId) complete(state.promptId);
      state.ended = true;
      state.tools = {};
      state.subagents = [];
      state.compactions = [];
      state.checkpoint = [];
      state.offset = state.contentFloor!;
      await updateJson(file, () => state);
      return;
    }
    if (name === "SessionStart") {
      if (!state.promptId) {
        state.offset = await checkpoint();
        state.contentFloor ??= state.offset;
      }
      await updateJson(file, () => state);
      return;
    }
    async function exportTurn(final: boolean): Promise<void> {
      if (
        !state.promptId ||
        state.ended ||
        typeof event.transcript_path !== "string"
      )
        return;
      try {
        const source = await readPendingTranscript(
          event.transcript_path,
          config,
          state.offset,
        );
        const candidates = [
          ...new Set([state.promptId, ...(state.pendingPrompts ?? [])]),
        ];
        const targets = candidates.filter((id) => {
          if (state.completed.includes(id)) return false;
          const row = source.rows.find(
            (row) => row.promptId === id && row.type === "user",
          );
          return row ? isHumanPrompt(row) : id === state.promptId;
        });
        for (const [index, promptId] of targets.entries()) {
          const last = index === targets.length - 1;
          const finalize = final || !last;
          const from = source.rows.findIndex(
            (row) => row.type === "user" && row.promptId === promptId,
          );
          const scoped = source.rows.slice(Math.max(from, 0));
          const next = scoped.findIndex(
            (row, i) =>
              i > 0 && isHumanPrompt(row) && row.promptId !== promptId,
          );
          if (next >= 0) scoped.splice(next);
          // Terminal hooks can belong to a system task notification, but never
          // to a different human prompt. Keep evidence scoped to source rows.
          const terminalId =
            [...scoped]
              .reverse()
              .find((row) => row.promptId && state.terminals?.[row.promptId])
              ?.promptId ?? promptId;
          const evidence = state.terminals?.[terminalId];
          const legacy =
            !state.terminals &&
            (state.terminalPromptId === promptId || candidates.length === 1);
          const turnState = {
            ...state,
            promptId,
            checkpoint:
              promptId === state.promptId ? [...(state.checkpoint ?? [])] : [],
            start: promptId === state.promptId ? state.start : undefined,
            terminalTextHash:
              evidence?.textHash ??
              (legacy ? state.terminalTextHash : undefined),
            terminalPromptId: evidence
              ? terminalId
              : legacy
                ? state.terminalPromptId
                : undefined,
            stopTime: evidence?.time ?? (legacy ? state.stopTime : undefined),
            gap:
              state.gap === "BACKGROUND_WORK_PENDING" ? undefined : state.gap,
          };
          const result = await exportTranscriptTurn(
            config,
            turnState,
            event.transcript_path,
            event.session_id!,
            evidence?.outcome ??
              (legacy ? state.terminal : undefined) ??
              "incomplete",
            {
              final: finalize,
              async beforeExport(ids, children) {
                // Source offsets survive parent-turn resets. Per-turn subagent
                // snapshots remain fixed so repeated Stops recreate parent contexts.
                state.childCursors = { ...state.childCursors, ...children };
                await lease.assertHeld();
                state.promptId = promptId;
                state.checkpoint = ids;
                if (finalize) {
                  complete(promptId);
                  if (last) state.ended = true;
                }
                state.pendingPrompts = (state.pendingPrompts ?? []).filter(
                  (id) => id !== promptId && !state.completed.includes(id),
                );
                await updateJson(file, () => state);
              },
            },
          );
          if (last && finalize) state.offset = result.offset;
          await diagnostic(
            config,
            result.outcome === "incomplete"
              ? "INCOMPLETE_TURN"
              : result.dropped
                ? "EXPORT_DROPPED"
                : finalize
                  ? "TURN_EXPORTED"
                  : "TURN_CHECKPOINTED",
          );
        }
        state.ended =
          targets.length > 0 &&
          targets.every((id) => state.completed.includes(id));
      } catch (error) {
        const code =
          error instanceof Error && /^[A-Z_]{1,64}$/.test(error.message)
            ? error.message
            : "CAPTURE_FAILED";
        await diagnostic(config, code);
        if (
          code !== "TERMINAL_TRANSCRIPT_PENDING" &&
          ([
            "TRANSCRIPT_LIMIT",
            "TRANSCRIPT_TRUNCATED",
            "PROMPT_NOT_IN_TRANSCRIPT",
          ].includes(code) ||
            final)
        ) {
          // Lose only this turn, not the rest of the conversation. Exclude its history too.
          state.offset = await checkpoint();
          state.contentFloor = state.offset;
          state.privacyPending = true;
          complete(state.promptId!);
          state.ended = true;
        }
      }
    }
    if (name === "UserPromptSubmit") {
      if (
        !validId(event.prompt_id) ||
        state.completed.includes(event.prompt_id) ||
        (state.promptId === event.prompt_id && !state.ended)
      )
        return;
      const boundary = await checkpoint(event.prompt_id);
      if (state.promptId && !state.ended && state.stopped) {
        // Background notification hooks are not user boundaries. Source metadata wins.
        const source = await readPendingTranscript(
          event.transcript_path!,
          config,
          boundary,
        );
        const row = source.rows.find(
          (r) => r.type === "user" && r.promptId === event.prompt_id,
        );
        if (!row || isHumanPrompt(row)) await exportTurn(true);
      }
      if (state.promptId && !state.ended) {
        state.pendingPrompts = [
          ...new Set([...(state.pendingPrompts ?? []), event.prompt_id]),
        ].slice(-16);
      } else {
        state = {
          ...state,
          offset: boundary,
          contentFloor: state.privacyPending
            ? boundary
            : (state.contentFloor ?? boundary),
          privacyPending: privateNow,
          privateTurn: privateNow,
          promptId: event.prompt_id,
          start: Date.now(),
          ended: false,
          stopped: false,
          tools: {},
          agentParents: {},
          compactions: [],
          subagents: [],
          pendingPrompts: [],
          checkpoint: [],
          terminal: undefined,
          terminals: {},
          terminalTextHash: undefined,
          terminalPromptId: undefined,
          stopTime: undefined,
          gap: undefined,
        };
      }
    } else {
      if (
        !validId(state.promptId) ||
        state.ended ||
        state.completed.includes(state.promptId)
      )
        return;
      if (
        event.prompt_id &&
        event.prompt_id !== state.promptId &&
        !state.pendingPrompts?.includes(event.prompt_id)
      )
        return;
      if (["PreToolUse", "SubagentStart", "PreCompact"].includes(name!)) {
        if (state.terminals)
          delete state.terminals[event.prompt_id ?? state.promptId!];
        state.stopped = false;
        state.terminal = undefined;
        state.terminalTextHash = undefined;
        state.terminalPromptId = undefined;
        state.stopTime = undefined;
      }
      if (
        name === "PreToolUse" &&
        validId(event.tool_use_id) &&
        Object.keys(state.tools).length < 128
      )
        state.tools[event.tool_use_id] = { start: Date.now() };
      if (
        ["PostToolUse", "PostToolUseFailure"].includes(name!) &&
        validId(event.tool_use_id)
      ) {
        const timing = state.tools[event.tool_use_id];
        if (timing) {
          timing.end = Date.now();
          timing.failed = name === "PostToolUseFailure";
        }
      }
      if (
        name === "SubagentStop" &&
        validId(event.agent_id) &&
        typeof event.agent_transcript_path === "string" &&
        state.subagents.length < 16 &&
        !state.subagents.some((s) => s.id === event.agent_id)
      )
        state.subagents.push({
          id: event.agent_id,
          path: event.agent_transcript_path,
          offset: state.childCursors[event.agent_id]?.offset,
          contentFloor:
            (state.childCursors[event.agent_id]?.contentFloor ?? -1) >= 0
              ? state.childCursors[event.agent_id]?.contentFloor
              : undefined,
        });
      if (name === "PreCompact" && state.compactions.length < 32)
        state.compactions.push(Date.now());
      if (["Stop", "StopFailure", "SessionEnd"].includes(name!)) {
        if (name === "Stop" && event.background_tasks?.length) {
          state.gap = "BACKGROUND_WORK_PENDING";
        } else {
          if (name === "Stop") {
            state.stopped = true;
            state.terminal = "success";
            state.terminalTextHash =
              typeof event.last_assistant_message === "string"
                ? contentHash(event.last_assistant_message)
                : undefined;
            state.terminalPromptId = event.prompt_id;
            state.stopTime = Date.now();
          }
          if (name === "StopFailure") state.terminal = "error";
          if (name === "Stop" || name === "StopFailure") {
            const id = event.prompt_id ?? state.promptId!;
            state.terminals ??= {};
            state.terminals[id] = {
              outcome: name === "Stop" ? "success" : "error",
              textHash: name === "Stop" ? state.terminalTextHash : undefined,
              time: Date.now(),
            };
          }
          // Stop hooks cannot observe the decisions of other Stop hooks. Never publish a final
          // root until SessionEnd or the next human prompt; attempted Stops export new children.
          await exportTurn(name !== "Stop");
        }
      }
      if (state.ended) {
        state.tools = {};
        state.subagents = [];
        state.compactions = [];
      }
    }
    await lease.assertHeld();
    await updateJson(file, () => state);
  } finally {
    await lease.release().catch(() => {});
  }
}

async function main(): Promise<void> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 256 * 1024) return;
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  await handleHook(value);
}
// Neutral hook protocol: no stdout/stderr, no permission decisions, bounded fail-open process.
if (require.main === module) {
  const timer = setTimeout(() => process.exit(0), 9000);
  main()
    .catch(() => {})
    .finally(() => {
      clearTimeout(timer);
      process.exitCode = 0;
    });
}
