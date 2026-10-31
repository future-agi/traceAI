import { randomBytes } from "node:crypto";
import type { Server } from "node:http";
import type { CanonicalFact } from "@traceai/agent-core";
import type { Adapter, NormalizeContext } from "../adapters.js";
import { AgentError } from "../security/files.js";
import { startReceiver } from "./receiver.js";

/** Process-scoped local credentials only. No keys or listener addresses go into persistent state. */
export class NativeIngress {
  private readonly leases = new Map<
    string,
    { route: string; server: Server; expires: number }
  >();
  private opening = 0;
  readonly activated = new Set<string>();
  private readonly rejections = new Map<
    string,
    { rejected: number; last_reason: string }
  >();
  status(route: string): { rejected: number; last_reason?: string } {
    return this.rejections.get(route) ?? { rejected: 0 };
  }
  async open(
    route: string,
    adapter: Adapter,
    context: NormalizeContext,
    active: () => boolean,
    admit: (fact: CanonicalFact) => Promise<unknown>,
  ): Promise<{ endpoint: string; token: string; lease: string }> {
    if (!adapter.nativeNormalize) throw new AgentError("NATIVE_UNSUPPORTED", 5);
    if (this.leases.size + this.opening >= 8)
      throw new AgentError("NATIVE_LISTENER_LIMIT", 7);
    this.opening++;
    const token = randomBytes(32).toString("hex"),
      lease = randomBytes(16).toString("hex");
    try {
      const server = await startReceiver({
        coexistence: { state: "owned-or-none" },
        token,
        rejected: (code) => {
          if (this.rejections.size >= 64 && !this.rejections.has(route)) return;
          this.rejections.set(route, {
            rejected: Math.min(1_000_000, this.status(route).rejected + 1),
            last_reason: code,
          });
        },
        normalize: (span, resource, scope) =>
          adapter.nativeNormalize!(span, resource, scope, context),
        admit: async (fact) => {
          if (!active() || !this.leases.has(lease))
            throw new AgentError("HELD_DISABLED", 3);
          const result = await admit(fact);
          if (active()) this.activated.add(route);
          return result;
        },
      });
      if (!active()) {
        server.closeAllConnections();
        server.close();
        throw new AgentError("HELD_DISABLED", 3);
      }
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        throw new AgentError("NATIVE_LISTENER_FAILED");
      }
      this.leases.set(lease, { route, server, expires: Date.now() + 60_000 });
      return {
        endpoint: `http://127.0.0.1:${address.port}/v1/traces`,
        token,
        lease,
      };
    } finally {
      this.opening--;
    }
  }
  renew(route: string, lease: string): void {
    const current = this.leases.get(lease);
    if (!current || current.route !== route)
      throw new AgentError("NATIVE_LEASE_EXPIRED", 3);
    current.expires = Date.now() + 60_000;
  }
  close(route: string, lease: string): void {
    const current = this.leases.get(lease);
    if (!current || current.route !== route) return;
    this.leases.delete(lease);
    current.server.closeAllConnections();
    current.server.close();
  }
  closeRoute(route: string): void {
    for (const [lease, current] of this.leases)
      if (current.route === route) this.close(route, lease);
    this.activated.delete(route);
  }
  sweep(): void {
    for (const [lease, current] of this.leases)
      if (current.expires <= Date.now()) this.close(current.route, lease);
  }
  stop(): void {
    for (const [lease, current] of this.leases)
      this.close(current.route, lease);
    this.activated.clear();
  }
}
