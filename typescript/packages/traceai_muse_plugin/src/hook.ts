import { deliveryStatePath } from "@traceai/agent-core/config";
import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import {
  createHash,
  createCipheriv,
  createDecipheriv,
  createHmac,
  randomBytes,
} from "node:crypto";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import {
  cumulativeDelta,
  controlPaths,
  collectionEnabled,
  createTurnTracer,
  ensurePrivateDirectory,
  readJson,
  resolveConfig,
  redact,
  updateJson,
  writeProtected,
  type ChildInput,
  type EndInput,
  type ResolvedConfig,
  type UsageInput,
  type DeliveryState,
} from "@traceai/agent-core";
import type { Binding } from "./index";

interface Child {
  start: Omit<ChildInput, "startTime"> & { startTime: number };
  end?: EndInput & { endTime: number };
}
interface Turn {
  version: 1;
  session: string;
  id: string;
  start: number;
  input?: string;
  children: Child[];
  cumulative?: UsageInput;
  usageStep?: number;
  usageDiscontinuous?: boolean;
  closed?: boolean;
  generation: number;
  expires: number;
  contentBytes: number;
}
interface Sealed {
  version: 1;
  iv: string;
  tag: string;
  ciphertext: string;
}
export type HookEvent = Record<string, unknown> & {
  hook_event_name: string;
  session_id: string;
  turn_id?: string;
};
const safeId = (v: unknown): v is string =>
  typeof v === "string" && /^[a-zA-Z0-9_.:-]{1,256}$/.test(v);
const safeLabel = (v: unknown): string | undefined =>
  typeof v === "string" && /^[a-zA-Z0-9_.:/-]{1,128}$/.test(v) ? v : undefined;
const hash = (key: Buffer, value: string): string =>
  createHmac("sha256", key).update(value).digest("hex");
export function seal(value: Turn, key: Buffer, aad: string): Sealed {
  const iv = randomBytes(12),
    cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(aad));
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(value)),
    cipher.final(),
  ]);
  return {
    version: 1,
    iv: iv.toString("hex"),
    tag: cipher.getAuthTag().toString("hex"),
    ciphertext: ciphertext.toString("base64"),
  };
}
export function unseal(value: Sealed, key: Buffer, aad: string): Turn {
  if (value.version !== 1) throw new Error("STATE_VERSION");
  const cipher = createDecipheriv(
    "aes-256-gcm",
    key,
    Buffer.from(value.iv, "hex"),
  );
  cipher.setAAD(Buffer.from(aad));
  cipher.setAuthTag(Buffer.from(value.tag, "hex"));
  return JSON.parse(
    Buffer.concat([
      cipher.update(Buffer.from(value.ciphertext, "base64")),
      cipher.final(),
    ]).toString(),
  );
}
export function usage(value: unknown): UsageInput | undefined {
  if (!value || typeof value !== "object") return;
  const raw = value as Record<string, unknown>;
  const count = (key: string) =>
    typeof raw[key] === "number" &&
    Number.isSafeInteger(raw[key]) &&
    (raw[key] as number) >= 0
      ? (raw[key] as number)
      : undefined;
  const input = count("input_tokens"),
    output = count("output_tokens");
  if (input === undefined && output === undefined) return;
  // Do not guess cache/reasoning inclusions or distribute turn totals across attempts.
  return { input, output, cacheInInput: true, reasoningInOutput: true };
}
/** Muse 1.4.14's PostLLMCall counters are cumulative within a turn, NOT per request.
 * Only a contiguous sequence of first-attempt successes has additive request deltas.
 * Retries/missing events retain a nonadditive turn summary rather than inventing ownership.
 */
export function requestUsage(
  turn: Pick<Turn, "cumulative" | "usageStep" | "usageDiscontinuous">,
  event: HookEvent,
): UsageInput | undefined {
  const next = usage(event.usage),
    step = event.step;
  const continuous =
    !turn.usageDiscontinuous &&
    event.attempt === 1 &&
    ["success", "tool_calls"].includes(String(event.status)) &&
    Number.isSafeInteger(step) &&
    (step as number) >= 0 &&
    (turn.usageStep === undefined ? step === 0 : step === turn.usageStep + 1);
  const previous = turn.cumulative;
  turn.cumulative = next ?? previous;
  turn.usageStep = typeof step === "number" ? step : undefined;
  try {
    if (!next || next.input === undefined || next.output === undefined)
      throw new Error("UNKNOWN_USAGE");
    return {
      input: cumulativeDelta(previous?.input ?? 0, next.input, continuous),
      output: cumulativeDelta(previous?.output ?? 0, next.output, continuous),
      cacheInInput: true,
      reasoningInOutput: true,
    };
  } catch {
    turn.usageDiscontinuous = true;
    return undefined;
  }
}
async function gap(config: ResolvedConfig, code: string): Promise<void> {
  await updateJson<{ lastGap: string; count: number }>(
    join(config.paths.stateDir, "muse-gaps.json"),
    (old) => ({ lastGap: code, count: (old?.count ?? 0) + 1 }),
  );
}
function text(
  value: unknown,
  config: ResolvedConfig,
  turn: Turn,
  output = false,
  error = false,
): string | undefined {
  if (
    !config.captureContent ||
    (output ? config.hideOutputs : config.hideInputs) ||
    (error && config.hideErrorDetails) ||
    value === undefined
  )
    return;
  let s = typeof value === "string" ? value : JSON.stringify(value);
  if (!s) return;
  s = s.slice(0, 20000);
  for (const key of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (key) s = s.split(key).join("[REDACTED]");
  // Bound aggregate content before persistence, not only at export.
  s = redact(s);
  const remaining = Math.max(0, 24000 - turn.contentBytes);
  while (Buffer.byteLength(s) > remaining)
    s = s.slice(0, Math.floor(s.length / 2));
  turn.contentBytes += Buffer.byteLength(s);
  return s || undefined;
}

/** Observational only. Callers never forward this function's output or errors to the model. */
export async function consume(
  event: HookEvent,
  config: ResolvedConfig,
  now = Date.now(),
): Promise<void> {
  if (
    !collectionEnabled(config) ||
    !config.credentials ||
    !config.destinationApproved ||
    !config.identityKey
  )
    return;
  if (!safeId(event.session_id)) return;
  const directory = join(config.paths.stateDir, "muse-turns");
  await ensurePrivateDirectory(directory);
  // Fixed TTL + bounded file count. Content is encrypted; no raw hook journal/transcript is persisted.
  const names = await fs.readdir(directory);
  for (const name of names.slice(0, 256)) {
    if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
    const path = join(directory, name),
      st = await fs.lstat(path);
    if (st.isFile() && now - st.mtimeMs > 86400000) {
      await fs.unlink(path);
      names.splice(names.indexOf(name), 1);
      await gap(config, "EXPIRED_INCOMPLETE_TURN");
    }
  }
  await updateJson<DeliveryState>(
    deliveryStatePath(config),
    (old) => ({
      pending: 0,
      exported: 0,
      dropped: 0,
      ...old,
      activatedAt: new Date(now).toISOString(),
    }),
  );
  if (event.hook_event_name === "SessionStart") {
    await gap(config, "MUSE_PREVIEW_ONLY_AND_HOST_OVERSIZE_HOOKS_UNOBSERVABLE");
    return;
  }
  if (!safeId(event.turn_id)) {
    return;
  }
  const key = Buffer.from(config.identityKey, "hex");
  const aad = JSON.stringify([
    config.endpoint,
    config.projectName,
    event.session_id,
    event.turn_id,
    config.generation,
  ]);
  const identity = hash(key, aad);
  const file = join(directory, `${identity}.json`);
  const dedupFile = join(config.paths.stateDir, "muse-finalized.json");
  const finalized = await readJson<Record<string, number>>(dedupFile);
  if ((finalized?.[identity] ?? 0) > now) {
    if (!["Stop", "StopFailure"].includes(event.hook_event_name))
      await gap(config, "EVENT_AFTER_FINALIZED_STOP");
    return;
  }
  const lock = await acquireWriteLock(`${file}.event`, { lockWaitMs: 1500 }).catch(() => undefined);
  if (!lock) {
    await gap(config, "CONCURRENT_HOOK_DROPPED");
    return;
  }
  try {
    const saved = await readJson<Sealed>(file);
    let turn: Turn | undefined = saved ? unseal(saved, key, aad) : undefined;
    if (turn?.closed) {
      if (!["Stop", "StopFailure"].includes(event.hook_event_name))
        await gap(config, "EVENT_AFTER_FINALIZED_STOP");
      return;
    }
    if (!turn) {
      if (event.hook_event_name !== "UserPromptSubmit") {
        await gap(config, "MISSING_PROMPT_OR_CHILD_CONTEXT");
        return;
      }
      if (names.length >= 128) {
        await gap(config, "STATE_CAP_REACHED");
        return;
      }
      turn = {
        version: 1,
        session: event.session_id,
        id: event.turn_id,
        start: now,
        children: [],
        generation: config.generation,
        expires: now + 86400000,
        contentBytes: 0,
      };
      turn.input = text(event.prompt, config, turn);
    }
    if (turn.expires < now || turn.generation !== config.generation) {
      await gap(config, "STALE_TURN");
      return;
    }
    const name = event.hook_event_name;
    let id: string | undefined,
      kind: ChildInput["kind"] | undefined,
      isStart = false,
      isEnd = false;
    if (name === "PostLLMCall" && event.response_id === "muse-tui-echo") {
      // The echo provider emits the same lifecycle hooks but never calls an LLM.
      turn.children = turn.children.filter(
        (x) =>
          x.start.id !== `request:${event.request_id}:attempt:${event.attempt}`,
      );
    } else if (name === "PreLLMCall" || name === "PostLLMCall") {
      if (!safeId(event.request_id) || !Number.isSafeInteger(event.attempt)) {
        await gap(config, "MISSING_REQUEST_ID_OR_ATTEMPT");
        return;
      }
      id = `request:${event.request_id}:attempt:${event.attempt}`;
      kind = "llm";
      isStart = name === "PreLLMCall";
      isEnd = !isStart;
    } else if (
      ["PreToolUse", "PostToolUse", "PostToolUseFailure"].includes(name)
    ) {
      if (!safeId(event.tool_use_id)) {
        await gap(config, "MISSING_TOOL_ID");
        return;
      }
      id = `tool:${event.tool_use_id}`;
      kind = "tool";
      isStart = name === "PreToolUse";
      isEnd = !isStart;
    } else if (["SubagentStart", "SubagentStop"].includes(name)) {
      if (!safeId(event.subagent_id)) return;
      id = `agent:${event.subagent_id}`;
      kind = "agent";
      isStart = name === "SubagentStart";
      isEnd = !isStart;
      await gap(config, "CHILD_SESSION_CONTENT_NOT_QUALIFIED");
    } else if (["PreCompact", "PostCompact"].includes(name)) {
      kind = "compaction";
      isStart = name === "PreCompact";
      isEnd = !isStart;
      id = isStart
        ? `compact:${turn.children.filter((x) => x.start.kind === "compaction").length}`
        : turn.children.findLast((x) => x.start.kind === "compaction" && !x.end)
            ?.start.id;
    }
    if (id && kind) {
      let child = turn.children.find((x) => x.start.id === id);
      if (!child && isStart) {
        if (turn.children.length >= 64) {
          await gap(config, "CHILD_CAP_REACHED");
          return;
        }
        child = {
          start: {
            id,
            kind,
            name:
              kind === "tool"
                ? safeLabel(event.tool_name)
                : kind === "llm"
                  ? "muse.model-attempt.preview"
                  : `muse.${kind}`,
            toolCallId: kind === "tool" ? String(event.tool_use_id) : undefined,
            timing: "observed-event",
            startTime: now,
            model: safeLabel(event.model),
            provider: safeLabel(event.provider ?? event.model_provider),
            fidelity: kind === "llm" ? "native-request" : "unknown",
            input: text(
              kind === "llm"
                ? {
                    completeness: "partial",
                    messages: event.messages,
                    tools: event.tools,
                  }
                : event.tool_input,
              config,
              turn,
            ),
          },
        };
        turn.children.push(child);
      }
      if (isEnd && child && !child.end) {
        const error =
          name === "PostToolUseFailure" ||
          (kind === "llm" && (event.status === "error" || !!event.error));
        child.end = {
          endTime: now,
          outcome: error ? "error" : "success",
          output: text(
            error
              ? event.error
              : kind === "llm"
                ? {
                    completeness: "partial",
                    output_text_preview: event.output_text_preview,
                  }
                : kind === "agent"
                  ? event.last_assistant_message
                  : event.tool_response,
            config,
            turn,
            true,
            error,
          ),
          usage: kind === "llm" ? requestUsage(turn, event) : undefined,
        };
        if (kind === "llm")
          await gap(
            config,
            turn.usageDiscontinuous
              ? "CUMULATIVE_USAGE_OWNERSHIP_UNRESOLVED"
              : "LLM_CONTENT_IS_PREVIEW_NOT_FULL_TRANSCRIPT",
          );
      } else if (isEnd && !child) await gap(config, "MISSING_CHILD_START");
    }
    if (name === "Stop" || name === "StopFailure") {
      if (!collectionEnabled(config)) return;
      const tracer = createTurnTracer({ config });
      try {
        const root = tracer.startTurn({
          sessionId: turn.session,
          turnId: turn.id,
          input: turn.input,
          startTime: new Date(turn.start),
          usagePlan: "attempt",
        });
        for (const child of turn.children) {
          const handle = root.child({
            ...child.start,
            startTime: new Date(child.start.startTime),
          });
          const end = child.end ?? {
            outcome: "incomplete" as const,
            endTime: now,
          };
          handle.end({ ...end, endTime: new Date(end.endTime) });
        }
        root.end({
          usage: turn.cumulative,
          output: text(
            event.last_assistant_message,
            config,
            turn,
            true,
            name === "StopFailure",
          ),
          outcome:
            name === "StopFailure"
              ? "error"
              : turn.children.some((x) => !x.end)
                ? "incomplete"
                : "success",
          endTime: new Date(now),
        });
        await tracer.shutdown();
      } finally {
        // No automatic retry after ambiguous HTTP delivery. A tombstone prevents duplicate Stop exports.
        turn = {
          ...turn,
          input: undefined,
          children: [],
          contentBytes: 0,
          closed: true,
        };
      }
    }
    if (collectionEnabled(config)) {
      await lock.assertHeld();
      await writeProtected(file, JSON.stringify(seal(turn, key, aad)));
      if (turn.closed) {
        // Keep only bounded metadata tombstones; completed turns must not exhaust the pending-state cap.
        await updateJson<Record<string, number>>(dedupFile, (old) => ({
          ...Object.fromEntries(
            Object.entries(old ?? {})
              .filter(([, expiry]) => expiry > now)
              .slice(-383),
          ),
          [identity]: now + 86400000,
        }));
        await fs.unlink(file);
      }
    }
  } finally {
    await lock.release();
  }
}

export async function main(binding: Binding): Promise<void> {
  // Muse scrubs FI_* and XDG_* from hook env. Pin discovery paths, never credentials, in the installed launcher.
  const watchdog = setTimeout(() => process.exit(0), 6000);
  watchdog.unref();
  try {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const part of process.stdin) {
      const chunk = Buffer.from(part);
      size += chunk.length;
      if (size > 262144) return;
      chunks.push(chunk);
    }
    const event = JSON.parse(
      Buffer.concat(chunks).toString("utf8"),
    ) as HookEvent;
    if (
      !event ||
      typeof event !== "object" ||
      typeof event.hook_event_name !== "string"
    )
      return;
    if (!safeId(event.session_id)) return;
    const env = {
      ...process.env,
      FI_AGENT_CONFIG_DIR: binding.configDir,
      XDG_STATE_HOME: binding.stateHome,
    };
    const base = {
      agent: "muse",
      home: binding.home,
      agentDir: binding.agentDir,
      env,
    };
    // Tool hooks may execute in a different cwd. Bind routing at session start/prompt,
    // never let a tool's chdir select another project's profile for the same turn.
    type Routes = Record<string, { directory: string; expires: number }>;
    const routeFile = join(controlPaths(base).stateDir, "muse-routing.json");
    const routeKey = createHash("sha256")
      .update(event.session_id)
      .digest("hex");
    const routes = (await readJson<Routes>(routeFile)) ?? {};
    const previous = routes[routeKey];
    const projectDir =
      previous && previous.expires > Date.now()
        ? previous.directory
        : process.cwd();
    const config = await resolveConfig({ ...base, projectDir });
    config.hideInputs ||= binding.hideInputs;
    config.hideOutputs ||= binding.hideOutputs;
    config.hideErrorDetails ||=
      binding.hideErrorDetails || binding.hideInputs || binding.hideOutputs;
    if (!collectionEnabled(config)) return;
    if (
      ["SessionStart", "UserPromptSubmit", "SubagentStart"].includes(
        event.hook_event_name,
      )
    ) {
      await updateJson<Routes>(routeFile, (current) => {
        const retained = Object.entries(current ?? {})
          .filter(([, item]) => item.expires > Date.now())
          .slice(-24);
        const next = Object.fromEntries(retained);
        next[routeKey] = {
          directory: projectDir,
          expires: Date.now() + 86400000,
        };
        if (
          event.hook_event_name === "SubagentStart" &&
          safeId(event.child_session_id)
        ) {
          next[
            createHash("sha256").update(event.child_session_id).digest("hex")
          ] = next[routeKey];
        }
        return next;
      });
    }
    await consume(event, config);
    if (event.hook_event_name === "SessionEnd")
      await updateJson<Routes>(routeFile, (current) => {
        const next = { ...current };
        delete next[routeKey];
        return next;
      });
  } catch {
    /* fail open, silent, no raw payload/error leakage */
  } finally {
    clearTimeout(watchdog);
  }
}
