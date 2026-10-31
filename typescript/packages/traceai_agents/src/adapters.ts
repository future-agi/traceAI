import { token, type CanonicalFact } from "@traceai/agent-core";
import type { Binding } from "./export/endpoint.js";
import { AgentError } from "./security/files.js";
export interface Capability {
  contract: 1;
  agent: string;
  surface: string;
  versions: readonly string[];
  schemas: readonly string[];
  identityProfiles: readonly string[];
  models: readonly string[];
  providers: readonly string[];
  tools: readonly string[];
  platforms: readonly string[];
  transcript: boolean;
  native: boolean;
  neutralResponse: string;
}
export interface Activation {
  installed: boolean;
  trusted: boolean | "unknown";
  activated: boolean;
  restartRequired: boolean;
}
export interface OwnedChange {
  /** Adapter preflights all effective fields; commit compares exact owned nonsecret values. */
  commit(): Promise<Activation>;
  rollback(): Promise<void>;
}
export interface Adapter {
  capability: Capability;
  preflight(
    action: "enable" | "disable" | "update" | "uninstall",
    context: AdapterContext,
  ): Promise<OwnedChange>;
  probe(context: AdapterContext): Promise<Activation>;
  normalize(
    input: unknown,
    context: NormalizeContext,
  ): readonly CanonicalFact[];
  /** Always preflight security/destinations; tracing=false must not start collectors
   * or add telemetry arguments. The CLI also omits tracing environment overlays. */
  launch?(
    context: AdapterContext,
    options: { tracing: boolean },
  ): Promise<{
    executable: string;
    env: Record<string, string>;
    args: readonly string[];
  }>;
  import?(bytes: Buffer, context: NormalizeContext): readonly CanonicalFact[];
}
export interface AdapterContext {
  routeRef: string;
  socketPath: string;
  surface: string;
  profile: string;
  binding: Binding;
  transcriptConsent: boolean;
}
export interface NormalizeContext {
  identityKey: Uint8Array;
  policyRevision: string;
  sourceSchema: string;
}
export class AdapterRegistry {
  private readonly entries = new Map<string, Adapter>();
  /** No package discovery, environment loading or repository-supplied executable templates. */
  constructor(adapters: readonly Adapter[] = []) {
    for (const adapter of adapters) {
      const c = adapter.capability;
      if (c.contract !== 1 || !token(c.agent) || !token(c.surface))
        throw new AgentError("INVALID_CAPABILITY", 2);
      const key = c.agent + "/" + c.surface;
      if (this.entries.has(key)) throw new AgentError("DUPLICATE_ADAPTER", 2);
      this.entries.set(key, adapter);
    }
  }
  get(agent: string, surface: string): Adapter {
    const adapter = this.entries.get(agent + "/" + surface);
    if (!adapter || !adapter.capability.platforms.includes(process.platform))
      throw new AgentError("UNSUPPORTED_AGENT_SURFACE", 5);
    return adapter;
  }
  list(): Capability[] {
    return [...this.entries.values()].map((a) => a.capability);
  }
}
export function enforceCapability(fact: CanonicalFact, c: Capability): void {
  if (
    fact.agent !== c.agent ||
    fact.surface !== c.surface ||
    !c.schemas.includes(fact.sourceSchema) ||
    !c.identityProfiles.includes(fact.identityProfile) ||
    !fact.agentVersion ||
    !c.versions.includes(fact.agentVersion)
  )
    throw new AgentError("UNKNOWN_SOURCE_SCHEMA", 5);
  for (const [value, allowed] of [
    [fact.model, c.models],
    [fact.provider, c.providers],
    [fact.tool, c.tools],
  ] as const)
    if (value !== undefined && !allowed.includes(value))
      throw new AgentError("UNREGISTERED_LABEL", 3);
}
