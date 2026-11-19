import { constants, promises as fs } from "node:fs";
import { type CoreAdapter, type AdapterContext } from "@traceai/agent-core";
import { VERSION, inspectHooks, installHooks, uninstallHooks } from "./registration";
import { isRecord } from "./transcript";

export { VERSION } from "./registration";
export { handleHook } from "./runtime";
export interface DesktopHost { product: "Devin" | "Windsurf"; version: string }
/** Intentionally empty until a pinned GUI build and account produce real Cascade traces.
 * The CLI named `devin` is not a Desktop Cascade version probe.
 */
const QUALIFIED_HOSTS: readonly string[] = [];
export function qualification(host?: DesktopHost): { supported: boolean; version?: string; reason: string } {
  if (!host) return { supported: false, reason: "CASCADE_DESKTOP_NOT_FOUND" };
  const parts = host.version.split(".").map(Number);
  const hasDocumentedTranscript = parts.length === 3 && parts.every(Number.isSafeInteger) &&
    (parts[0] > 1 || (parts[0] === 1 && (parts[1] > 9566 || (parts[1] === 9566 && parts[2] >= 9))));
  if (!hasDocumentedTranscript) return { supported: false, version: host.version, reason: "CASCADE_TRANSCRIPT_HOOK_UNAVAILABLE" };
  return { supported: QUALIFIED_HOSTS.includes(`${host.product}@${host.version}`), version: host.version, reason: "CASCADE_GUI_QUALIFICATION_REQUIRED" };
}
async function desktopHost(): Promise<DesktopHost | undefined> {
  const candidates = process.platform === "darwin" ? [
    ["Devin", "/Applications/Devin.app/Contents/Resources/app/package.json"],
    ["Windsurf", "/Applications/Windsurf.app/Contents/Resources/app/package.json"],
  ] : process.platform === "linux" ? [
    ["Devin", "/usr/share/devin/resources/app/package.json"],
    ["Windsurf", "/usr/share/windsurf/resources/app/package.json"],
  ] : [];
  for (const [product, path] of candidates) {
    try {
      const file = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const st = await file.stat();
        if (!st.isFile() || st.size > 256 * 1024 || st.mode & 0o022 ||
          (st.uid !== 0 && process.getuid && st.uid !== process.getuid())) continue;
        const json: unknown = JSON.parse(await file.readFile("utf8"));
        if (isRecord(json) && typeof json.version === "string" && /^\d+\.\d+\.\d+$/.test(json.version))
          return { product: product as DesktopHost["product"], version: json.version };
      } finally { await file.close(); }
    } catch { /* No executable probing, user auth files, or account mutation. */ }
  }
  return undefined;
}
async function qualifiedInstall(context: AdapterContext) {
  const host = qualification(await desktopHost());
  if (!host.supported) throw new Error(host.reason);
  return installHooks(context);
}
export const cascadeAdapter: CoreAdapter = {
  id: "devin-cascade", version: VERSION, requiresWorker: false,
  detect: async () => qualification(await desktopHost()),
  install: qualifiedInstall,
  inspect: inspectHooks,
  uninstall: uninstallHooks,
};
