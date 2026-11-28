// Optional SDK configuration bridge. Without the SDK, environment controls work.
// The SDK owns publishing this reader; fi-core never creates SDK state.
const CONFIG_KEY = Symbol.for("futureagi.runtime.skills.config.v1");
interface ConfigurationReader {
  v: 1;
  mode(): "on" | "off" | "load_only";
}

/** Shared with the owned pre-host observer; off/load_only install no writer. */
export function runtimeAttributesEnabled(useEnvironment = true): boolean {
  const config = (globalThis as unknown as Record<symbol, ConfigurationReader | undefined>)[CONFIG_KEY];
  if (config?.v === 1 && typeof config.mode === "function") return config.mode() === "on";
  const mode = process.env.FI_SKILLS_TRACING;
  return useEnvironment && mode !== "off" && mode !== "load_only";
}
