import { constants, promises as fs } from "node:fs";
import { dirname } from "node:path";
import { parse, type ParseError } from "jsonc-parser";
import { safeParents } from "@traceai/agent-core";

/** System settings can force native export above the user scope. Read only policy booleans; never copy settings into state. */
export async function systemPolicyConflict(): Promise<string | undefined> {
  const path =
    process.env.GEMINI_CLI_SYSTEM_SETTINGS_PATH ??
    (process.platform === "darwin"
      ? "/Library/Application Support/GeminiCli/settings.json"
      : "/etc/gemini-cli/settings.json");
  let file;
  try {
    await safeParents(dirname(path));
    file = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const stat = await file.stat();
    if (
      !stat.isFile() ||
      stat.nlink !== 1 ||
      stat.size > 65536 ||
      stat.mode & 0o022 ||
      (stat.uid !== 0 && stat.uid !== process.getuid?.())
    )
      return "UNSAFE_SYSTEM_SETTINGS";
    const bytes = Buffer.alloc(65537);
    const { bytesRead } = await file.read(bytes, 0, bytes.length, 0);
    if (bytesRead > 65536) return "UNSAFE_SYSTEM_SETTINGS";
    const errors: ParseError[] = [],
      settings = parse(bytes.subarray(0, bytesRead).toString("utf8"), errors, {
        allowTrailingComma: true,
      });
    if (
      errors.length ||
      !settings ||
      typeof settings !== "object" ||
      Array.isArray(settings)
    )
      return "INVALID_SYSTEM_SETTINGS";
    if (settings.telemetry?.enabled === true)
      return "NATIVE_TELEMETRY_CONFLICT";
    if (settings.hooksConfig?.enabled === false) return "HOST_HOOKS_DISABLED";
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT")
      return "SYSTEM_SETTINGS_UNREADABLE";
  } finally {
    await file?.close();
  }
}
