import {
  createServer,
  createConnection,
  type Server,
  type Socket,
} from "node:net";
import { chmodSync, mkdirSync, rmdirSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { frame } from "@traceai/agent-core/client";
import { minimize } from "@traceai/agent-core";
import { AdapterRegistry, enforceCapability } from "../adapters.js";
import { loadConfig, type Paths, type Config } from "../config.js";
import { AgentError, protectedDirectory } from "../security/files.js";
import { FileSecretStore, OSSecretStore } from "../security/credentials.js";
import { exportBytes, retryDelay } from "../export/http.js";
import { StorageClient } from "./storage-client.js";
export class Runtime {
  private storage!: StorageClient;
  private admissionPaused = false;
  private server: Server | undefined;
  private config: Config = { schema_version: 1, profiles: {}, routes: {} };
  private timer: NodeJS.Timeout | undefined;
  private exporting: Promise<void> | undefined;
  private controller: AbortController | undefined;
  private stopped = false;
  private sockets = new Set<Socket>();
  private ingressBytes = 0;
  private lastDurableAck: number | null = null;
  private sourceReceipts = new Map<string, number>();
  private statusCache: unknown = { state: "unknown", persistence: "unknown" };
  constructor(
    private readonly paths: Paths,
    private readonly childEntry: string,
    private readonly version: string,
    private readonly registry = new AdapterRegistry(),
  ) {}
  async start(): Promise<void> {
    protectedDirectory(this.paths.state, true);
    if (Buffer.byteLength(this.paths.socket) > 100)
      throw new AgentError("SOCKET_PATH_TOO_LONG");
    try {
      mkdirSync(join(this.paths.state, "owner.lock"), { mode: 0o700 });
    } catch {
      throw new AgentError("RUNTIME_OWNERSHIP_CONFLICT", 6);
    }
    this.storage = new StorageClient(this.childEntry);
    try {
      await this.storage.call("init", {
        root: join(this.paths.state, "spool"),
        version: this.version,
      });
      await this.refresh();
      this.server = createServer((socket) => this.connection(socket));
      this.server.maxConnections = 32;
      await new Promise<void>((resolve, reject) => {
        this.server!.once("error", reject);
        this.server!.listen(this.paths.socket, () => {
          chmodSync(this.paths.socket, 0o600);
          resolve();
        });
      });
      this.timer = setInterval(() => {
        if (!this.exporting && !this.stopped) {
          this.exporting = this.drain()
            .catch(() => undefined)
            .finally(() => {
              this.exporting = undefined;
            });
        }
      }, 1000);
    } catch (e) {
      await this.storage.close().catch(() => undefined);
      try {
        rmdirSync(join(this.paths.state, "owner.lock"));
      } catch {
        /* absent */
      }
      throw e;
    }
  }
  private async refresh(): Promise<void> {
    const config = loadConfig(this.paths);
    for (const route of Object.values(config.routes)) {
      this.registry.get(route.agent, route.surface);
      await this.storage.call("route", route.route);
    }
    this.config = config;
  }
  private connection(socket: Socket): void {
    this.sockets.add(socket);
    let bytes = Buffer.alloc(0),
      reserved = 0,
      handled = false;
    socket.setTimeout(1000, () => socket.destroy());
    socket.on("error", () => socket.destroy());
    socket.on("close", () => {
      this.sockets.delete(socket);
      this.ingressBytes -= reserved;
    });
    socket.on("data", (chunk) => {
      if (handled) return;
      if (
        bytes.length + chunk.length > 1_048_580 ||
        this.ingressBytes + chunk.length > 16_777_216
      ) {
        socket.destroy();
        return;
      }
      reserved += chunk.length;
      this.ingressBytes += chunk.length;
      bytes = Buffer.concat([bytes, chunk]);
      if (bytes.length < 4) return;
      const size = bytes.readUInt32BE(0);
      if (size > 1_048_576) {
        socket.destroy();
        return;
      }
      if (bytes.length < size + 4) return;
      handled = true;
      socket.pause();
      void this.dispatch(bytes.subarray(4, size + 4))
        .then((result) => socket.end(frame(result)))
        .catch((e) =>
          socket.end(
            frame({
              protocol_version: 1,
              durable: false,
              reason_code:
                e instanceof AgentError ? e.code : "LOCAL_UNAVAILABLE",
            }),
          ),
        );
    });
  }
  private async fence(): Promise<void> {
    this.controller?.abort();
    await this.exporting;
  }
  private async dispatch(bytes: Buffer): Promise<unknown> {
    let m: any;
    try {
      m = JSON.parse(bytes.toString());
    } catch {
      throw new AgentError("INVALID_FRAME", 2);
    }
    if (m.protocol_version !== 1) throw new AgentError("INVALID_PROTOCOL", 2);
    if (m.operation === "observe-source") {
      const route = this.config.routes[m.route_ref];
      if (
        !route ||
        !route.route.enabled ||
        route.captureMode === "import-only" ||
        this.stopped ||
        this.admissionPaused
      )
        throw new AgentError("HELD_DISABLED", 3);
      const adapter = this.registry.get(route.agent, route.surface);
      if (
        !adapter.capability.observerSchema ||
        adapter.capability.observerSchema !== m.source_schema
      )
        throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
      const profile = this.config.profiles[route.profile];
      if (!profile || profile.removed) throw new AgentError("HELD_AUTH", 3);
      const secrets =
        profile.storage === "file"
          ? new FileSecretStore(this.paths.credentials)
          : new OSSecretStore();
      const secret = await secrets.get(profile.credentialRef);
      if (!secret) throw new AgentError("HELD_IDENTITY_KEY", 3);
      const facts = adapter.normalize(m.event, {
        identityKey: Buffer.from(secret.identity_key, "hex"),
        policyRevision: route.route.policy.revision,
        sourceSchema: m.source_schema,
      });
      // One terminal observation per envelope. No partial multi-fact ACK or raw journal.
      if (facts.length !== 1) throw new AgentError("INVALID_OBSERVER_FACTS", 2);
      enforceCapability(facts[0], adapter.capability);
      const result = await this.storage.call("admit", {
        route: m.route_ref,
        fact: minimize(facts[0], route.route.policy),
        observed: true,
      });
      this.sourceReceipts.set(m.route_ref, route.route.epoch);
      this.lastDurableAck = Date.now();
      return {
        protocol_version: 1,
        durable: true,
        reason_code: result.duplicate ? "DUPLICATE" : "COMMITTED",
        receipt_id: result.receipt_id,
      };
    }
    if (m.operation === "observe") {
      const route = this.config.routes[m.route_ref];
      if (
        !route ||
        !route.route.enabled ||
        this.stopped ||
        this.admissionPaused
      )
        throw new AgentError("HELD_DISABLED", 3);
      if (route.agent === "cursor")
        throw new AgentError("HELD_UNPROVEN_SOURCE_ALIAS", 3);
      enforceCapability(
        m.fact,
        this.registry.get(route.agent, route.surface).capability,
      );
      const result = await this.storage.call("admit", {
        route: m.route_ref,
        fact: minimize(m.fact, route.route.policy),
      });
      this.lastDurableAck = Date.now();
      return {
        protocol_version: 1,
        durable: true,
        reason_code: result.duplicate ? "DUPLICATE" : "COMMITTED",
        receipt_id: result.receipt_id,
      };
    }
    if (m.operation === "status")
      return {
        schema_version: 1,
        state: "running",
        code: 0,
        storage: this.statusCache,
        last_durable_local_ack: this.lastDurableAck,
        unknown_gap: "unknown",
        persistence: "unknown",
        agents: Object.values(this.config.routes).map((r) => ({
          agent: r.agent,
          surface: r.surface,
          enabled: r.route.enabled,
          activation:
            this.sourceReceipts.get(r.route.id) === r.route.epoch &&
            r.route.enabled
              ? { ...r.activation, activated: true, restartRequired: false }
              : r.activation,
          policy: r.route.policy.revision,
          export:
            r.route.binding.backendContract === "immutable-agent-v1"
              ? "approved"
              : "HELD_BACKEND_CONTRACT",
        })),
      };
    if (m.operation === "auth-rotated") {
      this.admissionPaused = true;
      try {
        await this.fence();
        await this.refresh();
        if (!this.config.profiles[m.profile])
          throw new AgentError("UNKNOWN_PROFILE", 2);
        for (const [id, route] of Object.entries(this.config.routes))
          if (route.profile === m.profile) {
            while ((await this.storage.call("releaseAuth", id)) > 0) {
              /* bounded writes */
            }
          }
        return { state: "auth-refreshed", code: 0 };
      } finally {
        this.admissionPaused = false;
      }
    }
    if (m.operation === "refresh") {
      this.admissionPaused = true;
      try {
        await this.fence();
        await this.refresh();
        return { state: "refreshed", code: 0 };
      } finally {
        this.admissionPaused = false;
      }
    }
    if (m.operation === "disable") {
      const r = this.config.routes[m.route];
      if (!r) throw new AgentError("UNKNOWN_ROUTE", 3);
      r.route.enabled = false;
      this.sourceReceipts.delete(m.route);
      await this.fence();
      await this.storage.call("disable", m.route);
      if (m.purge) {
        while ((await this.storage.call("purge", m.route)) > 0) {
          /* bounded child transactions */
        }
      }
      return { state: m.purge ? "purged" : "held", code: 0 };
    }
    if (m.operation === "flush") {
      const deadline = Number(m.deadlineMs ?? 5000);
      if (!Number.isFinite(deadline) || deadline < 1 || deadline > 60_000)
        throw new AgentError("INVALID_DEADLINE", 2);
      // Track the same sender promise as the background loop so disable can fence it.
      if (!this.exporting)
        this.exporting = this.drain()
          .catch(() => undefined)
          .finally(() => {
            this.exporting = undefined;
          });
      let timer: NodeJS.Timeout | undefined;
      await Promise.race([
        this.exporting,
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, deadline);
        }),
      ]);
      if (timer) clearTimeout(timer);
      return {
        schema_version: 1,
        state: "pending",
        code: 4,
        storage: this.statusCache,
        persistence: "unknown",
      };
    }
    if (m.operation === "stop") {
      this.stopped = true;
      setImmediate(() => void this.stop());
      return { state: "stopping", code: 0 };
    }
    throw new AgentError("UNKNOWN_COMMAND", 2);
  }
  private async drain(): Promise<void> {
    for (const [id, route] of Object.entries(this.config.routes)) {
      if (this.stopped || !route.route.enabled) continue;
      const profile = this.config.profiles[route.profile];
      if (
        !profile ||
        profile.removed ||
        profile.binding.id !== route.route.binding.id ||
        profile.binding.endpoint !== route.route.binding.endpoint ||
        profile.binding.project !== route.route.binding.project ||
        profile.binding.approvedOrigin !== route.route.binding.approvedOrigin ||
        profile.binding.backendContract !== "immutable-agent-v1"
      )
        continue;
      const row = await this.storage.call("next", { route: id });
      if (!row) continue;
      const store =
        profile.storage === "file"
          ? new FileSecretStore(this.paths.credentials)
          : new OSSecretStore();
      let credentials;
      try {
        credentials = await store.get(profile.credentialRef);
      } catch {
        continue;
      }
      if (!credentials) continue;
      this.controller = new AbortController();
      const result = await exportBytes(
        profile.binding,
        Buffer.from(row.bytes),
        credentials,
        this.controller.signal,
      );
      this.controller = undefined;
      if (!route.route.enabled || this.stopped) continue;
      await this.storage.call("complete", {
        id: row.id,
        epoch: row.epoch,
        result: {
          ...result,
          nextAt: Date.now() + retryDelay(row.attempts, result.retryAfterMs),
        },
      });
    }
    await this.storage.call("expire");
    this.statusCache = await this.storage.call("status");
  }
  async stop(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.controller?.abort();
    this.server?.close();
    for (const s of this.sockets) s.destroy();
    await this.exporting?.catch(() => undefined);
    await this.storage.close().catch(() => undefined);
    try {
      unlinkSync(this.paths.socket);
    } catch {
      /* absent */
    }
    try {
      rmdirSync(join(this.paths.state, "owner.lock"));
    } catch {
      /* absent */
    }
  }
}
export async function runtimeRequest(
  path: string,
  message: object,
  deadlineMs = 2000,
): Promise<any> {
  return new Promise((resolve, reject) => {
    let data = Buffer.alloc(0),
      done = false;
    const socket = createConnection(path);
    const finish = (error?: Error, value?: unknown) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      socket.destroy();
      if (error) reject(error);
      else resolve(value);
    };
    const timer = setTimeout(
      () => finish(new AgentError("RUNTIME_UNAVAILABLE")),
      deadlineMs,
    );
    socket.on("error", () => finish(new AgentError("RUNTIME_UNAVAILABLE")));
    socket.on("end", () => {
      if (!done) finish(new AgentError("RUNTIME_UNAVAILABLE"));
    });
    socket.on("connect", () =>
      socket.write(frame({ protocol_version: 1, ...message })),
    );
    socket.on("data", (chunk) => {
      data = Buffer.concat([data, chunk]);
      if (data.length > 65_536)
        return finish(new AgentError("INVALID_RESPONSE"));
      if (data.length < 4 || data.length < data.readUInt32BE(0) + 4) return;
      try {
        const result = JSON.parse(data.subarray(4).toString());
        if (result.durable === false)
          return finish(new AgentError(result.reason_code));
        finish(undefined, result);
      } catch {
        finish(new AgentError("INVALID_RESPONSE"));
      }
    });
  });
}
