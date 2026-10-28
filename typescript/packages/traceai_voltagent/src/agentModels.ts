import { providerFromAiSdk } from "./mapping";

/**
 * VoltAgent records only `modelId` for an AI SDK model object (agent.ts getModelName); the
 * object's `provider` ("openai.chat", ...) never reaches a span. The agent is looked up by its
 * span's `entity.id` in VoltAgent's agent registry, a process-wide singleton kept on
 * `globalThis.___voltagent_agent_registry` (@voltagent/core 2.11.0 AgentRegistry.getInstance),
 * which `new VoltAgent({ agents })` fills. An agent that was never registered, or a dynamic model,
 * yields no provider.
 */
const REGISTRY_KEY = "___voltagent_agent_registry";

interface RegistryLike {
  getAgent?(id: string): { model?: unknown } | undefined;
}

interface ModelLike {
  provider?: unknown;
  modelId?: unknown;
}

function candidates(model: unknown): ModelLike[] {
  if (Array.isArray(model)) {
    // Fallback lists: AgentModelConfig[] ({ model, ... }).
    return model.map((entry) => (entry && typeof entry === "object" && "model" in entry ? entry.model : entry));
  }
  return [model as ModelLike];
}

/** gen_ai.provider.name for the AI SDK model object agent `agentId` used for `modelId`, if known. */
export function providerForAgentModel(agentId: string, modelId: string | undefined): string | undefined {
  const registry = (globalThis as Record<string, unknown>)[REGISTRY_KEY] as RegistryLike | undefined;
  const agent = typeof registry?.getAgent === "function" ? registry.getAgent(agentId) : undefined;
  for (const model of candidates(agent?.model)) {
    if (!model || typeof model !== "object" || typeof model.provider !== "string") continue;
    if (modelId !== undefined && model.modelId !== modelId) continue;
    return providerFromAiSdk(model.provider);
  }
  return undefined;
}
