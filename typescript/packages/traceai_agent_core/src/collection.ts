import {
  constants,
  closeSync,
  fstatSync,
  openSync,
  readFileSync,
} from "node:fs";
import type { ResolvedConfig } from "./config.js";

/** Persistent disable wins over a stale process snapshot; any unreadable fence fails closed. */
export function collectionEnabled(config: ResolvedConfig): boolean {
  if (
    !config.enabled ||
    process.env.FI_AGENT_ENABLED === "false" ||
    process.env.FI_AGENT_ENABLED === "0"
  )
    return false;
  let fd: number | undefined;
  try {
    fd = openSync(
      config.paths.manifest,
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    const st = fstatSync(fd);
    if (
      !st.isFile() ||
      st.nlink !== 1 ||
      st.size > 65536 ||
      st.mode & 0o077 ||
      (process.getuid && st.uid !== process.getuid())
    )
      return false;
    const value = JSON.parse(readFileSync(fd, "utf8"));
    return (
      value.schemaVersion === 1 &&
      value.disabled?.[config.agent] !== true &&
      (value.generations?.[config.agent] ?? 0) === config.generation
    );
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "ENOENT";
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}
