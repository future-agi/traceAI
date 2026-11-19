import { deliveryStatePath } from "@traceai/agent-core/config";
import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import { createCipheriv, createDecipheriv, createHmac, createHash, randomBytes } from "node:crypto";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import {
  collectionEnabled, createTurnTracer, ensurePrivateDirectory, readProtected, writeProtected,
  contentHash, updateJson, type ResolvedConfig, type DeliveryState, type TurnOutcome,
} from "@traceai/agent-core";
import { type KiroEvent } from "./events.js";

interface Turn { id: string; start: number; input?: string; tools: KiroEvent[]; gap?: boolean; }
interface State { version: 1; binding: string; updated: number; sequence: number; turn?: Turn; }
const TTL = 24 * 60 * 60 * 1000;
const MAX_STATES = 128;
const MAX_PLAIN_BYTES = 40000;
function key(config: ResolvedConfig): Buffer {
  if (config.identityKey) return Buffer.from(config.identityKey, "hex");
  if (!config.credentials) throw new Error("CREDENTIALS_REQUIRED");
  return createHash("sha256").update(JSON.stringify(["kiro-state-v1", config.credentials.apiKey, config.credentials.secretKey])).digest();
}
function binding(config: ResolvedConfig): string {
  return contentHash(JSON.stringify([config.endpoint, config.projectName, config.profile, config.generation,
    config.captureContent, config.hideInputs, config.hideOutputs, config.hideErrorDetails]));
}
function seal(state: State, secret: Buffer): string {
  const iv = randomBytes(12), cipher = createCipheriv("aes-256-gcm", secret, iv);
  cipher.setAAD(Buffer.from("futureagi-kiro-v1"));
  const body = Buffer.concat([cipher.update(JSON.stringify(state), "utf8"), cipher.final()]);
  return JSON.stringify({ v: 1, iv: iv.toString("base64"), tag: cipher.getAuthTag().toString("base64"), data: body.toString("base64") });
}
function unseal(value: string, secret: Buffer): State {
  const envelope = JSON.parse(value);
  if (envelope.v !== 1) throw new Error("INVALID_STATE");
  const cipher = createDecipheriv("aes-256-gcm", secret, Buffer.from(envelope.iv, "base64"));
  cipher.setAAD(Buffer.from("futureagi-kiro-v1"));
  cipher.setAuthTag(Buffer.from(envelope.tag, "base64"));
  const state = JSON.parse(Buffer.concat([cipher.update(Buffer.from(envelope.data, "base64")), cipher.final()]).toString("utf8"));
  if (state.version !== 1 || !Number.isSafeInteger(state.sequence) || !Number.isFinite(state.updated)) throw new Error("INVALID_STATE");
  return state;
}
async function diagnostic(config: ResolvedConfig, code?: string, count = 0): Promise<void> {
  await updateJson<DeliveryState>(deliveryStatePath(config), old => ({
    pending: 0, exported: 0, dropped: 0, ...old,
    activatedAt: old?.activatedAt ?? new Date().toISOString(),
    ...(code ? { lastError: code, dropped: (old?.dropped ?? 0) + count } : {}),
  })).catch(() => {});
}
async function prune(dir: string, target: string, config: ResolvedConfig): Promise<void> {
  const names = (await fs.readdir(dir)).filter(n => /^[a-f0-9]{64}\.json$/.test(n));
  let remaining = 0;
  for (const name of names) {
    const path = join(dir, name), st = await fs.lstat(path);
    if (!st.isFile() || st.isSymbolicLink()) throw new Error("UNSAFE_STATE");
    if (st.mtimeMs < Date.now() - TTL) {
      // Never follow paths taken from state. Expiry deliberately loses unfinished telemetry.
      await fs.unlink(path);
      await diagnostic(config, "STATE_EXPIRED", 1);
    } else remaining++;
  }
  if (remaining >= MAX_STATES && !names.includes(target)) throw new Error("STATE_LIMIT");
}
/** Claim and discard pending content before export: never retry ambiguous OTLP or replay after disable. */
export async function capture(event: KiroEvent, config: ResolvedConfig): Promise<void> {
  const collect = config.destinationApproved && !!config.credentials && collectionEnabled(config);
  // An observed kill switch discards this conversation's pending state even if no explicit disable command ran.
  if (!collect && !config.identityKey && !config.credentials) return;
  const secret = key(config), dir = join(config.paths.configDir, "kiro-state");
  await ensurePrivateDirectory(dir);
  const name = createHmac("sha256", secret).update(event.sessionId).digest("hex") + ".json";
  await prune(dir, name, config);
  const path = join(dir, name);
  const lock = await acquireWriteLock(`${path}.event`, { lockWaitMs: 1500 }).catch(() => undefined);
  if (!lock) { await diagnostic(config, "CONCURRENT_HOOK_DROPPED", 1); return; }
  const exports: { turn: Turn; end: number; output?: string; outcome: TurnOutcome }[] = [];
  try {
    const previous = await readProtected(path);
    let state: State | undefined;
    if (previous) {
      try { state = unseal(previous, secret); }
      catch { await diagnostic(config, "INVALID_STATE", 1); }
    }
    if (state && (state.binding !== binding(config) || state.updated < Date.now() - TTL)) {
      if (state.turn) await diagnostic(config, "STATE_POLICY_CHANGED", 1);
      state = undefined;
    }
    state ??= { version: 1, binding: binding(config), updated: Date.now(), sequence: 0 };
    if (!collect) {
      delete state.turn;
    } else if (event.event === "UserPromptSubmit") {
      if (state.turn) exports.push({ turn: state.turn, end: event.time, outcome: "incomplete" });
      state.sequence++;
      // Kiro has no source turn id: the protected local sequence is an observer anchor, not a provider id.
      state.turn = { id: `observed-${state.sequence}-${randomBytes(8).toString("hex")}`, start: event.time, input: event.input, tools: [] };
    } else if (event.event === "PostToolUse" && state.turn) {
      if (state.turn.tools.length < 60) {
        const { sessionId: _session, ...tool } = event;
        state.turn.tools.push({ ...tool, sessionId: "" });
        if (Buffer.byteLength(JSON.stringify(state)) > MAX_PLAIN_BYTES) {
          state.turn.tools[state.turn.tools.length - 1] = { ...tool, sessionId: "", input: undefined, output: undefined };
          state.turn.gap = true;
        }
      } else state.turn.gap = true;
    } else if ((event.event === "Stop" || event.event === "SessionEnd") && state.turn) {
      exports.push({ turn: state.turn, end: event.time, output: event.output,
        outcome: event.event === "SessionEnd" ? "interrupted" : state.turn.gap ? "incomplete" : "success" });
      delete state.turn;
    }
    state.updated = Date.now();
    await lock.assertHeld();
    await writeProtected(path, seal(state, secret), previous === undefined ? null : contentHash(previous));
  } finally {
    await lock.release();
  }
  if (!collect) return;
  await diagnostic(config);
  if (!exports.length || !collectionEnabled(config)) return;
  const tracer = createTurnTracer({ config });
  try {
    for (const item of exports) {
      const turn = tracer.startTurn({ sessionId: event.sessionId, turnId: item.turn.id,
        input: item.turn.input, startTime: new Date(item.turn.start), usagePlan: "unknown", timing: "observed-event" });
      for (const [index, tool] of item.turn.tools.entries()) {
        // No trustworthy tool call ids or duration: a point-in-time completion, never a guessed pre/post pair.
        turn.child({ id: `observed-tool-${index}`, kind: "tool", name: tool.tool,
          input: tool.input, startTime: new Date(tool.time), timing: "unknown", fidelity: "unknown" })
          .end({ output: tool.output, outcome: tool.failed ? "error" : "success", endTime: new Date(tool.time) });
      }
      turn.end({ output: item.output, outcome: item.outcome, endTime: new Date(item.end) });
    }
    await tracer.flush();
  } finally { await tracer.shutdown(); }
}
