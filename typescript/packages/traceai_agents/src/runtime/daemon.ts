import {
  createServer,
  createConnection,
  type Server,
  type Socket,
} from "node:net";
import { chmodSync, lstatSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { frame } from "@traceai/agent-core/client";
import { minimize, type CanonicalFact } from "@traceai/agent-core";
import { AdapterRegistry, enforceCapability } from "../adapters.js";
import { loadConfig, type Paths, type Config } from "../config.js";
import { AgentError, protectedDirectory } from "../security/files.js";
import { FileSecretStore, OSSecretStore } from "../security/credentials.js";
import { exportBytes, retryDelay } from "../export/http.js";
import { StorageClient } from "./storage-client.js";
import { NativeIngress } from "./native.js";
export class Runtime {
  private storage!: StorageClient;
  private readonly native = new NativeIngress();
  private admissionPaused = false;
  private server: Server | undefined;
  private config: Config = { schema_version: 1, profiles: {}, routes: {} };
  private timer: NodeJS.Timeout | undefined;
  private exporting: Promise<boolean> | undefined;
  private nextRoute = 0;
  private controller: AbortController | undefined;
  private stopped = false;
  private stopping: Promise<void> | undefined;
  private sockets = new Set<Socket>();
  private ingressBytes = 0;
  private lastDurableAck: number | null = null;
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
    this.storage = new StorageClient(this.childEntry, () => {
      // Loss of the crash-released storage lock also revokes this sender.
      this.stopped = true;
      this.controller?.abort();
      this.native.stop();
      this.server?.close();
      if (this.timer) clearInterval(this.timer);
      for (const socket of this.sockets) socket.destroy();
    });
    try {
      await this.storage.call("init", {
        root: join(this.paths.state, "spool"),
        version: this.version,
      });
      // Only the exclusive SQLite owner may remove a socket left by a crash.
      // Never remove a regular file, symlink, or another user's socket.
      try {
        const s = lstatSync(this.paths.socket);
        if (!s.isSocket() || s.uid !== process.getuid?.() || s.mode & 0o077)
          throw new AgentError("UNSAFE_RUNTIME_SOCKET", 6);
        // A storage-child death can release its lock before its live parent has
        // closed ingress. A connectable (even unresponsive) listener is not stale.
        if (await socketIsLive(this.paths.socket))
          throw new AgentError("RUNTIME_OWNERSHIP_CONFLICT", 6);
        unlinkSync(this.paths.socket);
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
      }
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
        this.native.sweep();
        if (!this.exporting && !this.stopped) {
          this.startDrain(10_000);
        }
      }, 1000);
    } catch (e) {
      this.server?.close();
      await this.storage.close().catch(() => undefined);
      throw e;
    }
  }
  private async refresh(): Promise<void> {
    const config = loadConfig(this.paths);
    for (const route of Object.values(config.routes)) {
      this.registry.get(route.agent, route.surface);
      await this.storage.call("validateRoute", route.route);
    }
    for (const route of Object.values(config.routes))
      await this.storage.call("route", route.route);
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
      void this.dispatch(bytes.subarray(4, size + 4), socket)
        .then((result) => socket.end(frame(result)))
        .catch((e) =>
          socket.end(
            frame({
              protocol_version: 1,
              durable: false,
              code: e instanceof AgentError ? e.exitCode : 7,
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
  private async dispatch(bytes: Buffer, socket: Socket): Promise<unknown> {
    let m: any;
    try {
      m = JSON.parse(bytes.toString());
    } catch {
      throw new AgentError("INVALID_FRAME", 2);
    }
    if (m.protocol_version !== 1) throw new AgentError("INVALID_PROTOCOL", 2);
    // The short timer bounds incomplete ingress, not a complete administrative
    // operation. Flush replaces this bound with its validated caller deadline.
    socket.setTimeout(15_000);
    if (["native-open", "native-close", "native-renew"].includes(m.operation)) {
      const route = this.config.routes[m.route];
      if (!route) throw new AgentError("UNKNOWN_ROUTE", 3);
      if (m.operation === "native-close") {
        this.native.close(m.route, m.lease);
        return { code: 0, state: "closed" };
      }
      if (!route.route.enabled || this.stopped || this.admissionPaused)
        throw new AgentError("HELD_DISABLED", 3);
      if (m.operation === "native-renew") {
        this.native.renew(m.route, m.lease);
        return { code: 0, state: "renewed" };
      }
      const adapter = this.registry.get(route.agent, route.surface);
      const profile = this.config.profiles[route.profile];
      if (!profile || profile.removed) throw new AgentError("HELD_AUTH", 3);
      const secrets =
        profile.storage === "file"
          ? new FileSecretStore(this.paths.credentials)
          : new OSSecretStore();
      const credentials = await secrets.get(profile.credentialRef);
      if (!credentials) throw new AgentError("HELD_IDENTITY_KEY", 3);
      const epoch = route.route.epoch;
      return this.native.open(
        m.route,
        adapter,
        {
          identityKey: Buffer.from(credentials.identity_key, "hex"),
          policyRevision: route.route.policy.revision,
          sourceSchema: adapter.capability.schemas[0],
        },
        () =>
          !this.stopped &&
          this.config.routes[m.route]?.route.enabled === true &&
          this.config.routes[m.route]?.route.epoch === epoch,
        (fact) => this.admit(m.route, fact),
      );
    }
    if (m.operation === "observe") return this.admit(m.route_ref, m.fact);
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
          activation: r.activation,
          native_durable_ack: this.native.activated.has(r.route.id),
          native_capture: this.native.status(r.route.id),
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
    if (m.operation === "route-state") {
      try {
        return { route: await this.storage.call("getRoute", m.route), code: 0 };
      } catch (e) {
        if (e instanceof AgentError && e.code === "UNKNOWN_ROUTE")
          return { route: null, code: 0 };
        throw e;
      }
    }
    if (m.operation === "validate-route") {
      await this.storage.call("validateRoute", m.route);
      return { state: "validated", code: 0 };
    }
    if (m.operation === "disable") {
      const r = this.config.routes[m.route];
      if (!r) throw new AgentError("UNKNOWN_ROUTE", 3);
      r.route.enabled = false;
      this.native.closeRoute(m.route);
      await this.fence();
      await this.storage.call("disable", m.route);
      r.route.epoch = (await this.storage.call("getRoute", m.route)).epoch;
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
      socket.setTimeout(deadline + 500);
      const until = Date.now() + deadline;
      let timer: NodeJS.Timeout | undefined;
      const expired = new Promise<false>((resolve) => {
        timer = setTimeout(() => resolve(false), deadline);
      });
      try {
        // Join the existing sender; never create parallel leases. A bounded batch
        // may leave eligible rows, so continue fair batches within this deadline.
        while (Date.now() < until && !this.stopped && !this.admissionPaused) {
          const more = await Promise.race([
            this.exporting ?? this.startDrain(until - Date.now()),
            expired,
          ]);
          if (!more) break;
        }
      } finally {
        if (timer) clearTimeout(timer);
      }
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
  /** Native and hook ingress share the same capability, policy and durable ACK boundary. */
  private async admit(routeRef: string, fact: CanonicalFact): Promise<unknown> {
    const route = this.config.routes[routeRef];
    if (!route || !route.route.enabled || this.stopped || this.admissionPaused)
      throw new AgentError("HELD_DISABLED", 3);
    const safe = minimize(fact, route.route.policy);
    enforceCapability(
      safe,
      this.registry.get(route.agent, route.surface).capability,
    );
    const result = await this.storage.call("admit", {
      route: routeRef,
      fact: safe,
    });
    this.lastDurableAck = Date.now();
    return {
      protocol_version: 1,
      durable: true,
      reason_code: result.duplicate ? "DUPLICATE" : "COMMITTED",
      receipt_id: result.receipt_id,
    };
  }
  private startDrain(deadlineMs: number): Promise<boolean> {
    this.exporting = this.drain(deadlineMs)
      .catch(() => false)
      .finally(() => {
        this.exporting = undefined;
      });
    return this.exporting;
  }
  private async drain(deadlineMs: number): Promise<boolean> {
    const controller = new AbortController();
    this.controller = controller;
    const until = Date.now() + deadlineMs;
    const timer = setTimeout(() => controller.abort(), Math.max(1, deadlineMs));
    const routes = Object.entries(this.config.routes);
    const deferred = new Set<string>();
    let sent = 0,
      idle = 0;
    const active = () =>
      !this.stopped &&
      !this.admissionPaused &&
      !controller.signal.aborted &&
      Date.now() < until;
    try {
      // Round-robin one row at a time; keep the cursor across batches so a busy
      // or slow first route cannot monopolize subsequent background/flush work.
      while (routes.length && idle < routes.length && sent < 128 && active()) {
        const [id, route] = routes[this.nextRoute++ % routes.length];
        this.nextRoute %= routes.length;
        idle++;
        if (!route.route.enabled || deferred.has(id)) continue;
        const profile = this.config.profiles[route.profile];
        if (
          !profile ||
          profile.removed ||
          profile.binding.id !== route.route.binding.id ||
          profile.binding.endpoint !== route.route.binding.endpoint ||
          profile.binding.project !== route.route.binding.project ||
          profile.binding.approvedOrigin !==
            route.route.binding.approvedOrigin ||
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
          deferred.add(id);
          continue;
        }
        if (!credentials || !active() || !route.route.enabled) continue;
        const result = await exportBytes(
          profile.binding,
          Buffer.from(row.bytes),
          credentials,
          controller.signal,
        );
        sent++;
        idle = 0;
        if (!active() || !route.route.enabled) break;
        await this.storage.call("complete", {
          id: row.id,
          epoch: row.epoch,
          result: {
            ...result,
            nextAt: Date.now() + retryDelay(row.attempts, result.retryAfterMs),
          },
        });
        // Do not turn a flush into a retry loop or keep sending to an unhealthy
        // destination; next_at and persistent destination holds still apply.
        if (result.state !== "accepted") deferred.add(id);
      }
      await this.storage.call("expire");
      this.statusCache = await this.storage.call("status");
      return idle < routes.length && (sent >= 128 || Date.now() >= until);
    } finally {
      clearTimeout(timer);
      if (this.controller === controller) this.controller = undefined;
    }
  }
  stop(): Promise<void> {
    return (this.stopping ??= this.shutdown());
  }
  private async shutdown(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.controller?.abort();
    this.native.stop();
    const closed = this.server?.listening
      ? new Promise<void>((resolve) => this.server!.close(() => resolve()))
      : Promise.resolve();
    for (const s of this.sockets) s.destroy();
    await closed;
    await this.exporting?.catch(() => undefined);
    // server.close removes our socket before releasing storage ownership.
    // Never unlink after close: a successor may already own the pathname.
    await this.storage.close().catch(() => undefined);
  }
}
async function socketIsLive(path: string): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = createConnection(path);
    const finish = (live: boolean) => {
      clearTimeout(timer);
      socket.destroy();
      resolve(live);
    };
    // Unknown errors/timeouts are conflicts, never authority to unlink.
    const timer = setTimeout(() => finish(true), 500);
    socket.once("connect", () => finish(true));
    socket.once("error", (e: NodeJS.ErrnoException) =>
      finish(e.code !== "ECONNREFUSED" && e.code !== "ENOENT"),
    );
  });
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
          return finish(
            new AgentError(
              result.reason_code,
              Number.isInteger(result.code) &&
              result.code >= 2 &&
              result.code <= 7
                ? result.code
                : 7,
            ),
          );
        finish(undefined, result);
      } catch {
        finish(new AgentError("INVALID_RESPONSE"));
      }
    });
  });
}
