import { homedir } from "node:os";
import { join } from "node:path";
import { mkdirSync, rmdirSync } from "node:fs";
import { token } from "@traceai/agent-core";
import {
  AgentError,
  atomicProtected,
  protectedDirectory,
  readProtected,
} from "./security/files.js";
import { validateBinding, type Binding } from "./export/endpoint.js";
import type { Route } from "./storage/store.js";
export interface Profile {
  binding: Binding;
  storage: "os" | "file";
  credentialRef: string;
  removed?: boolean;
}
export interface InstalledRoute {
  route: Route;
  agent: string;
  surface: string;
  profile: string;
  transcriptConsent: boolean;
  activation: {
    installed: boolean;
    trusted: boolean | "unknown";
    activated: boolean;
    restartRequired: boolean;
  };
}
export interface Config {
  schema_version: 1;
  profiles: Record<string, Profile>;
  routes: Record<string, InstalledRoute>;
  /** Durable privacy tombstones survive daemon absence and uninstall. */
  fences?: Record<string, { purge: boolean }>;
}
export interface Paths {
  config: string;
  state: string;
  credentials: string;
  socket: string;
  configFile: string;
}
export function paths(env: NodeJS.ProcessEnv = process.env): Paths {
  const home = homedir();
  const config =
    env.FI_AGENT_CONFIG_HOME ??
    (process.platform === "darwin"
      ? join(home, "Library/Application Support/FutureAGI/agents")
      : join(env.XDG_CONFIG_HOME ?? join(home, ".config"), "futureagi/agents"));
  const state =
    env.FI_AGENT_STATE_HOME ??
    (process.platform === "darwin"
      ? join(config, "state")
      : join(
          env.XDG_STATE_HOME ?? join(home, ".local/state"),
          "futureagi/agents",
        ));
  return {
    config,
    state,
    credentials: join(config, "credentials"),
    socket: join(state, "runtime.sock"),
    configFile: join(config, "config.json"),
  };
}
export function loadConfig(p: Paths): Config {
  let bytes: Buffer;
  try {
    bytes = readProtected(p.configFile);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT")
      return { schema_version: 1, profiles: {}, routes: {} };
    throw e;
  }
  try {
    const c = JSON.parse(bytes.toString());
    if (
      c.schema_version !== 1 ||
      !c.profiles ||
      !c.routes ||
      Object.keys(c.profiles).length > 64 ||
      Object.keys(c.routes).length > 64
    )
      throw new Error();
    for (const [key, value] of Object.entries(c.profiles) as [
      string,
      Profile,
    ][]) {
      if (
        !token(key) ||
        !token(value.credentialRef) ||
        !["os", "file"].includes(value.storage)
      )
        throw new Error();
      validateBinding(value.binding);
    }
    for (const [key, value] of Object.entries(c.routes) as [
      string,
      InstalledRoute,
    ][]) {
      if (
        !token(key) ||
        !token(value.agent) ||
        !token(value.surface) ||
        !c.profiles[value.profile] ||
        value.route.id !== key
      )
        throw new Error();
    }
    if (c.fences !== undefined) {
      if (!c.fences || Object.keys(c.fences).length > 64) throw new Error();
      for (const [key, fence] of Object.entries(c.fences) as [
        string,
        { purge: boolean },
      ][]) {
        if (
          !token(key) ||
          typeof fence?.purge !== "boolean" ||
          c.routes[key]?.route.enabled
        )
          throw new Error();
      }
    }
    return c;
  } catch {
    throw new AgentError("INVALID_CONFIG", 2);
  }
}
export function saveConfig(p: Paths, c: Config): void {
  const value = JSON.stringify(c);
  if (Buffer.byteLength(value) > 65_536) throw new AgentError("CONFIG_QUOTA");
  atomicProtected(p.configFile, value);
}
export async function configTransaction<T>(
  p: Paths,
  action: (config: Config) => Promise<T>,
  apply?: () => Promise<void>,
): Promise<T> {
  protectedDirectory(p.config, true);
  const lock = join(p.config, "write.lock");
  try {
    mkdirSync(lock, { mode: 0o700 });
  } catch {
    throw new AgentError("CONFIGURATION_CONFLICT", 6);
  }
  try {
    const config = loadConfig(p);
    const previous = structuredClone(config);
    const result = await action(config);
    saveConfig(p, config);
    try {
      await apply?.();
    } catch (e) {
      // A rejected runtime registration must not leave the installation enabled
      // on disk. Keep the write lock through rollback and runtime reconciliation.
      saveConfig(p, previous);
      await apply?.().catch(() => undefined);
      throw e;
    }
    return result;
  } finally {
    rmdirSync(lock);
  }
}
