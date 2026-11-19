import { join } from "node:path";
import { readJson } from "@traceai/agent-core/files";
import type { ResolvedConfig } from "@traceai/agent-core/config";

export interface Registration {
  schemaVersion: 1;
  runtime: string;
  node: string;
  digest: string;
  command: string;
  hooks: string;
  manifest: string;
  userScope?: boolean;
  projectBindings?: string[];
}
export const registrationPath = (config: ResolvedConfig) =>
  join(config.paths.configDir, "goose-plugin.json");

/** Global discovery is not global consent: project installs authorize only the canonical root. */
export function registrationApplies(
  registration: Registration | undefined,
  config: ResolvedConfig,
): boolean {
  return (
    registration?.userScope === true ||
    (!!config.paths.projectBinding &&
      registration?.projectBindings?.includes(config.paths.projectBinding) ===
        true)
  );
}
export async function registeredFor(config: ResolvedConfig): Promise<boolean> {
  return registrationApplies(
    await readJson<Registration>(registrationPath(config)),
    config,
  );
}
