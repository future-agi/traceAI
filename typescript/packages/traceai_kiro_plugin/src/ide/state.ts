import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import { createCipheriv, createDecipheriv, createHmac, randomBytes, randomUUID } from "node:crypto";
import { contentHash, ensurePrivateDirectory, readProtected, writeProtected, withFileLock, writeProtectedUnderLock, type ResolvedConfig } from "@traceai/agent-core";
import type { IdeEvent } from "./events.js";

const TTL = 24 * 60 * 60 * 1000;
const MAX_PLAIN = 44 * 1024;
export interface IdeTurn { id: string; events: IdeEvent[]; gap?: string }
interface Envelope { schema: 1; route: string; updated: number; sealed?: string }
export function routeId(c: ResolvedConfig): string {
  return contentHash(JSON.stringify([c.endpoint, c.projectName, c.profile, c.paths.projectBinding, c.generation,
    c.captureContent, c.hideInputs, c.hideOutputs, c.hideErrorDetails, c.userId, c.environment, c.tags,
    c.credentials?.apiKey, c.credentials?.secretKey]));
}
async function keyFor(c: ResolvedConfig): Promise<Buffer> {
  const file = join(c.paths.configDir, "kiro-ide-state-key");
  return withFileLock(file, async assertHeld => {
    let value = await readProtected(file);
    if (!value) {
      value = randomBytes(32).toString("hex");
      await assertHeld();
      await writeProtectedUnderLock(file, value, null);
    }
    if (!/^[a-f0-9]{64}$/.test(value)) throw new Error("INVALID_STATE_KEY");
    return Buffer.from(value, "hex");
  }, { lockWaitMs: 1500 });
}
function seal(turn: IdeTurn, key: Buffer, route: string): string {
  const iv = randomBytes(12), cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(route));
  const bytes = Buffer.concat([cipher.update(JSON.stringify(turn)), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), bytes]).toString("base64");
}
function open(value: string, key: Buffer, route: string): IdeTurn {
  const bytes = Buffer.from(value, "base64");
  const decipher = createDecipheriv("aes-256-gcm", key, bytes.subarray(0, 12));
  decipher.setAAD(Buffer.from(route));
  decipher.setAuthTag(bytes.subarray(12, 28));
  const turn = JSON.parse(Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString());
  if (typeof turn.id !== "string" || !Array.isArray(turn.events) || turn.events.length > 63) throw new Error("INVALID_TURN_STATE");
  return turn;
}
/** Per-session encrypted state, at most 256 x 64 KiB. No background process or raw journal.
 * A terminal claim removes content before export. No retry after ambiguity (possible loss).
 * This does not isolate secrets from malicious code running as the same OS user.
 */
export async function appendIdeEvent(c: ResolvedConfig, e: IdeEvent): Promise<IdeTurn | undefined> {
  if (e.event === "PreToolUse") return; // Cannot pair concurrent calls without qualified source IDs.
  const dir = join(c.paths.stateDir, "kiro-ide-turns");
  await ensurePrivateDirectory(dir);
  const key = await keyFor(c), route = routeId(c);
  const files = await fs.readdir(dir);
  if (files.length > 512) throw new Error("STATE_DIRECTORY_LIMIT");
  let count = 0;
  for (const name of files) {
    if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
    const path = join(dir, name), stat = await fs.lstat(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.mode & 0o077 || (process.getuid && stat.uid !== process.getuid())) throw new Error("UNSAFE_STATE");
    if (Date.now() - stat.mtimeMs > TTL) await fs.unlink(path);
    else count++;
  }
  // No filesystem path from the event is ever used. Route changes fence old state.
  const file = join(dir, createHmac("sha256", key).update(JSON.stringify([route, e.session])).digest("hex") + ".json");
  const lock = await acquireWriteLock(`${file}.event`, { lockWaitMs: 1500 });
  try {
    const before = await readProtected(file);
    if (before === undefined && count >= 256) throw new Error("STATE_DIRECTORY_LIMIT");
    const old: Envelope | undefined = before ? JSON.parse(before) : undefined;
    if (old && (old.schema !== 1 || old.route !== route)) throw new Error("STATE_ROUTE_MISMATCH");
    let turn = old?.sealed ? open(old.sealed, key, route) : undefined;
    if (e.event === "SessionStart") turn = undefined;
    else if (e.event === "UserPromptSubmit") {
      // The IDE serializer has no source turn id. This observed sequence is not a resume/branch anchor.
      turn = { id: randomUUID(), events: [], ...(turn ? { gap: "MISSING_STOP" } : {}) };
    } else if (!turn) return;
    if (turn) {
      if (turn.events.length >= 62 && e.event !== "Stop") { turn.gap = "EVENT_LIMIT"; }
      else {
        let accepted = e;
        if (Buffer.byteLength(JSON.stringify(turn)) + Buffer.byteLength(JSON.stringify(e)) > MAX_PLAIN - 1024) {
          accepted = { ...e, input: undefined, output: undefined };
          turn.gap = "CONTENT_LIMIT";
        }
        turn.events.push(accepted);
      }
    }
    if (turn && Buffer.byteLength(JSON.stringify(turn)) > MAX_PLAIN) throw new Error("STATE_LIMIT");
    const terminal = e.event === "Stop";
    const next: Envelope = { schema: 1, route, updated: Date.now(),
      ...(turn && !terminal ? { sealed: seal(turn, key, route) } : {}) };
    await lock.assertHeld();
    await writeProtected(file, JSON.stringify(next), before === undefined ? null : contentHash(before));
    return terminal ? turn : undefined;
  } finally {
    await lock.release();
  }
}
