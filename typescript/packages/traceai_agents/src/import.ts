import { createHash, randomUUID } from "node:crypto";
import { join } from "node:path";
import { minimize, type CanonicalFact } from "@traceai/agent-core";
import type { Adapter, NormalizeContext } from "./adapters.js";
import { enforceCapability } from "./adapters.js";
import {
  AgentError,
  atomicProtected,
  readProtected,
  protectedDirectory,
} from "./security/files.js";
interface Plan {
  schema_version: 1;
  id: string;
  sourceDigest: string;
  adapter: string;
  schema: string;
  policy: string;
  workIds: string[];
  created: number;
}
export class ImportPlanner {
  constructor(private readonly directory: string) {
    protectedDirectory(directory, true);
  }
  private read(
    adapter: Adapter,
    path: string,
    context: NormalizeContext,
  ): { digest: string; facts: CanonicalFact[] } {
    if (!adapter.import) throw new AgentError("UNSUPPORTED_IMPORT", 5);
    const bytes = adapter.readImport
      ? adapter.readImport(path)
      : readProtected(path, 1_048_576);
    if (bytes.length > 1_048_576)
      throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
    const facts = adapter.import(bytes, context).map((f) => {
      enforceCapability(f, adapter.capability);
      return minimize(f);
    });
    if (facts.length > 128) throw new AgentError("IMPORT_SLICE_REQUIRED", 4);
    return { digest: createHash("sha256").update(bytes).digest("hex"), facts };
  }
  plan(adapter: Adapter, path: string, context: NormalizeContext): Plan {
    const source = this.read(adapter, path, context);
    const plan: Plan = {
      schema_version: 1,
      id: randomUUID(),
      sourceDigest: source.digest,
      adapter: adapter.capability.agent + "/" + adapter.capability.surface,
      schema: context.sourceSchema,
      policy: context.policyRevision,
      workIds: source.facts.map((f) => f.sourceWorkId),
      created: Date.now(),
    };
    // Exactly one bounded plan per local planner; no unbounded historical archive.
    atomicProtected(join(this.directory, "plan.json"), JSON.stringify(plan));
    return plan;
  }
  async commit(
    id: string,
    adapter: Adapter,
    path: string,
    context: NormalizeContext,
    admit: (fact: CanonicalFact) => Promise<unknown>,
  ): Promise<{ admitted: number; pending: number }> {
    const plan = JSON.parse(
      readProtected(join(this.directory, "plan.json")).toString(),
    ) as Plan;
    if (
      plan.id !== id ||
      Date.now() - plan.created > 3_600_000 ||
      plan.adapter !==
        adapter.capability.agent + "/" + adapter.capability.surface ||
      plan.schema !== context.sourceSchema ||
      plan.policy !== context.policyRevision
    )
      throw new AgentError("IMPORT_PLAN_CHANGED", 6);
    const source = this.read(adapter, path, context);
    if (
      source.digest !== plan.sourceDigest ||
      JSON.stringify(source.facts.map((f) => f.sourceWorkId)) !==
        JSON.stringify(plan.workIds)
    )
      throw new AgentError("SOURCE_CHANGED", 6);
    let admitted = 0;
    for (const fact of source.facts) {
      try {
        await admit(fact);
        admitted++;
      } catch {
        break;
      }
    }
    return { admitted, pending: source.facts.length - admitted };
  }
}
