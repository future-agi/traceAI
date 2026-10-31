import { dirname, join } from "node:path";
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { opaqueId } from "@traceai/agent-core";
import type {
  AdapterContext,
  LocalObserver,
  ObserverServices,
} from "../../adapters.js";
import { AgentError, atomicProtected, readProtected } from "../../security/files.js";
import { CodexCodec, CodexGraph } from "./codec.js";
import { RolloutReader } from "./reader.js";
import { EVENTS, registration } from "./plugin.js";
interface Active {
  reader: RolloutReader;
  quietSince: number;
  touched: number;
  sent: Set<string>;
  coordinate: string;
  closed: boolean;
  session: string;
}
/** Bounded off-hook worker. Only explicitly announced active files are read.
 * Restart rereads source anchors; deterministic admission makes replay safe. */
export class CodexObserver implements LocalObserver {
  private active = new Map<string, Active>();
  private graph = new CodexGraph();
  private timer: NodeJS.Timeout;
  private running?: Promise<void>;
  private stopped = false;
  private held = new Set<string>();
  private callbacks = 0;
  private admitted = 0;
  private readonly recoveryFile: string;
  private readonly recoveryKey: Buffer;
  private restoring = false;
  /** References are encrypted at rest: even an ordinary source path can contain
   * sensitive home or project names. This is not a transcript copy. */
  private persist(): void {
    const refs = [...this.active.values()].map((a) => ({
      path: a.reader.path,
      session: a.session,
      touched: a.touched,
      closed: a.closed,
    }));
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.recoveryKey, iv);
    const ciphertext = Buffer.concat([cipher.update(JSON.stringify(refs), "utf8"), cipher.final()]);
    atomicProtected(this.recoveryFile, JSON.stringify({version:1, iv:iv.toString("hex"), tag:cipher.getAuthTag().toString("hex"), data:ciphertext.toString("base64")}));
  }
  private restore(): void {
    let raw: Buffer;
    try { raw = readProtected(this.recoveryFile, 65536); }
    catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") return;
      this.held.add("HELD_SOURCE_RECOVERY"); return;
    }
    try {
      const saved = JSON.parse(raw.toString("utf8"));
      if (saved.version !== 1 || !/^[a-f0-9]{24}$/.test(saved.iv) || !/^[a-f0-9]{32}$/.test(saved.tag) || typeof saved.data !== "string" || saved.data.length > 64000)
        throw new Error("invalid receipt");
      const decipher = createDecipheriv("aes-256-gcm", this.recoveryKey, Buffer.from(saved.iv,"hex"));
      decipher.setAuthTag(Buffer.from(saved.tag,"hex"));
      const refs = JSON.parse(Buffer.concat([decipher.update(Buffer.from(saved.data,"base64")),decipher.final()]).toString("utf8"));
      if (!Array.isArray(refs) || refs.length > 16) throw new Error("invalid references");
      const r = registration(this.context.socketPath);
      if (!r.enabled || !this.context.transcriptConsent) return;
      this.restoring = true;
      for (const ref of refs) {
        if (!ref || typeof ref.path !== "string" || ref.path.length > 4096 || typeof ref.session !== "string" || !/^[a-zA-Z0-9_-]{1,256}$/.test(ref.session) || !Number.isSafeInteger(ref.touched) || ref.touched > Date.now() + 60000 || Date.now() - ref.touched > 86_400_000 || typeof ref.closed !== "boolean")
          throw new Error("invalid reference");
        this.accept({bundle:r.fingerprint,event:ref.closed ? "SessionEnd":"SessionStart",session:ref.session,path:ref.path});
        const key = opaqueId(this.services.normalize.identityKey,"codex-local-stream",ref.session + "\n" + ref.path);
        this.active.get(key)!.touched = ref.touched;
      }
    } catch {
      this.active.clear();
      this.held.add("HELD_SOURCE_RECOVERY");
    } finally { this.restoring = false; }
  }
  constructor(
    private readonly context: AdapterContext,
    private readonly services: ObserverServices,
  ) {
    this.recoveryKey = createHash("sha256").update("codex-source-recovery-v1\0").update(services.normalize.identityKey).update(context.routeRef).digest();
    this.recoveryFile = join(dirname(context.socketPath), "codex", "announced-" + createHash("sha256").update(context.routeRef).digest("hex") + ".json");
    this.restore();
    this.timer = setInterval(() => {
      if (!this.running && !this.stopped)
        this.running = this.tick()
          .catch(() => {
            this.held.add("LOCAL_UNAVAILABLE");
          })
          .finally(() => {
            this.running = undefined;
          });
    }, 250);
  }
  accept(input: unknown): void {
    if (this.stopped || !this.context.transcriptConsent)
      throw new AgentError("HELD_DISABLED", 3);
    const r = registration(this.context.socketPath);
    if (!r.enabled) throw new AgentError("HELD_DISABLED", 3);
    const i = input as any;
    if (
      !i ||
      i.bundle !== r.fingerprint ||
      !EVENTS.includes(i.event) ||
      typeof i.session !== "string" ||
      !/^[a-zA-Z0-9_-]{1,256}$/.test(i.session)
    )
      throw new AgentError("INVALID_OBSERVER_EVENT", 2);
    this.callbacks++;
    // No per-event disk journal. One bounded activation receipt, no source IDs/text.
    if (this.callbacks === 1)
      atomicProtected(
        join(dirname(this.context.socketPath), "codex", "activation.json"),
        JSON.stringify({
          activated: true,
          contract: 1,
          fingerprint: r.fingerprint,
        }),
      );
    if (i.path === null) {
      this.held.add("EPHEMERAL_TRANSCRIPT_UNAVAILABLE");
      return;
    }
    if (typeof i.path !== "string" || i.path.length > 4096)
      throw new AgentError("UNSAFE_SOURCE_PATH", 3);
    const key = opaqueId(
      this.services.normalize.identityKey,
      "codex-local-stream",
      i.session + "\n" + i.path,
    );
    let a = this.active.get(key);
    if (!a) {
      if (this.active.size >= 16) {
        this.held.add("ACTIVE_SOURCE_LIMIT");
        throw new AgentError("ACTIVE_SOURCE_LIMIT", 4);
      }
      a = {
        reader: new RolloutReader(
          join(r.home, "sessions"),
          i.path,
          new CodexCodec(this.services.normalize, i.session, this.graph),
        ),
        quietSince: Date.now(),
        touched: Date.now(),
        sent: new Set(),
        coordinate: "0",
        closed: false,
        session: i.session,
      };
      this.active.set(key, a);
    } else if (a.reader.path !== i.path)
      throw new AgentError("HELD_ROTATED_SOURCE", 3);
    a.touched = Date.now();
    if (i.event === "SessionEnd") a.closed = true;
    if (!this.restoring) {
      try { this.persist(); }
      catch (e) { this.active.delete(key); throw e; }
    }
  }
  private async tick(): Promise<void> {
    if (!registration(this.context.socketPath).enabled) {
      this.active.clear();
      this.persist();
      return;
    }
    for (const [key, a] of [...this.active].slice(0, 4)) {
      if (this.stopped) break;
      this.active.delete(key);
      this.active.set(key, a);
      try {
        // Bound decoding at a terminal window, but retain the normal quiet
        // grace before admitting it. Continuous writes never force an early
        // immutable snapshot just to free codec memory.
        if (a.reader.codec.needsDrain && Date.now() - a.quietSince < 2000) continue;
        const read = a.reader.read();
        a.coordinate = read.coordinate;
        if (read.changed) a.quietSince = Date.now();
        if ((!read.batchFull && !read.caughtUp) || Date.now() - a.quietSince < 2000) continue;
        for (const fact of a.reader.codec.facts()) {
          if (this.stopped) break;
          if (a.sent.has(fact.sourceWorkId)) continue;
          try {
            await this.services.admit(fact, {
              stream: key,
              coordinate: a.coordinate,
            });
          } catch (e) {
            // A previously accepted immutable snapshot cannot be revised, but
            // must not censor later independently proven requests or turns.
            if (
              !(e instanceof AgentError) ||
              e.code !== "HELD_IMMUTABLE_CONFLICT"
            )
              throw e;
            this.held.add(e.code);
            a.sent.add(fact.sourceWorkId);
            continue;
          }
          a.sent.add(fact.sourceWorkId);
          this.admitted++;
        }
        if (this.stopped) return; // Never retire facts that were not admitted.
        a.reader.codec.release();
        a.sent.clear();
        // No process-liveness inference. Quiet unfinished turns remain unavailable;
        // readers expire without emitting a made-up successful terminal span.
        if (Date.now() - a.touched > 86_400_000) {
          this.held.add("SOURCE_EXPIRED_UNKNOWN_GAP");
          this.active.delete(key);
          this.persist();
          continue;
        }
        if (a.reader.codec.pendingAncestry) {
          this.held.add("HELD_SPAWN_CONTEXT");
          continue;
        }
        if (a.closed && read.caughtUp) {
          if (a.reader.codec.pendingTurns > 0)
            this.held.add("SOURCE_ENDED_WITHOUT_TERMINAL");
          this.active.delete(key);
          this.persist();
        }
      } catch (e) {
        if (
          (e as NodeJS.ErrnoException).code === "ENOENT" &&
          Date.now() - a.touched < 5000
        )
          continue;
        this.held.add(e instanceof AgentError ? e.code : "SOURCE_UNAVAILABLE");
        this.active.delete(key);
        this.persist();
      }
    }
  }
  status(): object {
    return {
      callbacks: this.callbacks,
      admitted: this.admitted,
      active: this.active.size,
      reasons: [...this.held].slice(0, 16),
      timing: "reconstructed-not-hook-paired",
      persistence: "unknown",
    };
  }
  async stop(): Promise<void> {
    this.stopped = true;
    clearInterval(this.timer);
    await this.running;
    this.active.clear();
  }
}
