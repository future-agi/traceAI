import { join, isAbsolute } from "node:path";
import { homedir } from "node:os";
import { readProtected } from "@traceai/agent-core/files";
import { parse } from "yaml";

export const VERSION = "0.1.0"; // x-release-please-version
export const TESTED_HOST = "1.54.0";
export const EVENTS = [
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "Stop",
  "SessionEnd",
] as const;
/** Goose Paths::get_dir uses XDG on Unix, or an absolute GOOSE_PATH_ROOT. */
export function goosePaths(env = process.env, home = homedir()) {
  const root = env.GOOSE_PATH_ROOT;
  if (root && !isAbsolute(root))
    throw new Error("GOOSE_PATH_ROOT_MUST_BE_ABSOLUTE");
  return {
    config: root
      ? join(root, "config")
      : join(env.XDG_CONFIG_HOME || join(home, ".config"), "goose"),
    data: root
      ? join(root, "data")
      : join(env.XDG_DATA_HOME || join(home, ".local", "share"), "goose"),
    plugin: join(root || home, ".agents", "plugins", "futureagi-goose"),
  };
}
/** Never silently replace a user's native telemetry destination or double-export it. */
export async function nativeConflict(env = process.env): Promise<boolean> {
  if (["true", "1"].includes(env.OTEL_SDK_DISABLED?.toLowerCase() || ""))
    return false;
  const exporter = env.OTEL_TRACES_EXPORTER?.toLowerCase();
  if (exporter === "none") return false;
  if (
    exporter === "otlp" ||
    exporter === "console" ||
    exporter === "stdout" ||
    exporter === ""
  )
    return true;
  if (env.OTEL_EXPORTER_OTLP_ENDPOINT || env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)
    return true;
  const text = await readProtected(
    join(goosePaths(env).config, "config.yaml"),
    false,
  );
  if (!text) return false;
  const config = parse(text, { maxAliasCount: 0 });
  return !!config?.otel_exporter_otlp_endpoint;
}
