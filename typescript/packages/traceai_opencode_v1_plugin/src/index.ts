import type { Plugin, Hooks } from "@opencode-ai/plugin";
import { createTurnTracer, resolveConfig } from "@traceai/agent-core";
import { observe } from "./observer.js";

const key = Symbol.for("@traceai/opencode-v1-plugin/instances/v1");
type Instance = { hooks: Hooks; users: number };
const registry = globalThis as typeof globalThis & { [key]?: Map<string, Instance> };

// OpenCode V1 executes every runtime export. Export ONLY the plugin function;
// shared helpers live in non-exported package modules, never V2 SDK peers.
const plugin: Plugin = async (host) => {
  try {
    const config = await resolveConfig({ agent: "opencode-v1", projectDir: host.directory });
    if (!config.enabled) return {};
    const instances = registry[key] ??= new Map();
    const existing = instances.get(host.directory);
    if (existing) {
      existing.users++;
      return lease(existing);
    }
    if (instances.size >= 32) return {};
    const instance: Instance = { hooks: observe(config, createTurnTracer({ config })), users: 1 };
    instances.set(host.directory, instance);
    return lease(instance);
    function lease(instance: Instance): Hooks {
      let disposed = false;
      return {
        ...instance.hooks,
        async dispose() {
          if (disposed) return;
          disposed = true;
          if (--instance.users > 0) return;
          instances.delete(host.directory);
          await instance.hooks.dispose?.();
        },
      };
    }
  } catch {
    return {}; // Missing/unsafe configuration never prevents the agent starting.
  }
};
export default plugin;
