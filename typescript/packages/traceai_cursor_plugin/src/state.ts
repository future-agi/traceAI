import { promises as fs } from "node:fs";
import { join } from "node:path";
import {
  createCipheriv,
  createDecipheriv,
  createHmac,
  randomBytes,
} from "node:crypto";
import {
  contentHash,
  ensurePrivateDirectory,
  readProtected,
  withFileLock,
  writeProtectedUnderLock,
  type ResolvedConfig,
} from "@traceai/agent-core/hook-state";
import { type CursorEvent } from "./events.js";

const MAX_PLAINTEXT = 44 * 1024;
const EXPIRY_MS = 24 * 60 * 60 * 1000;
export const FINISHER_CAP_MS = 30_000;
interface Envelope {
  schemaVersion: 1;
  route: string;
  /** Opaque source-session identity; retained on content-free tombstones for sessionEnd deduplication. */
  session?: string;
  updatedAt: number;
  phase: "collecting" | "pending" | "finalized";
  encrypted?: string;
  finisher?: {
    startedAt: number;
    deadline: number;
    token?: string;
    pid?: number;
    detached?: boolean;
    result?: string;
    finishedAt?: number;
  };
}
export interface TurnSnapshot {
  events: CursorEvent[];
  /** Latest source response and aggregate usage survive the detail-event cap. */
  response?: CursorEvent;
  gap?: string;
  projectDir?: string;
}
function replayEvents(state: TurnSnapshot): CursorEvent[] {
  return [...state.events, ...(state.response ? [state.response] : [])];
}
export function routeId(c: ResolvedConfig): string {
  return contentHash(
    JSON.stringify([
      c.agent,
      c.endpoint,
      c.projectName,
      c.profile,
      c.paths.projectBinding,
      c.generation,
      c.captureContent,
      c.hideInputs,
      c.hideOutputs,
      c.hideErrorDetails,
      c.userId,
      c.environment,
      c.tags,
      c.credentials?.apiKey,
      c.credentials?.secretKey,
    ]),
  );
}
/** Share the core's no-follow lease, bounded below the hook's 8s budget.
 * Legacy O_EXCL and adapter .mutex claims are inert after this migration. */
async function locked<T>(file: string, work: () => Promise<T>): Promise<T> {
  return withFileLock(file, async (assertHeld) => {
    const result = await work();
    await assertHeld();
    return result;
  }, { lockWaitMs: 3000 });
}
/** Caller holds the shared file lease; never acquire a nested write lock. */
async function saveState(
  file: string,
  text: string,
  before: string | undefined,
): Promise<void> {
  if (Buffer.byteLength(text) > 65536) throw new Error("STATE_LIMIT");
  await writeProtectedUnderLock(file, text, before === undefined ? null : contentHash(before));
}
async function stateKey(c: ResolvedConfig): Promise<Buffer> {
  const file = join(c.paths.configDir, `${c.agent}-state-key`);
  return locked(file, async () => {
    let key = await readProtected(file);
    if (!key) {
      await saveState(file, randomBytes(32).toString("hex"), undefined);
      key = await readProtected(file);
    }
    if (!key || !/^[a-f0-9]{64}$/.test(key))
      throw new Error("INVALID_STATE_KEY");
    return Buffer.from(key, "hex");
  });
}

function encrypt(value: TurnSnapshot, key: Buffer, route: string): string {
  const nonce = randomBytes(12),
    cipher = createCipheriv("aes-256-gcm", key, nonce);
  cipher.setAAD(Buffer.from(route));
  const data = Buffer.concat([
    cipher.update(JSON.stringify(value)),
    cipher.final(),
  ]);
  return Buffer.concat([nonce, cipher.getAuthTag(), data]).toString("base64");
}
function decrypt(data: string, key: Buffer, route: string): TurnSnapshot {
  const bytes = Buffer.from(data, "base64");
  const decipher = createDecipheriv("aes-256-gcm", key, bytes.subarray(0, 12));
  decipher.setAAD(Buffer.from(route));
  decipher.setAuthTag(bytes.subarray(12, 28));
  const value = JSON.parse(
    Buffer.concat([
      decipher.update(bytes.subarray(28)),
      decipher.final(),
    ]).toString(),
  );
  // 128 detail events, a separate response slot, and the terminal boundary.
  if (!value || !Array.isArray(value.events) || value.events.length > 130)
    throw new Error("INVALID_STATE");
  return value;
}
/** Bounded encrypted, already policy-filtered turn state, not a raw hook journal.
 * The local key is a separate owner-only config file. This is NOT isolation from same-user code.
 * Hooks retain encrypted terminal snapshots while waiting for the host's final disk write.
 * The export claim destroys content before HTTP; ambiguous delivery is never replayed.
 */
export async function appendEvent(
  c: ResolvedConfig,
  event: CursorEvent,
  options: { defer?: boolean; projectDir?: string } = {},
): Promise<TurnSnapshot | undefined> {
  if (!event.generation && event.event !== "sessionEnd") return undefined;
  const route = routeId(c),
    key = await stateKey(c);
  const session = createHmac("sha256", key)
    .update(JSON.stringify(["session", route, event.conversation]))
    .digest("hex");
  const dir = join(c.paths.stateDir, `${c.agent}-turns`);
  await ensurePrivateDirectory(dir);
  const entries = await fs.readdir(dir);
  if (entries.length > 512) throw new Error("STATE_DIRECTORY_LIMIT");
  let count = 0;
  for (const entry of entries)
    if (/^[a-f0-9]{64}\.json$/.test(entry)) {
      const file = join(dir, entry),
        stat = await fs.lstat(file).catch((error: NodeJS.ErrnoException) => {
          if (error.code !== "ENOENT") throw error;
          return undefined;
        });
      if (!stat) continue;
      if (
        !stat.isFile() ||
        stat.isSymbolicLink() ||
        stat.nlink !== 1 ||
        (process.getuid && stat.uid !== process.getuid())
      )
        throw new Error("UNSAFE_STATE");
      if (Date.now() - stat.mtimeMs > EXPIRY_MS)
        await fs.unlink(file).catch((error: NodeJS.ErrnoException) => {
          if (error.code !== "ENOENT") throw error;
        });
      else count++;
    }
  const identity = createHmac("sha256", key)
    .update(JSON.stringify([route, event.conversation, event.generation]))
    .digest("hex");
  const file = join(dir, identity + ".json");
  // A CLI sessionEnd can have a different generation id from the turns it closes.
  // Serialize session claims with appends, retaining only opaque identities after export.
  return locked(join(dir, session + ".session"), async () => {
    if (event.event === "sessionEnd") {
      let seen = false;
      const pending: {
        file: string;
        before: string;
        snapshot: TurnSnapshot;
      }[] = [];
      for (const entry of await fs.readdir(dir)) {
        if (!/^[a-f0-9]{64}\.json$/.test(entry)) continue;
        const candidate = join(dir, entry);
        const before = await readProtected(candidate);
        if (!before) continue;
        const envelope: Envelope = JSON.parse(before);
        if (envelope.session !== session || envelope.route !== route) continue;
        if (envelope.schemaVersion !== 1) throw new Error("INVALID_STATE");
        seen = true;
        if (envelope.phase === "collecting" && envelope.encrypted) {
          // Same bound as the observer: never accumulate an unbounded session snapshot.
          if (pending.length >= 2) throw new Error("SESSION_TURN_LIMIT");
          pending.push({
            file: candidate,
            before,
            snapshot: decrypt(envelope.encrypted, key, route),
          });
        }
      }
      if (seen) {
        if (!pending.length) return undefined;
        for (const item of pending)
          await locked(item.file, () =>
            saveState(
              item.file,
              JSON.stringify({
                schemaVersion: 1,
                route,
                session,
                updatedAt: Date.now(),
                phase: options.defer ? "pending" : "finalized",
                ...(options.defer
                  ? {
                      encrypted: encrypt(
                        {
                          events: [...replayEvents(item.snapshot), event],
                          gap: item.snapshot.gap,
                          projectDir: options.projectDir,
                        },
                        key,
                        route,
                      ),
                      finisher: {
                        startedAt: Date.now(),
                        deadline: Date.now() + FINISHER_CAP_MS,
                      },
                    }
                  : {}),
              } satisfies Envelope),
              item.before,
            ),
          );
        return {
          events: [
            ...pending.flatMap((item) => replayEvents(item.snapshot)),
            event,
          ],
          gap: pending.find((item) => item.snapshot.gap)?.snapshot.gap,
        };
      }
    }
    return locked(file, async () => {
      const before = await readProtected(file);
      if (before === undefined && count >= 256)
        throw new Error("TURN_STATE_LIMIT");
      const old: Envelope | undefined = before ? JSON.parse(before) : undefined;
      if (old && (old.schemaVersion !== 1 || old.route !== route))
        throw new Error("STATE_ROUTE_MISMATCH");
      if (old?.phase === "finalized" || old?.phase === "pending")
        return undefined;
      const state: TurnSnapshot = old?.encrypted
        ? decrypt(old.encrypted, key, route)
        : { events: [] };
      const terminal = event.event === "stop" || event.event === "sessionEnd";
      // Terminals are outside the detail-event cap. Keep the latest response separately
      // so its aggregate usage can still finalize a full turn when stop omits usage.
      const response = event.event === "afterAgentResponse";
      const previousResponse = state.response;
      if (response) state.response = undefined;
      // Suppress exact hook replay; do not collapse two different calls or infer alias identities.
      const signature = ({ receivedAt: _time, ...rest }: CursorEvent) =>
        JSON.stringify(rest);
      if (
        !terminal &&
        (response ||
          !state.events.some(
            (previous) => signature(previous) === signature(event),
          ))
      ) {
        if (!response && state.events.length >= 128)
          state.gap = "TURN_EVENT_LIMIT";
        else {
          let accepted = response
            ? {
                ...event,
                usage: event.usage ?? previousResponse?.usage,
                model: event.model ?? previousResponse?.model,
                transcript: event.transcript ?? previousResponse?.transcript,
              }
            : event;
          if (
            Buffer.byteLength(JSON.stringify(state)) +
              Buffer.byteLength(JSON.stringify(accepted)) >
            MAX_PLAINTEXT - 1024
          ) {
            accepted = { ...accepted, input: undefined, output: undefined };
            state.gap = "TURN_CONTENT_LIMIT";
          }
          if (response) state.response = accepted;
          else state.events.push(accepted);
        }
      }
      if (Buffer.byteLength(JSON.stringify(state)) > MAX_PLAINTEXT)
        throw new Error("STATE_LIMIT");
      const snapshot = terminal
        ? {
            events: [...replayEvents(state), event],
            gap: state.gap,
            projectDir: options.projectDir,
          }
        : undefined;
      const next: Envelope = {
        schemaVersion: 1,
        route,
        session,
        updatedAt: Date.now(),
        phase: terminal
          ? options.defer
            ? "pending"
            : "finalized"
          : "collecting",
        ...(terminal
          ? options.defer
            ? {
                encrypted: encrypt(snapshot!, key, route),
                finisher: {
                  startedAt: Date.now(),
                  deadline: Date.now() + FINISHER_CAP_MS,
                },
              }
            : {}
          : { encrypted: encrypt(state, key, route) }),
      };
      await saveState(file, JSON.stringify(next), before);
      return snapshot;
    });
  });
}

export interface PendingTurn {
  id: string;
  route: string;
  token: string;
  startedAt: number;
  deadline: number;
  snapshot: TurnSnapshot;
}
const validId = (id: string) => /^[a-f0-9]{64}$/.test(id);
async function turnFiles(c: ResolvedConfig): Promise<string[]> {
  const entries = await fs
    .readdir(join(c.paths.stateDir, `${c.agent}-turns`))
    .catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "ENOENT") throw error;
      return [];
    });
  if (entries.length > 512) throw new Error("STATE_DIRECTORY_LIMIT");
  return entries.filter((name) => /^[a-f0-9]{64}\.json$/.test(name));
}
function alive(pid: number | undefined): boolean {
  if (!Number.isSafeInteger(pid) || !pid || pid < 1) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}
/** Read metadata only; status never decrypts content or claims a pending turn. */
export async function finisherStatus(c: ResolvedConfig) {
  const pending: {
    turn: string;
    startedAt: string;
    deadline: string;
    recoverable: boolean;
  }[] = [];
  let last: { turn: string; result: string; finishedAt: string } | undefined;
  for (const name of await turnFiles(c)) {
    const file = join(c.paths.stateDir, `${c.agent}-turns`, name);
    // Serialize metadata reads with atomic replacement: an opened old inode can
    // otherwise reach nlink=0 mid-read and correctly trip the no-follow guard.
    const raw = await locked(file, () => readProtected(file));
    if (!raw) continue;
    const state: Envelope = JSON.parse(raw),
      f = state.finisher;
    if (state.schemaVersion !== 1 || !f) continue;
    const turn = name.slice(0, -5);
    if (state.phase === "pending")
      pending.push({
        turn,
        startedAt: new Date(f.startedAt).toISOString(),
        deadline: new Date(f.deadline).toISOString(),
        recoverable: !alive(f.pid) || Date.now() >= f.deadline,
      });
    else if (f.result && f.finishedAt) {
      const finishedAt = new Date(f.finishedAt).toISOString();
      if (!last || finishedAt > last.finishedAt)
        last = { turn, result: f.result, finishedAt };
    }
  }
  return { pending, last };
}
export async function pendingTurnIds(c: ResolvedConfig): Promise<string[]> {
  const ids: string[] = [];
  for (const name of await turnFiles(c)) {
    const file = join(c.paths.stateDir, `${c.agent}-turns`, name);
    // Serialize metadata reads with atomic replacement: an opened old inode can
    // otherwise reach nlink=0 mid-read and correctly trip the no-follow guard.
    const raw = await locked(file, () => readProtected(file));
    if (!raw) continue;
    const state: Envelope = JSON.parse(raw);
    if (
      state.schemaVersion === 1 &&
      state.phase === "pending" &&
      state.route === routeId(c)
    )
      ids.push(name.slice(0, -5));
  }
  return ids;
}
/** Dead owners are recoverable until the export claim; a nonce fences out delayed/stale children. */
export async function claimPending(
  c: ResolvedConfig,
  id: string,
  token?: string,
): Promise<PendingTurn | undefined> {
  if (!validId(id)) return;
  const file = join(c.paths.stateDir, `${c.agent}-turns`, id + ".json");
  return locked(file, async () => {
    const before = await readProtected(file);
    if (!before) return;
    const state: Envelope = JSON.parse(before),
      f = state.finisher;
    if (
      state.schemaVersion !== 1 ||
      state.phase !== "pending" ||
      (!token && state.route !== routeId(c)) ||
      !f ||
      !state.encrypted
    )
      return;
    if (token ? token !== f.token : alive(f.pid) && Date.now() < f.deadline)
      return;
    const snapshot = decrypt(state.encrypted, await stateKey(c), state.route);
    f.token = token ?? randomBytes(32).toString("hex");
    f.pid = process.pid;
    await saveState(file, JSON.stringify(state), before);
    return {
      id,
      route: state.route,
      token: f.token,
      startedAt: f.startedAt,
      deadline: f.deadline,
      snapshot,
    };
  });
}
export async function handoffPending(
  c: ResolvedConfig,
  claim: PendingTurn,
  pid: number,
): Promise<void> {
  const file = join(c.paths.stateDir, `${c.agent}-turns`, claim.id + ".json");
  await locked(file, async () => {
    const before = await readProtected(file);
    if (!before) return;
    const state: Envelope = JSON.parse(before);
    if (state.phase !== "pending" || state.finisher?.token !== claim.token)
      return;
    state.finisher.pid = pid;
    state.finisher.detached = true;
    await saveState(file, JSON.stringify(state), before);
  });
}
/** First call commits the at-most-once export boundary. Subsequent calls only update its result. */
export async function finishPending(
  c: ResolvedConfig,
  claim: PendingTurn,
  result: string,
  update = false,
): Promise<boolean> {
  const file = join(c.paths.stateDir, `${c.agent}-turns`, claim.id + ".json");
  return locked(file, async () => {
    const before = await readProtected(file);
    if (!before) return false;
    const state: Envelope = JSON.parse(before);
    if (
      state.finisher?.token !== claim.token ||
      state.phase !== (update ? "finalized" : "pending")
    )
      return false;
    state.phase = "finalized";
    delete state.encrypted;
    delete state.finisher.pid;
    state.updatedAt = Date.now();
    state.finisher.result = result;
    state.finisher.finishedAt = Date.now();
    await saveState(file, JSON.stringify(state), before);
    return true;
  });
}
