import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import {
  ensurePrivateDirectory,
  updateJson,
  withFileLock,
  readJson,
  writeProtected,
} from "@traceai/agent-core/files";
import type { ResolvedConfig } from "@traceai/agent-core/config";
import type { Event } from "./events";
interface Envelope {
  v: 1;
  iv: string;
  tag: string;
  body: string;
}
export interface TurnState {
  events: Event[];
  gap: boolean;
}
const digest = (v: unknown) =>
  createHash("sha256").update(JSON.stringify(v)).digest("hex");
export class EventStore {
  readonly dir: string;
  private key: Buffer;
  private route: string;
  constructor(private config: ResolvedConfig) {
    if (!config.identityKey) throw new Error("VALIDATED_PROFILE_REQUIRED");
    this.key = createHash("sha256")
      .update("futureagi-codex-state-v1\0" + config.identityKey)
      .digest();
    this.dir = join(config.paths.configDir, "codex-state");
    this.route = digest([
      config.endpoint,
      config.projectName,
      config.generation,
      config.captureContent,
      config.hideInputs,
      config.hideOutputs,
      config.hideErrorDetails,
    ]);
  }
  private prefix(session: string) {
    return digest([this.route, session]).slice(0, 24);
  }
  private file(event: Event) {
    return join(
      this.dir,
      this.prefix(event.session_id) +
        "-" +
        digest(event.turn_id).slice(0, 24) +
        ".json",
    );
  }
  private decrypt(e: Envelope): TurnState {
    if (
      e.v !== 1 ||
      !/^[a-f0-9]{24}$/.test(e.iv) ||
      !/^[a-f0-9]{32}$/.test(e.tag)
    )
      throw new Error("INVALID_STATE");
    const cipher = createDecipheriv(
      "aes-256-gcm",
      this.key,
      Buffer.from(e.iv, "hex"),
    );
    cipher.setAAD(Buffer.from(this.route));
    cipher.setAuthTag(Buffer.from(e.tag, "hex"));
    return JSON.parse(
      Buffer.concat([
        cipher.update(Buffer.from(e.body, "base64")),
        cipher.final(),
      ]).toString(),
    );
  }
  private encrypt(state: TurnState): Envelope {
    const iv = randomBytes(12),
      cipher = createCipheriv("aes-256-gcm", this.key, iv);
    cipher.setAAD(Buffer.from(this.route));
    return {
      v: 1,
      iv: iv.toString("hex"),
      body: Buffer.concat([
        cipher.update(JSON.stringify(state)),
        cipher.final(),
      ]).toString("base64"),
      tag: cipher.getAuthTag().toString("hex"),
    };
  }
  async append(event: Event): Promise<void> {
    await ensurePrivateDirectory(this.dir);
    const names = await fs.readdir(this.dir);
    if (names.length > 1024) throw new Error("STATE_LIMIT");
    await withFileLock(this.file(event) + ".journal", async () => {
      if (await readJson(this.file(event) + ".done")) return;
      await updateJson<Envelope>(this.file(event), (old) => {
        const state = old ? this.decrypt(old) : { events: [], gap: false };
        state.events.push({ ...event });
        const bytes = () => Buffer.byteLength(JSON.stringify(state));
        // Reserve the bounded journal for identities, request usage and terminal
        // records. Older content must never crowd out later structural events.
        for (const retained of state.events) {
          if (bytes() <= 42000) break;
          for (const field of [
            "prompt",
            "tool_input",
            "tool_response",
            "last_assistant_message",
            "error",
          ] as const) {
            if (retained[field] !== undefined) {
              delete retained[field];
              retained.content_truncated = true;
            }
          }
        }
        // Pathological structural volume still has a hard bound. Keep the newest
        // event (in particular Stop/Interrupt) so the loss is exported as a gap,
        // rather than retaining a full journal that can never be finalized.
        while (state.events.length > 128 || bytes() > 42000) {
          state.events.shift();
          state.gap = true;
        }
        return this.encrypt(state);
      });
    });
  }
  /** Atomic claim prevents concurrent terminal hooks replaying the same journal. */
  async take(event: Event): Promise<TurnState | undefined> {
    const file = this.file(event);
    return withFileLock(file + ".journal", async () => {
      if (await readJson(file + ".done")) return;
      const old = await readJson<Envelope>(file);
      if (!old) return;
      const state = this.decrypt(old);
      await writeProtected(
        file + ".done",
        JSON.stringify({ finalizedAt: Date.now() }),
      );
      await fs.unlink(file);
      return state;
    });
  }
  async takeSession(
    session: string,
    ready?: (state: TurnState) => Promise<boolean>,
  ): Promise<TurnState[]> {
    await ensurePrivateDirectory(this.dir);
    const result: TurnState[] = [];
    const prefix = this.prefix(session) + "-";
    for (const name of (await fs.readdir(this.dir))
      .filter((n) => n.startsWith(prefix) && n.endsWith(".json"))
      .slice(0, 16)) {
      const envelope = await readJson<Envelope>(join(this.dir, name));
      if (!envelope) continue;
      const pending = this.decrypt(envelope);
      const event = pending.events[0];
      if (!event || (ready && !(await ready(pending)))) continue;
      const state = await this.take(event);
      if (state) result.push(state);
    }
    return result;
  }
  async expire(): Promise<void> {
    await ensurePrivateDirectory(this.dir);
    // Fixed bounds: at most 1024 files, each <=64 KiB; content expires after 24 hours.
    for (const name of (await fs.readdir(this.dir)).slice(0, 1024)) {
      if (!/^[a-f0-9]{24}-[a-f0-9]{24}\.json(?:\.done)?$/.test(name)) continue;
      const p = join(this.dir, name),
        s = await fs.lstat(p);
      if (
        !s.isSymbolicLink() &&
        s.isFile() &&
        s.mtimeMs < Date.now() - 86400000
      )
        await fs.unlink(p);
    }
  }
}
