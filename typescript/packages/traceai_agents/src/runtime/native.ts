import { randomBytes } from "node:crypto";
import type { Server } from "node:http";
import type { CanonicalFact } from "@traceai/agent-core";
import type { Adapter, NormalizeContext } from "../adapters.js";
import { AgentError } from "../security/files.js";
import { startReceiver } from "./receiver.js";

interface Source {
  adapter: Adapter;
  context: NormalizeContext;
  /** Recheck the live route and epoch, including after asynchronous startup. */
  check(): void;
  admit(fact: CanonicalFact): Promise<unknown>;
}
interface Lease {
  route: string;
  server: Server;
  key: Uint8Array;
  timer: NodeJS.Timeout;
  check(): void;
}
export interface NativeLease {
  id: string;
  endpoint: string;
  token: string;
  renewAfterMs: number;
}

/** Runtime-owned, short-lived HTTP ingress. No upstream auth leaves this process.
 * Native capabilities are same-UID scoped, NOT a sandbox against child tools.
 * Lease expiry handles a killed wrapper; disable/refresh/stop revoke immediately. */
export class NativeSessions {
  private readonly leases = new Map<string, Lease>();
  private readonly opening = new Set<{ route: string; revoked: boolean }>();
  constructor(
    private readonly resolve: (route: string) => Promise<Source>,
    private readonly lifetimeMs = 15_000,
  ) {}

  async open(route: string): Promise<NativeLease> {
    if (this.leases.size + this.opening.size >= 8)
      throw new AgentError("NATIVE_SESSION_LIMIT", 7);
    const pending = { route, revoked: false };
    this.opening.add(pending);
    let key: Uint8Array | undefined;
    let server: Server | undefined;
    let installed = false;
    try {
      const source = await this.resolve(route);
      key = source.context.identityKey;
      const id = randomBytes(16).toString("hex");
      const token = randomBytes(32).toString("hex");
      const check = () => {
        if (!this.leases.has(id))
          throw new AgentError("HELD_NATIVE_SESSION", 3);
        source.check();
      };
      source.check();
      if (pending.revoked) throw new AgentError("HELD_NATIVE_SESSION", 3);
      server = await startReceiver({
        // Effective producer/destination preflight belongs to the qualified
        // adapter, before it requests this owned local endpoint. No fan-out.
        coexistence: { state: "owned-or-none" },
        token,
        normalize: (_span, envelope) => {
          check();
          const facts = source.adapter.normalize(envelope, source.context);
          if (facts.length !== 1)
            throw new AgentError("INVALID_NATIVE_PROFILE", 5);
          return facts[0];
        },
        admit: async (fact) => {
          check();
          return source.admit(fact);
        },
      });
      source.check();
      if (pending.revoked) throw new AgentError("HELD_NATIVE_SESSION", 3);
      const address = server.address();
      if (!address || typeof address === "string")
        throw new AgentError("LOCAL_UNAVAILABLE");
      const timer = setTimeout(() => void this.close(id), this.lifetimeMs);
      timer.unref();
      this.leases.set(id, { route, server, key, timer, check });
      installed = true;
      return {
        id,
        token,
        endpoint: `http://127.0.0.1:${address.port}/v1/traces`,
        renewAfterMs: Math.floor(this.lifetimeMs / 3),
      };
    } finally {
      this.opening.delete(pending);
      if (!installed) {
        key?.fill(0);
        if (server) await this.closeServer(server);
      }
    }
  }

  renew(id: string): void {
    const lease = this.leases.get(id);
    if (!lease) throw new AgentError("HELD_NATIVE_SESSION", 3);
    lease.check();
    lease.timer.refresh();
  }
  count(route: string): number {
    return [...this.leases.values()].filter((lease) => lease.route === route)
      .length;
  }
  async close(id: string): Promise<void> {
    const lease = this.leases.get(id);
    if (!lease) return;
    this.leases.delete(id);
    clearTimeout(lease.timer);
    lease.key.fill(0);
    await this.closeServer(lease.server);
  }
  async revoke(route?: string): Promise<void> {
    // Also fence opens suspended on the credential store or listen callback.
    for (const pending of this.opening)
      if (route === undefined || pending.route === route)
        pending.revoked = true;
    await Promise.all(
      [...this.leases.entries()]
        .filter(([, lease]) => route === undefined || route === lease.route)
        .map(([id]) => this.close(id)),
    );
  }
  private async closeServer(server: Server): Promise<void> {
    const closed = new Promise<void>((resolve) =>
      server.close(() => resolve()),
    );
    // A producer with an unfinished body must not stall disable or agent exit.
    server.closeAllConnections();
    await closed;
  }
}
