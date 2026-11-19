import { acquireWriteLock } from "@traceai/agent-core/file-lock";
import { createCipheriv, createDecipheriv, createHmac, randomBytes, randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import { contentHash, readProtected, writeProtected, ensurePrivateDirectory, type ResolvedConfig } from "@traceai/agent-core";
import type { ToolRecord } from "./events";

export interface SessionState { schema: 1; id: string; generation: number; expires: number; tools: ToolRecord[]; dropped: number; content: boolean; hideInputs: boolean; hideOutputs: boolean; hideErrorDetails: boolean }
const TTL = 24 * 60 * 60 * 1000;
function material(config: ResolvedConfig): Buffer {
  const source = config.identityKey ?? JSON.stringify([config.credentials?.apiKey, config.credentials?.secretKey]);
  return createHmac("sha256", source).update(JSON.stringify(["augment-state-v1", config.agent, config.endpoint, config.projectName])).digest();
}
export function statePath(config: ResolvedConfig, session: string): string {
  return join(config.paths.stateDir, `${config.agent}-sessions`, createHmac("sha256", material(config)).update(session).digest("hex") + ".json");
}
export function fresh(config: ResolvedConfig): SessionState {
  return { schema: 1, id: randomUUID(), generation: config.generation, expires: Date.now() + TTL, tools: [], dropped: 0,
    content: config.captureContent, hideInputs: config.hideInputs, hideOutputs: config.hideOutputs, hideErrorDetails: config.hideErrorDetails };
}
export async function load(path: string, config: ResolvedConfig): Promise<SessionState> {
  const text = await readProtected(path);
  if (!text) return fresh(config);
  try {
    const envelope = JSON.parse(text);
    const decipher = createDecipheriv("aes-256-gcm", material(config), Buffer.from(envelope.iv, "hex"));
    decipher.setAAD(Buffer.from(path));
    decipher.setAuthTag(Buffer.from(envelope.tag, "hex"));
    const state: SessionState = JSON.parse(Buffer.concat([decipher.update(Buffer.from(envelope.data, "base64")), decipher.final()]).toString());
    if (state.schema !== 1 || state.expires < Date.now() || state.generation !== config.generation) return fresh(config);
    // A privacy reduction purges buffered content. Later re-enabling cannot resurrect it.
    for (const tool of state.tools) {
      if (config.hideInputs) delete tool.input;
      if (config.hideOutputs || (config.hideErrorDetails && tool.outcome === "error")) delete tool.output;
    }
    return state;
  } catch { return fresh(config); }
}
export async function save(path: string, state: SessionState, config: ResolvedConfig): Promise<void> {
  while (state.tools.length > 32 || Buffer.byteLength(JSON.stringify(state)) > 40000) {
    state.tools.shift(); state.dropped++;
  }
  const iv = randomBytes(12), cipher = createCipheriv("aes-256-gcm", material(config), iv);
  cipher.setAAD(Buffer.from(path));
  const data = Buffer.concat([cipher.update(JSON.stringify(state)), cipher.final()]);
  const previous = await readProtected(path);
  await writeProtected(path, JSON.stringify({ iv: iv.toString("hex"), tag: cipher.getAuthTag().toString("hex"), data: data.toString("base64") }), previous === undefined ? null : contentHash(previous));
}
/** Bounded, metadata-only cleanup: maximum 128 encrypted sessions, each below 64 KiB. */
export async function lockState(path: string): Promise<() => Promise<void>> {
  const dir = join(path, "..");
  await ensurePrivateDirectory(dir);
  const entries = await fs.readdir(dir);
  if (entries.length > 260) throw new Error("STATE_CAPACITY");
  const candidates = [];
  for (const name of entries) {
    if (!/^[0-9a-f]{64}\.json$/.test(name)) continue;
    const file = join(dir, name), st = await fs.lstat(file);
    if (st.isSymbolicLink() || !st.isFile()) throw new Error("UNSAFE_STATE");
    if (Date.now() - st.mtimeMs > TTL) await fs.unlink(file);
    else candidates.push(file);
  }
  if (candidates.length >= 128 && !candidates.includes(path)) throw new Error("STATE_CAPACITY");
  const lock = await acquireWriteLock(`${path}.event`, { lockWaitMs: 1500 });
  return async () => { await lock.release(); };
}
