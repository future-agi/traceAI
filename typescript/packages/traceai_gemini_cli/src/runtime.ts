import { deliveryStatePath } from "@traceai/agent-core/config";
import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";
import { promises as fs } from "node:fs";
import { dirname, join } from "node:path";
import { parse, type ParseError } from "jsonc-parser";
import {
  resolveConfig,
  createTurnTracer,
  ensurePrivateDirectory,
  readProtected,
  writeProtected,
  updateJson,
  type ResolvedConfig,
  type DeliveryState,
  type TurnOutcome,
} from "@traceai/agent-core";
import {
  applyEvent,
  binding,
  opaque,
  validateEvent,
  eventTime,
  type TurnState,
} from "./mapper.js";
import { systemPolicyConflict } from "./host-policy.js";

const MAX_AGE = 24 * 60 * 60 * 1000;
function encryptionKey(config: ResolvedConfig): Buffer {
  if (!config.identityKey) throw new Error("SETUP_REQUIRED");
  return createHash("sha256")
    .update("gemini-recovery-v1")
    .update(config.identityKey)
    .digest();
}
export function seal(state: TurnState, config: ResolvedConfig): string {
  const iv = randomBytes(12),
    cipher = createCipheriv("aes-256-gcm", encryptionKey(config), iv);
  cipher.setAAD(Buffer.from(binding(config)));
  const encrypted = Buffer.concat([
    cipher.update(JSON.stringify(state), "utf8"),
    cipher.final(),
  ]);
  return JSON.stringify({
    iv: iv.toString("hex"),
    tag: cipher.getAuthTag().toString("hex"),
    data: encrypted.toString("base64"),
  });
}
export function unseal(raw: string, config: ResolvedConfig): TurnState {
  const value = JSON.parse(raw),
    decipher = createDecipheriv(
      "aes-256-gcm",
      encryptionKey(config),
      Buffer.from(value.iv, "hex"),
    );
  decipher.setAAD(Buffer.from(binding(config)));
  decipher.setAuthTag(Buffer.from(value.tag, "hex"));
  const state = JSON.parse(
    Buffer.concat([
      decipher.update(Buffer.from(value.data, "base64")),
      decipher.final(),
    ]).toString("utf8"),
  );
  if (
    state.schema !== 1 ||
    state.binding !== binding(config) ||
    !Array.isArray(state.children) ||
    Date.now() - state.start > MAX_AGE
  )
    throw new Error("STALE_STATE");
  return state;
}
async function diagnostic(config: ResolvedConfig, code: string): Promise<void> {
  await updateJson<DeliveryState>(
    deliveryStatePath(config),
    (old) => ({
      pending: 0,
      exported: old?.exported ?? 0,
      dropped: (old?.dropped ?? 0) + 1,
      lastAcceptedAt: old?.lastAcceptedAt,
      activatedAt: old?.activatedAt,
      lastError: code,
    }),
  ).catch(() => {});
}
async function emit(
  state: TurnState,
  config: ResolvedConfig,
  time: number,
  outcome: TurnOutcome,
): Promise<void> {
  const tracer = createTurnTracer({ config });
  const turn = tracer.startTurn({
    sessionId: state.session,
    turnId: state.turn,
    input: state.input,
    startTime: new Date(state.start),
    usagePlan: "request",
  });
  for (const child of state.children) {
    // A BeforeModel hook alone can be blocked or replaced by a synthetic response. It is not an LLM call.
    if (child.kind === "llm" && !child.observedResponse) continue;
    turn
      .child({
        id: child.id,
        kind: child.kind,
        name: child.name,
        input: child.input,
        model: child.model,
        provider: child.kind === "llm" ? "google" : undefined,
        startTime: new Date(child.start),
        fidelity: child.kind === "llm" ? "native-request" : "unknown",
      })
      .end({
        output: child.output,
        endTime: new Date(child.end ?? time),
        usage: child.usage,
        outcome:
          child.kind === "llm" && !child.finished
            ? "incomplete"
            : (child.outcome ?? "success"),
      });
  }
  turn.end({
    output: state.output,
    endTime: new Date(time),
    outcome: state.gap ? "incomplete" : outcome,
  });
  await tracer.flush();
  await tracer.shutdown();
}
export async function handleHook(
  raw: unknown,
  projectDir = process.cwd(),
): Promise<void> {
  const event = validateEvent(raw);
  // Use the hook process's working directory, never the payload-supplied cwd or transcript path.
  const config = await resolveConfig({ agent: "gemini", projectDir });
  const directory = join(config.paths.stateDir, "gemini-turns");
  await ensurePrivateDirectory(directory);
  const path = join(directory, `${opaque(config, event.session_id)}.json`);
  if (!config.enabled || !config.destinationApproved || !config.identityKey) {
    await fs.unlink(path).catch(() => {});
    return;
  }
  const policyConflict = await systemPolicyConflict();
  if (
    policyConflict ||
    [process.env.GEMINI_TELEMETRY_ENABLED].some(
      (v) => v === "true" || v === "1",
    )
  ) {
    await fs.unlink(path).catch(() => {});
    await diagnostic(config, policyConflict ?? "NATIVE_TELEMETRY_CONFLICT");
    return;
  }
  const userSettingsPath = join(dirname(config.paths.user), "settings.json");
  for (const settingsPath of new Set([
    userSettingsPath,
    join(projectDir, ".gemini", "settings.json"),
  ])) {
    const rawSettings = await readProtected(settingsPath, false).catch(error => {
      if (settingsPath === userSettingsPath) throw error;
      // resolveConfig already reports PROJECT_CONFIG_IGNORED for unsafe workspace
      // ancestors. A secondary host-settings probe must not drop that user-configured turn.
      return undefined;
    });
    if (!rawSettings) continue;
    const errors: ParseError[] = [];
    const settings = parse(rawSettings, errors, { allowTrailingComma: true });
    if (errors.length || settings?.telemetry?.enabled === true) {
      await fs.unlink(path).catch(() => {});
      await diagnostic(
        config,
        errors.length ? "INVALID_GEMINI_SETTINGS" : "NATIVE_TELEMETRY_CONFLICT",
      );
      return;
    }
  }
  const lock = await acquireWriteLock(`${path}.event`, { lockWaitMs: 1500 }).catch(() => undefined);
  if (!lock) {
    await diagnostic(config, "HOOK_CONTENTION");
    return;
  }
  try {
    const entries = await fs.readdir(directory);
    let bytes = 0;
    for (const name of entries.slice(0, 2048)) {
      if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
      const item = join(directory, name),
        stat = await fs.lstat(item);
      if (stat.isSymbolicLink() || !stat.isFile()) continue;
      if (Date.now() - stat.mtimeMs > MAX_AGE) {
        await fs.unlink(item);
        await diagnostic(config, "STATE_EXPIRED");
      } else bytes += stat.size;
    }
    if (entries.length > 2048 || bytes > 64 * 1024 * 1024) {
      await diagnostic(config, "STATE_CAPACITY");
      return;
    }
    let state: TurnState | undefined;
    const saved = await readProtected(path);
    if (saved) {
      try {
        state = unseal(saved, config);
      } catch {
        await fs.unlink(path);
        await diagnostic(config, "STATE_DISCARDED");
      }
    }
    if (event.hook_event_name === "BeforeAgent" && state) {
      const next = applyEvent(state, event, config);
      if (next === state) return; // exact duplicate callback
      await fs.unlink(path).catch(() => {});
      await emit(state, config, eventTime(event), "incomplete");
    }
    await lock.assertHeld();
    state = applyEvent(state, event, config);
    if (!state) return;
    if (
      event.hook_event_name === "AfterAgent" ||
      event.hook_event_name === "SessionEnd"
    ) {
      // Remove before export: at-most-one local attempt. Transport ambiguity is not blindly retried.
      await fs.unlink(path).catch(() => {});
      await emit(
        state,
        config,
        eventTime(event),
        event.hook_event_name === "AfterAgent" ? "success" : "incomplete",
      );
    } else {
      // The protected core file API caps ciphertext at 64 KiB. Drop excess content first, never spill raw data.
      if (Buffer.byteLength(JSON.stringify(state)) > 44000) {
        state.gap = true;
        state.input = undefined;
        state.output = undefined;
        for (const child of state.children) {
          child.input = undefined;
          child.output = undefined;
        }
        while (
          Buffer.byteLength(JSON.stringify(state)) > 44000 &&
          state.children.length
        )
          state.children.shift();
        await diagnostic(config, "TURN_CONTENT_CAP");
      }
      await writeProtected(path, seal(state, config));
    }
  } finally {
    await lock.release();
  }
}
