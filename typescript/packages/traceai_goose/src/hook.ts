#!/usr/bin/env node
import { promises as fs } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, join, isAbsolute } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { lock } from "proper-lockfile";
import {
  contentHash,
  ensurePrivateDirectory,
  readJson,
  updateJson,
  writeProtectedUnderLock,
} from "@traceai/agent-core/files";
import {
  deliveryStatePath,
  resolveConfig,
  type ResolvedConfig,
} from "@traceai/agent-core/config";
import { collectionEnabled } from "@traceai/agent-core/control";
import { EVENTS, nativeConflict } from "./paths.js";
import { registeredFor } from "./registration.js";
import {
  exportTurn,
  hasTerminalResponse,
  messageIndex,
  messageKey,
  persistedPrompt,
  openTranscript,
  readRows,
  validSession,
  type Row,
  type SessionState,
} from "./transcript.js";

const STATE_LIMIT = 1024 * 1024;
// Leave time for export inside the bundled hook's eight-second capture budget.
const TERMINAL_WAIT_MS = 6500;
// Only called while holding this session's recoverable .hook-lock. Keep one
// lease per state write; legacy O_EXCL .lock claims are intentionally inert.
const saveState = (file: string, state: SessionState) =>
  writeProtectedUnderLock(
    file,
    JSON.stringify(state, null, 2) + "\n",
    undefined,
    STATE_LIMIT,
  );

interface Hook {
  event?: string;
  session_id?: string;
  message?: string;
  working_dir?: string;
  tool_call_id?: string;
  reason?: string;
}
const policy = (c: ResolvedConfig) =>
  contentHash(
    JSON.stringify([
      c.endpoint,
      c.projectName,
      c.profile,
      c.hideInputs,
      c.hideOutputs,
      c.hideErrorDetails,
    ]),
  );
async function diagnostic(c: ResolvedConfig, code: string) {
  await updateJson<Record<string, unknown>>(
    deliveryStatePath(c),
    (old) => ({
      pending: 0,
      exported: 0,
      dropped: 0,
      ...old,
      ...(c.deliveryBinding ? { binding: c.deliveryBinding } : {}),
      activatedAt: old?.activatedAt ?? new Date().toISOString(),
      lastError: code,
    }),
  ).catch(() => {});
}
export async function handleHook(event: Hook): Promise<void> {
  if (
    !event ||
    typeof event !== "object" ||
    !EVENTS.includes(event.event as (typeof EVENTS)[number]) ||
    !validSession(event.session_id) ||
    (event.working_dir !== undefined &&
      (typeof event.working_dir !== "string" ||
        !isAbsolute(event.working_dir))) ||
    (event.message !== undefined && typeof event.message !== "string")
  ) {
    await diagnostic(
      await resolveConfig({ agent: "goose" }),
      "UNQUALIFIED_HOOK_SCHEMA",
    );
    return;
  }
  // Find the protected session cursor before resolving a project. Goose's
  // SessionEnd omits working_dir, including on cross-directory resume.
  let config = await resolveConfig({ agent: "goose" });
  const dir = join(dirname(config.paths.user), "futureagi-sessions");
  await ensurePrivateDirectory(dir);
  const file = join(dir, contentHash(event.session_id) + ".json");
  // Metadata only, 512 session files and a 24-hour age bound. No copied conversation content.
  const files = (await fs.readdir(dir)).filter((n) =>
    /^[a-f0-9]{64}\.json$/.test(n),
  );
  if (event.event === "UserPromptSubmit") {
    let retained = files.length;
    for (const name of files) {
      const path = join(dir, name),
        st = await fs.lstat(path);
      if (
        st.isFile() &&
        !st.isSymbolicLink() &&
        st.nlink === 1 &&
        st.mtimeMs < Date.now() - 86400000
      ) {
        try {
          await fs.unlink(path);
          retained--;
        } catch {
          /* concurrent cleanup */
        }
      }
    }
    if (
      retained >= 512 &&
      !files.includes(contentHash(event.session_id) + ".json")
    ) {
      await diagnostic(config, "STATE_LIMIT");
      return;
    }
  }
  let release: (() => Promise<void>) | undefined;
  try {
    release = await lock(file, {
      realpath: false,
      lockfilePath: file + ".hook-lock",
      stale: 10000,
      update: 5000,
      retries: { retries: 50, factor: 1, minTimeout: 40, maxTimeout: 40 },
    });
  } catch {
    await diagnostic(config, "HOOK_CONTENTION");
    return;
  }
  try {
    let state = await readJson<SessionState>(file, true, STATE_LIMIT);
    if (state?.schemaVersion !== 2) state = undefined; // Never reuse an old row-ID cursor.
    const projectDir = await fs.realpath(
      event.working_dir || state?.projectDir || process.cwd(),
    );
    config = await resolveConfig({ agent: "goose", projectDir });
    // Re-read approval, scope, privacy and disable fences on every hook.
    if (!(await registeredFor(config))) return;
    if (!collectionEnabled(config)) {
      if (state) await saveState(file, { ...state, pending: false, tools: {} });
      return;
    }
    if (await nativeConflict()) {
      await diagnostic(config, "NATIVE_TRACING_CONFLICT");
      return;
    }
    if (
      state &&
      (state.generation !== config.generation ||
        state.policy !== policy(config))
    ) {
      state = { ...state, pending: false, tools: {} };
      await saveState(file, state);
    }
    if (event.event === "UserPromptSubmit") {
      const db = await openTranscript();
      let baseline: string[],
        previousRows: Row[] = [];
      try {
        db.exec("BEGIN");
        const index = messageIndex(db, event.session_id);
        // Default loop emits before persistence; the state-machine path emits after it.
        // Only a new tail user message is eligible, never a historical text match.
        const prompt = persistedPrompt(
          db,
          event.session_id,
          index,
          state?.observed ?? state?.completed ?? [],
          event.message,
        );
        baseline = index
          .filter((r) => r.key !== (prompt && messageKey(prompt)))
          .map((r) => r.key);
        if (state?.pending)
          previousRows = readRows(
            db,
            event.session_id,
            state.baseline,
            index,
          ).filter((r) => !prompt || messageKey(r) !== messageKey(prompt));
      } finally {
        db.close();
      }
      const previous = state;
      state = {
        schemaVersion: 2,
        baseline,
        completed: baseline,
        turnId: randomUUID(),
        promptHash:
          typeof event.message === "string"
            ? contentHash(event.message)
            : undefined,
        pending: true,
        projectDir,
        start: Date.now(),
        generation: config.generation,
        policy: policy(config),
        tools: {},
      };
      // Commit the new boundary before delivery; ambiguous delivery is never retried.
      await saveState(file, state);
      if (previous?.pending)
        await exportTurn(config, event.session_id, previous, previousRows);
      return;
    }
    if (!state?.pending) return;
    if (
      ["PreToolUse", "PostToolUse", "PostToolUseFailure"].includes(event.event!)
    ) {
      if (
        typeof event.tool_call_id !== "string" ||
        event.tool_call_id.length > 512 ||
        Object.keys(state.tools).length >= 128
      )
        return;
      // Work after a rejected Stop invalidates its candidate completion time.
      state.stopped = false;
      state.stoppedAt = undefined;
      const id = contentHash(event.tool_call_id),
        before = state.tools[id];
      state.tools[id] =
        event.event === "PreToolUse"
          ? (before ?? { start: Date.now() })
          : {
              start: before?.start ?? Date.now(),
              end: Date.now(),
              error: event.event === "PostToolUseFailure",
            };
      await saveState(file, state);
      return;
    }
    if (event.event === "Stop") {
      // Stop is a blocking decision channel. A later policy may reject it, so retain
      // the turn until SessionEnd or the next user prompt confirms a boundary.
      const db = await openTranscript();
      let observed: string[];
      try {
        observed = messageIndex(db, event.session_id).map((r) => r.key);
      } finally {
        db.close();
      }
      const unchanged =
        state.stopped &&
        JSON.stringify(state.observed) === JSON.stringify(observed);
      await saveState(file, {
        ...state,
        stopped: true,
        observed,
        stoppedAt: unchanged ? (state.stoppedAt ?? Date.now()) : Date.now(),
      });
      return;
    }
    if (event.event === "SessionEnd") {
      const interrupted = event.reason === "user_cancel";
      const deadline = Date.now() + TERMINAL_WAIT_MS;
      let rows: Row[], completed: string[], ready: boolean;
      do {
        // Release the read transaction before waiting so the host can commit
        // late responses. Every iteration sees a fresh, consistent snapshot.
        const db = await openTranscript();
        try {
          db.exec("BEGIN");
          const index = messageIndex(db, event.session_id);
          rows = readRows(db, event.session_id, state.baseline, index);
          completed = index.map((r) => r.key);
        } finally {
          db.close();
        }
        ready = hasTerminalResponse(rows);
        if (interrupted || !state.stopped || ready || Date.now() >= deadline)
          break;
        await delay(Math.min(50, Math.max(0, deadline - Date.now())));
      } while (true);
      // SessionEnd confirms the records committed after Stop, not extra model
      // work. Keep Stop's timestamp so persistence wait and user idle time are
      // never counted as turn duration. Any intervening tool hook clears Stop.
      if (state.stopped && ready && !interrupted)
        state = { ...state, observed: completed };
      state = { ...state, interrupted };
      // The wait must not bypass a disable, privacy change or removed registration.
      config = await resolveConfig({ agent: "goose", projectDir });
      await saveState(file, {
        ...state,
        pending: false,
        completed,
        observed: completed,
        tools: {},
      });
      if (
        !collectionEnabled(config) ||
        !(await registeredFor(config)) ||
        state.generation !== config.generation ||
        state.policy !== policy(config)
      )
        return;
      await exportTurn(config, event.session_id, state, rows);
    }
  } catch (e) {
    const message =
      e instanceof Error && /^[A-Z][A-Z0-9_]{0,60}$/.test(e.message)
        ? e.message
        : "CAPTURE_FAILED";
    await diagnostic(config, message);
  } finally {
    await release?.().catch(() => {});
  }
}
async function main() {
  if (process.argv[2]) {
    if (!isAbsolute(process.argv[2])) return;
    process.env.FI_AGENT_CONFIG_DIR = process.argv[2];
  }
  // Never emit stdout, stderr or a nonzero exit into the host's decision channel.
  const deadline = setTimeout(() => process.exit(0), 8000);
  let size = 0;
  const buffers: Buffer[] = [];
  try {
    for await (const chunk of process.stdin) {
      const b = Buffer.from(chunk);
      size += b.length;
      if (size > 1024 * 1024) return;
      buffers.push(b);
    }
    await handleHook(JSON.parse(Buffer.concat(buffers).toString("utf8")));
  } catch {
    /* unavailable config/transcript never blocks Goose */
  } finally {
    clearTimeout(deadline);
  }
}
if (require.main === module)
  void main().then(
    () => {
      process.exitCode = 0;
    },
    () => {
      process.exitCode = 0;
    },
  );
