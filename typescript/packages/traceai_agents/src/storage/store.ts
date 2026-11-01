import Database from "better-sqlite3";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { chmodSync } from "node:fs";
import {
  minimize,
  planId,
  spanId,
  token,
  type CanonicalFact,
  type Policy,
} from "@traceai/agent-core";
import { AgentError } from "../security/files.js";
import {
  decodeTraces,
  encodeFacts,
  encodeTraceObject,
} from "../export/otlp.js";
import { sameDestination, validateBinding, type Binding } from "../export/endpoint.js";
import { measure, preflight, reserve, STORAGE_LIMITS } from "./budget.js";
export interface Route {
  id: string;
  binding: Binding;
  policy: Policy;
  enabled: boolean;
  epoch: number;
}
interface Row {
  id: number;
  route: string;
  work: string;
  bytes: Buffer;
  encrypted: number;
  epoch: number;
  attempts: number;
  created: number;
  state: string;
}
// Silence is not evidence of process death. The inactivity window is retained
// only for legacy callers; terminal observations alone make roots exportable.
export const OBSERVED_TURN_WINDOW_MS = 15 * 60_000;
export const TERMINAL_GRACE_MS = 2_000;
interface TurnWindow {
  start_ns: string;
  end_ns: string;
  deadline: number;
  epoch: number;
  terminal: string | null;
}
const digest = (bytes: Buffer): string =>
  createHash("sha256").update(bytes).digest("hex");
/** Frozen comparison v1: runtime packaging is not source semantics. Keep all
 * source timing, topology, usage, content and reconstruction fields comparable.
 * This representation is only a receipt; the first wire bytes are never changed. */
function semanticHash(bytes: Buffer): string {
  const request = decodeTraces(bytes);
  for (const resource of request.resourceSpans ?? []) {
    resource.resource.attributes = resource.resource.attributes.filter(
      (a: { key: string }) => a.key !== "service.version",
    );
    for (const scope of resource.scopeSpans ?? []) delete scope.scope.version;
  }
  return "semantic-v1:" + digest(encodeTraceObject(request));
}
/** Internal storage-child class. Runtime clients only use RPC; never share this connection. */
export class Spool {
  private db: Database.Database;
  private paused: string | undefined;
  // A prior runtime may already have expired receipts but left turn windows.
  private gcOrphans = true;
  private usageCursor = 0;
  private planCursor = 0;
  private turnCursor = 0;
  private gcRestartPending = false;
  constructor(
    private readonly root: string,
    private readonly runtimeVersion: string,
    exclusiveOwner = false,
  ) {
    process.umask(0o077);
    reserve(root);
    preflight(root);
    this.db = new Database(join(root, "spool.db"), {
      timeout: exclusiveOwner ? 500 : 10,
    });
    try {
      chmodSync(join(root, "spool.db"), 0o600);
      // In WAL mode EXCLUSIVE retains the OS lock between transactions. The
      // storage child is the sole connection and process death releases ownership;
      // no PID files, leases, stale-directory deletion, or second lock database.
      if (exclusiveOwner) {
        this.db.pragma("locking_mode=EXCLUSIVE");
        // Acquire ownership before reading schema/header pragmas. Competing
        // connections must not retain shared locks then race to upgrade them.
        this.db.exec("BEGIN EXCLUSIVE; COMMIT;");
      }
      if (
        this.db.prepare("SELECT sqlite_version() v").get() &&
        (this.db.prepare("SELECT sqlite_version() v").get() as any).v !==
          "3.50.4"
      ) {
        throw new AgentError("UNQUALIFIED_SQLITE");
      }
      this.db.pragma("page_size=4096");
      if (this.db.pragma("page_size", { simple: true }) !== 4096)
        throw new AgentError("UNQUALIFIED_PAGE_SIZE");
      this.db.pragma("max_page_count=16384");
      if (this.db.pragma("max_page_count", { simple: true }) !== 16384)
        throw new AgentError("UNQUALIFIED_PAGE_LIMIT");
      this.db.pragma("journal_mode=WAL");
      if (exclusiveOwner) this.db.exec("BEGIN EXCLUSIVE; COMMIT;");
      this.db.pragma("synchronous=FULL");
      this.db.pragma("wal_autocheckpoint=0");
      this.db.pragma("journal_size_limit=0");
      this.db.pragma("cache_spill=OFF");
      this.db.pragma("cache_size=16384");
      this.db.pragma("mmap_size=0");
      this.db.pragma("temp_store=MEMORY");
      this.db.pragma("busy_timeout=10");
      const existingVersion = this.db.pragma("user_version", { simple: true });
      if (![0, 1, 2, 3, 4].includes(Number(existingVersion)))
        throw new AgentError("HELD_STORAGE_SCHEMA");
      this.write(() =>
        this.db.exec(`
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY,config TEXT NOT NULL,enabled INTEGER NOT NULL,epoch INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS plans (binding TEXT NOT NULL,id TEXT NOT NULL,choice TEXT NOT NULL,PRIMARY KEY(binding,id));
      CREATE TABLE IF NOT EXISTS usage (binding TEXT NOT NULL,id TEXT NOT NULL,work TEXT NOT NULL,PRIMARY KEY(binding,id));
      CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY,route TEXT NOT NULL,binding TEXT NOT NULL,work TEXT NOT NULL,
        trace TEXT NOT NULL,span TEXT NOT NULL,hash TEXT NOT NULL,bytes BLOB,encrypted INTEGER NOT NULL,epoch INTEGER NOT NULL,
        state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at INTEGER NOT NULL DEFAULT 0,created INTEGER NOT NULL,
        plan TEXT,legacy_unknown INTEGER NOT NULL DEFAULT 0,UNIQUE(binding,work),UNIQUE(binding,trace,span));
      CREATE INDEX IF NOT EXISTS pending ON records(route,state,next_at,id);
      CREATE INDEX IF NOT EXISTS terminal_age ON records(created,id) WHERE bytes IS NULL;
      CREATE TABLE IF NOT EXISTS observed_turns (binding TEXT NOT NULL,work TEXT NOT NULL,route TEXT NOT NULL,
        epoch INTEGER NOT NULL,start_ns TEXT NOT NULL,end_ns TEXT NOT NULL,deadline INTEGER NOT NULL,PRIMARY KEY(binding,work));
      CREATE TABLE IF NOT EXISTS cursors (route TEXT NOT NULL,stream TEXT NOT NULL,coordinate TEXT NOT NULL,
        updated INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(route,stream));
    `),
      );
      const version = this.db
        .prepare("SELECT value FROM meta WHERE key=?")
        .get("schema") as { value: string } | undefined;
      if (version && !["1", "2", "3", "4"].includes(version.value))
        throw new AgentError("HELD_STORAGE_SCHEMA");
      if (Number(existingVersion) < 4 || version?.value !== "4")
        this.write(() => {
          // Legacy receipts retain their accounting plan until the last receipt expires.
          const columns = (table: string): Set<string> =>
            new Set((this.db.pragma(`table_info(${table})`) as { name: string }[]).map((row) => row.name));
          if (!columns("records").has("plan"))
            this.db.exec("ALTER TABLE records ADD COLUMN plan TEXT");
          if (!columns("cursors").has("updated")) {
            this.db.exec("ALTER TABLE cursors ADD COLUMN updated INTEGER NOT NULL DEFAULT 0");
            this.db.prepare("UPDATE cursors SET updated=?").run(Date.now());
          }
          if (!columns("records").has("legacy_unknown")) {
            this.db.exec("ALTER TABLE records ADD COLUMN legacy_unknown INTEGER NOT NULL DEFAULT 0");
            this.db.exec("UPDATE records SET legacy_unknown=1 WHERE plan IS NULL");
          }
          if (!columns("observed_turns").has("terminal"))
            this.db.exec("ALTER TABLE observed_turns ADD COLUMN terminal TEXT");
          this.db.prepare("INSERT OR REPLACE INTO meta VALUES (?,?)").run("schema", "4");
          this.db.pragma("user_version=4");
        });
      this.write(() => this.db.exec(`
        CREATE INDEX IF NOT EXISTS record_plans ON records(binding,plan);
        CREATE INDEX IF NOT EXISTS legacy_plan_refs ON records(binding) WHERE legacy_unknown=1;
      `));
    } catch (e) {
      this.db.close();
      if (exclusiveOwner && (e as { code?: string }).code === "SQLITE_BUSY")
        throw new AgentError("RUNTIME_OWNERSHIP_CONFLICT", 6);
      throw e;
    }
  }
  private checkpoint(): void {
    const row = (this.db.pragma("wal_checkpoint(TRUNCATE)") as any[])[0];
    if (!row || row.busy !== 0 || row.log !== 0 || row.checkpointed !== 0) {
      this.paused = "PAUSED_CHECKPOINT";
      throw new AgentError(this.paused);
    }
    this.paused = undefined;
  }
  private write<T>(fn: () => T): T {
    this.checkpoint();
    preflight(this.root);
    let value: T;
    try {
      value = this.db.transaction(fn).immediate();
    } catch (e) {
      this.paused = e instanceof AgentError ? e.code : "PAUSED_STORAGE";
      throw e instanceof AgentError ? e : new AgentError("PAUSED_STORAGE");
    }
    // A committed fact is durable even if reset fails. No subsequent write is admitted.
    try {
      this.checkpoint();
      preflight(this.root);
    } catch (e) {
      this.paused = e instanceof AgentError ? e.code : "PAUSED_STORAGE";
    }
    return value;
  }
  validateRoute(route: Route): void {
    if (
      !token(route.id) ||
      !Number.isSafeInteger(route.epoch) ||
      route.epoch < 1
    )
      throw new AgentError("INVALID_ROUTE", 2);
    validateBinding(route.binding);
    // Content capture remains held until per-policy protected-key qualification.
    if (route.policy.categories.length || route.policy.ceiling.length)
      throw new AgentError("HELD_CONTENT_QUALIFICATION", 3);
    const old = this.db
      .prepare("SELECT config,enabled,epoch FROM routes WHERE id=?")
      .get(route.id) as any;
    if (old) {
      const prior = JSON.parse(old.config);
      // Contract approval may be rolled back by a failed config transaction.
      // Only the export eligibility changes; the destination identity never does.
      if (!sameDestination(prior.binding, route.binding))
        throw new AgentError("HELD_DESTINATION_CHANGED", 3);
      if (
        route.epoch < old.epoch ||
        (route.enabled && !old.enabled && route.epoch === old.epoch) ||
        (route.epoch === old.epoch &&
          JSON.stringify(prior.policy) !== JSON.stringify(route.policy))
      )
        throw new AgentError("HELD_POLICY", 3);
    }
  }
  route(route: Route): void {
    this.validateRoute(route);
    this.write(() => {
      const prior = this.db
        .prepare("SELECT enabled,epoch FROM routes WHERE id=?")
        .get(route.id) as { enabled: number; epoch: number } | undefined;
      this.db
        .prepare(
          "INSERT INTO routes VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,enabled=excluded.enabled,epoch=excluded.epoch",
        )
        .run(
          route.id,
          JSON.stringify(route),
          Number(route.enabled),
          route.epoch,
        );
      // Re-enable transfers retained capture once; pending roots from an old
      // generation cannot accept a stop, so export them as incomplete.
      if (prior?.enabled === 0 && route.enabled && route.epoch > prior.epoch) {
        this.db
          .prepare(
            "UPDATE records SET state='queued',epoch=?,next_at=0 WHERE route=? AND epoch<? AND state='pending-turn'",
          )
          .run(route.epoch, route.id, route.epoch);
        this.db
          .prepare(
            "UPDATE records SET epoch=?,next_at=0 WHERE route=? AND epoch<? AND bytes IS NOT NULL AND state IN ('queued','ambiguous','held','held-auth','split')",
          )
          .run(route.epoch, route.id, route.epoch);
      }
    });
  }
  getRoute(id: string): Route {
    const row = this.db
      .prepare("SELECT config,enabled,epoch FROM routes WHERE id=?")
      .get(id) as any;
    if (!row) throw new AgentError("UNKNOWN_ROUTE", 3);
    return {
      ...JSON.parse(row.config),
      enabled: row.enabled === 1,
      epoch: row.epoch,
    };
  }
  admit(
    routeId: string,
    input: CanonicalFact,
    cursor?: { stream: string; coordinate: string },
    observed = false,
    expectedEpoch?: number,
    observedTurns = false,
  ): { receipt_id: string; duplicate: boolean } {
    const route = this.getRoute(routeId);
    if (!route.enabled) throw new AgentError("HELD_DISABLED", 3);
    if (expectedEpoch !== undefined && expectedEpoch !== route.epoch)
      throw new AgentError("STALE_CAPTURE_EPOCH", 3);
    const fact = minimize(input, route.policy);
    if (observed) {
      // Hooks without wall timestamps have an estimated callback interval. Freeze its
      // first receipt clock with the fact in the SAME transaction, so duplicate hooks
      // do not become changed immutable spans merely because they arrived later.
      if (
        cursor ||
        fact.source !== "hook" ||
        !["unknown", "estimated"].includes(fact.timing) ||
        fact.startNs !== "0" ||
        BigInt(fact.endNs) > 86_400_000_000_000n
      )
        throw new AgentError("INVALID_OBSERVATION_CLOCK", 2);
      const old = this.db
        .prepare("SELECT coordinate FROM cursors WHERE route=? AND stream=?")
        .get(routeId, fact.sourceWorkId) as { coordinate: string } | undefined;
      const end =
        old?.coordinate ?? (BigInt(Date.now()) * 1_000_000n).toString();
      fact.startNs = (BigInt(end) - BigInt(fact.endNs)).toString();
      fact.endNs = end;
      cursor = { stream: fact.sourceWorkId, coordinate: end };
    }
    if (observedTurns && !observed)
      throw new AgentError("INVALID_OBSERVATION_CLOCK", 2);
    // Finalization precedes admission, so an overdue/finished turn cannot acquire
    // new children or change its already immutable root. Replays still dedupe.
    if (observedTurns) this.finalizeTurns(routeId);
    return this.write(() => {
      const terminal = observedTurns
        ? this.observeTurn(route, fact, cursor)
        : undefined;
      return terminal ?? this.insertFact(route, fact, cursor);
    });
  }
  private checkQuota(routeId: string, size: number, replacing?: number): void {
    const global = (
      this.db
        .prepare(
          "SELECT coalesce(sum(length(bytes)),0) n FROM records WHERE id != ?",
        )
        .get(replacing ?? -1) as any
    ).n;
    const local = (
      this.db
        .prepare(
          "SELECT coalesce(sum(length(bytes)),0) n FROM records WHERE route=? AND id != ?",
        )
        .get(routeId, replacing ?? -1) as any
    ).n;
    if (
      global + size > STORAGE_LIMITS.logical ||
      local + size > STORAGE_LIMITS.route
    )
      throw new AgentError("PAUSED_LOGICAL_QUOTA");
  }
  private insertFact(
    route: Route,
    fact: CanonicalFact,
    cursor?: { stream: string; coordinate: string },
    state = "queued",
    allowPending = false,
  ): { receipt_id: string; duplicate: boolean } {
    const routeId = route.id;
    const bytes = encodeFacts([fact], route.binding, this.runtimeVersion);
    if (bytes.length > 262_144) throw new AgentError("OVERSIZE_SPAN", 2);
    const hash = semanticHash(bytes);
    if (
      cursor &&
      (!/^[a-f0-9]{64}$/.test(cursor.stream) ||
        !/^(0|[1-9][0-9]{0,19})$/.test(cursor.coordinate))
    )
      throw new AgentError("INVALID_CURSOR", 2);
    const old = this.db
      .prepare(
        "SELECT id,hash,work,state,bytes FROM records WHERE binding=? AND (work=? OR (trace=? AND span=?))",
      )
      .get(
        route.binding.id,
        fact.sourceWorkId,
        fact.traceId,
        fact.spanId,
      ) as any;
    if (old) {
      const equal =
        old.hash === hash ||
        (/^[a-f0-9]{64}$/.test(old.hash) &&
          (old.bytes
            ? semanticHash(old.bytes) === hash
            : old.hash === digest(bytes) ||
              old.hash ===
                digest(encodeFacts([fact], route.binding, "0.1.0"))));
      if (
        old.work !== fact.sourceWorkId ||
        ((old.state !== "pending-turn" || !allowPending) && !equal)
      )
        throw new AgentError("HELD_IMMUTABLE_CONFLICT", 3);
      // Upgrade legacy wire-only receipts without altering already exportable bytes.
      if (
        equal &&
        old.hash !== hash &&
        !(old.state === "pending-turn" && allowPending)
      )
        this.db
          .prepare("UPDATE records SET hash=? WHERE id=?")
          .run(hash, old.id);
    }
    let id = old?.id;
    if (!old || (old.state === "pending-turn" && allowPending)) {
      this.checkQuota(routeId, bytes.length, old?.id);
      const u = fact.usage;
      if (u && u.contribution !== "none") {
        if (u.planId !== planId(fact.turnId))
          throw new AgentError("HELD_USAGE_PLAN", 3);
        const plan = this.db
          .prepare("SELECT choice FROM plans WHERE binding=? AND id=?")
          .get(route.binding.id, u.planId) as any;
        if (plan && plan.choice !== u.contribution)
          throw new AgentError("HELD_USAGE_PLAN", 3);
        this.db
          .prepare("INSERT OR IGNORE INTO plans VALUES (?,?,?)")
          .run(route.binding.id, u.planId, u.contribution);
        for (const usage of u.factIds) {
          const existing = this.db
            .prepare("SELECT work FROM usage WHERE binding=? AND id=?")
            .get(route.binding.id, usage) as any;
          if (existing && existing.work !== fact.sourceWorkId)
            throw new AgentError("HELD_USAGE_CONFLICT", 3);

          this.db
            .prepare("INSERT OR IGNORE INTO usage VALUES (?,?,?)")
            .run(route.binding.id, usage, fact.sourceWorkId);
        }
      }
      if (old) {
        this.db
          .prepare("UPDATE records SET hash=?,bytes=?,state=? WHERE id=?")
          .run(hash, bytes, state, old.id);
      } else
        id = this.db
          .prepare(
            "INSERT INTO records(route,binding,work,trace,span,hash,bytes,encrypted,epoch,state,created,plan) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
          )
          .run(
            routeId,
            route.binding.id,
            fact.sourceWorkId,
            fact.traceId,
            fact.spanId,
            hash,
            bytes,
            0,
            route.epoch,
            state,
            Date.now(),
            u && u.contribution !== "none" ? u.planId : null,
          ).lastInsertRowid;
    }
    if (cursor)
      this.db
        .prepare(
          "INSERT INTO cursors(route,stream,coordinate,updated) VALUES (?,?,?,?) ON CONFLICT(route,stream) DO UPDATE SET coordinate=excluded.coordinate,updated=excluded.updated",
        )
        .run(routeId, cursor.stream, cursor.coordinate, Date.now());
    return {
      receipt_id: String(id),
      duplicate: !!old && old.state !== "pending-turn",
    };
  }
  /** Pending parents are never leased. Their bytes and the child commit together. */
  private observeTurn(
    route: Route,
    fact: CanonicalFact,
    cursor?: { stream: string; coordinate: string },
  ): { receipt_id: string; duplicate: boolean } | undefined {
    if (
      !["turn", "tool"].includes(fact.unit) ||
      fact.source !== "hook" ||
      fact.traceId.length !== 32 ||
      (fact.unit === "tool" && fact.parentSpanId !== spanId(fact.turnId))
    )
      throw new AgentError("INVALID_OBSERVED_TURN", 2);
    const parent = this.db
      .prepare("SELECT state FROM records WHERE binding=? AND work=?")
      .get(route.binding.id, fact.turnId) as { state: string } | undefined;
    const window = this.db
      .prepare("SELECT * FROM observed_turns WHERE binding=? AND work=?")
      .get(route.binding.id, fact.turnId) as TurnWindow | undefined;
    if (window && window.epoch !== route.epoch)
      throw new AgentError("STALE_CAPTURE_EPOCH", 3);
    const old = this.db
      .prepare("SELECT id FROM records WHERE binding=? AND work=?")
      .get(route.binding.id, fact.sourceWorkId);
    if (parent && parent.state !== "pending-turn") {
      if (!old) throw new AgentError("HELD_TURN_FINALIZED", 3);
      // Terminal replay keeps its originally bounded start; a late stop after a
      // timeout remains a conflict instead of changing incomplete into success.
      if (fact.unit === "turn" && window) {
        fact.startNs = window.start_ns;
        fact.endNs = window.end_ns;
        fact.timing =
          window.start_ns === window.end_ns ? "unknown" : "estimated";
      }
      return;
    }
    const start =
      window && BigInt(window.start_ns) < BigInt(fact.startNs)
        ? window.start_ns
        : fact.startNs;
    const end =
      window && BigInt(window.end_ns) > BigInt(fact.endNs)
        ? window.end_ns
        : fact.endNs;
    const terminal =
      fact.unit === "turn" ? JSON.stringify(fact) : (window?.terminal ?? null);
    if (
      fact.unit === "turn" &&
      window?.terminal &&
      window.terminal !== terminal
    )
      throw new AgentError("HELD_IMMUTABLE_CONFLICT", 3);
    // Keep the first terminal observation independently of the mutable local
    // interval. Later tools may widen the root, but cannot replace its outcome.
    const source = terminal ? (JSON.parse(terminal) as CanonicalFact) : fact;
    const root = minimize(
      {
        ...source,
        sourceWorkId: fact.turnId,
        spanId: spanId(fact.turnId),
        parentSpanId: undefined,
        unit: "turn",
        tool: undefined,
        outcome: terminal ? source.outcome : "incomplete",
        model: undefined,
        provider: undefined,
        usage: undefined,
        content: undefined,
        causedBy: undefined,
        links: undefined,
        inputCompleteness: "unavailable",
        outputCompleteness: "unavailable",
        incomplete: true,
        startNs: start,
        endNs: end,
        timing: start === end ? "unknown" : "estimated",
      },
      route.policy,
    );
    const receipt = this.insertFact(
      route,
      root,
      fact.unit === "turn" ? cursor : undefined,
      "pending-turn",
      true,
    );
    const deadline = window?.terminal
      ? window.deadline
      : fact.unit === "turn"
        ? Date.now() + TERMINAL_GRACE_MS
        : 0;
    this.db
      .prepare(
        `INSERT INTO observed_turns VALUES (?,?,?,?,?,?,?,?)
      ON CONFLICT(binding,work) DO UPDATE SET start_ns=excluded.start_ns,end_ns=excluded.end_ns,
      deadline=excluded.deadline,terminal=excluded.terminal`,
      )
      .run(
        route.binding.id,
        fact.turnId,
        route.id,
        route.epoch,
        start,
        end,
        deadline,
        terminal,
      );
    if (fact.unit === "turn")
      return { ...receipt, duplicate: !!window?.terminal };
  }
  /** Only an observed stop can close a root; a quiet live process is not dead. */
  finalizeTurns(routeId: string): number {
    const route = this.getRoute(routeId);
    if (!route.enabled) return 0;
    const due = this.db
      .prepare(
        `SELECT r.id FROM records r JOIN observed_turns t
      ON r.binding=t.binding AND r.work=t.work WHERE r.route=? AND r.epoch=?
      AND r.state='pending-turn' AND t.terminal IS NOT NULL AND t.deadline<=? LIMIT 128`,
      )
      .all(routeId, route.epoch, Date.now()) as {
      id: number;
    }[];
    if (!due.length) return 0;
    return this.write(() => {
      const update = this.db.prepare(
        "UPDATE records SET state='queued' WHERE id=? AND state='pending-turn'",
      );
      for (const row of due) update.run(row.id);
      return due.length;
    });
  }
  next(routeId: string, includeDisabled = false): Row | undefined {
    const route = this.getRoute(routeId);
    if (!route.enabled && !includeDisabled) return undefined;
    this.finalizeTurns(routeId);
    if (
      this.db
        .prepare("SELECT value FROM meta WHERE key=?")
        .get("hold:" + route.binding.id)
    )
      return undefined;
    return this.db
      .prepare(
        "SELECT id,route,work,bytes,encrypted,epoch,attempts,created,state FROM records WHERE route=? AND (state='queued' OR (state='ambiguous' AND ?)) AND epoch=? AND next_at<=? AND created>? ORDER BY id LIMIT 1",
      )
      .get(
        routeId,
        Number(route.binding.backendContract === "immutable-agent-v1"),
        route.epoch,
        Date.now(),
        Date.now() - 86_400_000,
      ) as Row | undefined;
  }
  complete(
    id: number,
    epoch: number,
    result: {
      state: "accepted" | "retry" | "held" | "ambiguous" | "split";
      code: string;
      nextAt?: number;
    },
  ): void {
    this.write(() => {
      const row = this.db
        .prepare("SELECT route,epoch,state FROM records WHERE id=?")
        .get(id) as any;
      if (
        !row ||
        row.epoch !== epoch ||
        this.getRoute(row.route).epoch !== epoch
      )
        throw new AgentError("STALE_LEASE", 3);
      this.db
        .prepare(
          "UPDATE records SET state=?,bytes=CASE WHEN ? THEN NULL ELSE bytes END,attempts=attempts+1,next_at=? WHERE id=?",
        )
        .run(
          result.code === "HELD_AUTH"
            ? "held-auth"
            : result.state === "retry"
              ? row.state === "ambiguous"
                ? "ambiguous"
                : "queued"
              : result.state,
          result.state === "accepted" ? 1 : 0,
          result.nextAt ?? 0,
          id,
        );
      if (["HELD_AUTH", "HELD_REDIRECT", "HELD_TLS"].includes(result.code)) {
        const binding = this.getRoute(row.route).binding.id;
        this.db
          .prepare(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          )
          .run("hold:" + binding, result.code);
      }
    });
  }
  releaseAuth(routeId: string): number {
    const binding = this.getRoute(routeId).binding.id;
    return this.write(() => {
      const changes = this.db
        .prepare(
          "UPDATE records SET state='queued',next_at=0 WHERE id IN (SELECT id FROM records WHERE binding=? AND state='held-auth' LIMIT 128)",
        )
        .run(binding).changes;
      this.db
        .prepare("DELETE FROM meta WHERE key=? AND value='HELD_AUTH'")
        .run("hold:" + binding);
      return changes;
    });
  }
  disable(routeId: string): void {
    this.write(() =>
      this.db
        .prepare(
          "UPDATE routes SET enabled=0,epoch=epoch+1 WHERE id=? AND enabled=1",
        )
        .run(routeId),
    );
  }
  purge(routeId: string): number {
    if (this.getRoute(routeId).enabled)
      throw new AgentError("FENCE_REQUIRED", 3);
    // Keep immutable identity/accounting receipts; purge cannot mint new work on replay.
    return this.write(() => {
      const result = this.db
        .prepare(
          "UPDATE records SET bytes=NULL,state='purged' WHERE id IN (SELECT id FROM records WHERE route=? AND bytes IS NOT NULL LIMIT 128)",
        )
        .run(routeId);
      this.db
        .prepare(
          "DELETE FROM cursors WHERE rowid IN (SELECT rowid FROM cursors WHERE route=? LIMIT 128)",
        )
        .run(routeId);
      const turns = this.db
        .prepare(
          "DELETE FROM observed_turns WHERE rowid IN (SELECT rowid FROM observed_turns WHERE route=? LIMIT 128)",
        )
        .run(routeId).changes;
      return Math.max(result.changes, turns);
    });
  }
  /** Examine at most `limit` candidates, not `limit` successful deletions.
   * Persisting the cursor is unnecessary: a restart begins a new bounded
   * sweep, and a live process advances until it reaches the orphan tail. */
  private collectOrphans(
    table: "usage" | "plans" | "observed_turns",
    limit: number,
  ): boolean {
    const cursor = table === "usage"
      ? this.usageCursor
      : table === "plans" ? this.planCursor : this.turnCursor;
    const rows = this.db.prepare(
      `SELECT rowid FROM ${table} WHERE rowid>? ORDER BY rowid LIMIT ?`,
    ).all(cursor, limit) as { rowid: number }[];
    if (rows.length) {
      const last = rows[rows.length - 1].rowid;
      if (table === "usage")
        this.db.prepare(`DELETE FROM usage WHERE rowid>? AND rowid<=?
          AND NOT EXISTS (SELECT 1 FROM records r WHERE r.binding=usage.binding AND r.work=usage.work)`)
          .run(cursor, last);
      else if (table === "plans")
        this.db.prepare(`DELETE FROM plans WHERE rowid>? AND rowid<=?
          AND NOT EXISTS (SELECT 1 FROM records r WHERE r.binding=plans.binding AND r.plan=plans.id)
          AND NOT EXISTS (SELECT 1 FROM records r WHERE r.binding=plans.binding AND r.legacy_unknown=1)`)
          .run(cursor, last);
      else
        this.db.prepare(`DELETE FROM observed_turns WHERE rowid>? AND rowid<=?
          AND CAST(end_ns AS INTEGER)<?
          AND NOT EXISTS (SELECT 1 FROM records r WHERE r.binding=observed_turns.binding AND r.work=observed_turns.work)`)
          .run(cursor, last, BigInt(Date.now() - 7 * 86_400_000) * 1_000_000n);
    }
    const more = rows.length === limit;
    if (table === "usage") this.usageCursor = more ? rows[rows.length - 1].rowid : 0;
    else if (table === "plans") this.planCursor = more ? rows[rows.length - 1].rowid : 0;
    else this.turnCursor = more ? rows[rows.length - 1].rowid : 0;
    return more;
  }
  /** Retain pending roots for at most 24 hours from their first observation.
   * Terminal receipts stay for seven days so replay cannot mint duplicate usage. */
  private collectReceipts(limit: number): number {
    const cutoff = Date.now() - 7 * 86_400_000;
    const removed = this.db.prepare(
      "DELETE FROM records WHERE id IN (SELECT id FROM records WHERE bytes IS NULL AND created<? LIMIT ?)",
    ).run(cutoff, limit).changes;
    if (removed || this.gcOrphans) {
      // A receipt deleted behind a running sweep needs one more full sweep.
      if (removed && (this.usageCursor || this.planCursor || this.turnCursor))
        this.gcRestartPending = true;
      const factsMore = this.collectOrphans("usage", limit);
      const plansMore = this.collectOrphans("plans", limit);
      const turnsMore = this.collectOrphans("observed_turns", limit);
      const completed = !factsMore && !plansMore && !turnsMore;
      this.gcOrphans = factsMore || plansMore || turnsMore || this.gcRestartPending;
      if (completed) this.gcRestartPending = false;
    }
    this.db.prepare(
      "DELETE FROM cursors WHERE rowid IN (SELECT rowid FROM cursors WHERE updated<? LIMIT ?)",
    ).run(cutoff, limit);
    return removed;
  }
  expire(): number {
    return this.write(() => {
      const expired = this.db.prepare(
        "UPDATE records SET bytes=NULL,state='expired' WHERE id IN (SELECT id FROM records WHERE bytes IS NOT NULL AND created<? LIMIT 128)",
      ).run(Date.now() - 86_400_000).changes;
      this.collectReceipts(128);
      return expired;
    });
  }
  status(): object {
    return {
      state: this.paused ?? "ready",
      physical: measure(this.root),
      cap: STORAGE_LIMITS.cap,
      logical_bytes: (
        this.db
          .prepare("SELECT coalesce(sum(length(bytes)),0) n FROM records")
          .get() as any
      ).n,
      destination_holds: this.db
        .prepare(
          "SELECT value code,count(*) count FROM meta WHERE key LIKE 'hold:%' GROUP BY value LIMIT 8",
        )
        .all(),
      counts: this.db
        .prepare(
          "SELECT state,count(*) count FROM records GROUP BY state LIMIT 16",
        )
        .all(),
      drain_counts: this.db
        .prepare(
          `SELECT count(*) pending,
            count(*) FILTER (WHERE state IN ('held','held-auth','split')) rejected,
            count(*) FILTER (WHERE state='ambiguous') ambiguous
           FROM records WHERE bytes IS NOT NULL`,
        )
        .get(),
      held_disabled: (
        this.db
          .prepare(
            "SELECT count(*) n FROM records r JOIN routes q ON r.route=q.id WHERE r.bytes IS NOT NULL AND (q.enabled=0 OR r.epoch!=q.epoch)",
          )
          .get() as any
      ).n,
      oldest_age_ms:
        (
          this.db
            .prepare(
              "SELECT ?-min(created) n FROM records WHERE bytes IS NOT NULL",
            )
            .get(Date.now()) as any
        ).n ?? 0,
      persistence: "unknown",
    };
  }
  close(): void {
    this.db.close();
  }
}
