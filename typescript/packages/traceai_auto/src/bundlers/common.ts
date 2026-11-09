import { existsSync } from "node:fs";
import { basename, resolve } from "node:path";
import { pathToFileURL } from "node:url";
export interface PluginOptions {
  /** Required for Rollup (which has no platform flag). Never enable for browser/edge. */
  server?: boolean;
  /** Override only when deploying a separately copied dist/agent directory. */
  agentPath?: string;
}
export const MARKER = "/* traceai:auto:preload */";
export function agentPath(options: PluginOptions): string {
  const deployed = resolve(__dirname, "../../agent/preload.cjs");
  const file = options.agentPath ? resolve(options.agentPath) : existsSync(deployed) ? deployed : resolve(__dirname, "../../dist/agent/preload.cjs");
  if (!existsSync(file)) throw new Error(`[traceai:auto] Agent preload not found at ${file}. Build @traceai/auto or set agentPath to a deployed preload.cjs.`);
  return file;
}
export function registration(file: string, esm: boolean): string {
  const load = esm ? `await import(${JSON.stringify(pathToFileURL(file).href)});` : `require(${JSON.stringify(file)});`;
  return `${MARKER}try { ${load} } catch (_) { try { console.warn("[traceai:auto] Preload failed; the application will continue."); } catch (_) {} }\n`;
}
/** ESM dependencies are linked before static preload imports run. Load the real
 * entry dynamically, only after the loader has registered, preserving its code/maps. */
export function esmBootstrap(code: string, file: string, payload: string, exports: string[] = []): string {
  if (exports.length) throw new Error("[traceai:auto] Exported ESM entries require an external --require preload (or Next instrumentation register()); a bootstrap cannot preserve live exports.");
  const shebang = code.startsWith("#!") ? code.split("\n", 1)[0] + "\n" : "";
  const specifier = "./" + basename(payload).split("/").map(encodeURIComponent).join("/");
  return shebang + registration(file, true) + `await import(${JSON.stringify(specifier)});\n`;
}
export function payloadName(file: string): string { return file.replace(/\.[cm]?js$/, ".traceai-app.mjs"); }
export function prepend(code: string, line: string): string {
  if (code.includes(MARKER)) return code;
  // A statement before a directive prologue otherwise silently disables strict mode.
  // Repeat the existing directive on the injected line; no extra mapping shift is needed.
  let prologue = code.replace(/^#![^\n]*(?:\n|$)/, "");
  const directive = /^(?:\s|\/\*[\s\S]*?\*\/|\/\/[^\n]*(?:\n|$))*("(?:[^"\\\r\n]|\\.)*"|'(?:[^'\\\r\n]|\\.)*')[ \t]*(?:;|(?=\r?\n|$))/;
  for (let found; (found = directive.exec(prologue)); prologue = prologue.slice(found[0].length)) {
    if (found[1] === '"use strict"' || found[1] === "'use strict'") { line = '"use strict";' + line; break; }
  }
  if (code.startsWith("#!")) { const end = code.indexOf("\n"); return end < 0 ? `${code}\n${line}` : code.slice(0, end + 1) + line + code.slice(end + 1); }
  return line + code;
}
export function serverMode(platform: boolean | undefined, options: PluginOptions, tool: string): boolean {
  if (options.server === true && platform === false) throw new Error(`[traceai:auto] ${tool}: server:true conflicts with a browser/edge target. Use a separate server build.`);
  if (platform === undefined && options.server === undefined) throw new Error(`[traceai:auto] ${tool}: specify { server: true } for Node server builds or { server: false } for browser/edge builds.`);
  return platform === false ? false : options.server ?? platform ?? false;
}
