import { applyEdits, modify, parseTree, getNodeValue, type ParseError } from "jsonc-parser";
import { contentHash, readProtected, writeProtected } from "@traceai/agent-core";

export type Harness = "agent-host" | "legacy";
export const prefixes: Record<Harness, string> = {
  "agent-host": "chat.agentHost.otel.",
  legacy: "github.copilot.chat.otel.",
};
export const EDITOR_HOLD = "VSCODE_GUI_AND_TRANSPORT_QUALIFICATION_REQUIRED";
// Deliberately empty. Upstream main docs are not a tested released host range.
export const qualifiedEditorVersions: readonly string[] = [];
export const ownedSuffixes = ["enabled", "exporterType", "otlpEndpoint", "captureContent"] as const;
type Value = string | boolean;
export interface SettingsOwnership {
  schemaVersion: 1;
  harness: Harness;
  /** Only keys created by this adapter, never values from arbitrary settings. */
  values: Record<string, Value>;
}
export interface SettingsPlan {
  text: string;
  ownership: SettingsOwnership;
  expected: string | null;
}

/** Reject ambiguous duplicate keys as well as malformed JSONC; never echo file text. */
export function parseSettings(text: string | undefined): Record<string, unknown> {
  if (text === undefined) return {};
  if (Buffer.byteLength(text) > 64 * 1024) throw new Error("VSCODE_SETTINGS_TOO_LARGE");
  const errors: ParseError[] = [];
  const tree = parseTree(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.length || tree?.type !== "object") throw new Error("VSCODE_INVALID_SETTINGS_JSONC");
  const visit = (node: typeof tree): void => {
    if (node.type === "object") {
      const seen = new Set<string>();
      for (const property of node.children ?? []) {
        const key = property.children?.[0]?.value as string;
        if (seen.has(key)) throw new Error("VSCODE_DUPLICATE_SETTING_KEY");
        seen.add(key);
      }
    }
    for (const child of node.children ?? []) visit(child);
  };
  visit(tree);
  return getNodeValue(tree) as Record<string, unknown>;
}
export function harness(value: string | undefined): Harness | undefined {
  if (value === undefined || value === "") return undefined;
  if (value !== "agent-host" && value !== "legacy") throw new Error("VSCODE_INVALID_HARNESS");
  return value;
}

/** Settings candidates carry no keys. Only an explicit local capture endpoint is accepted.
 * This does NOT provide a receiver, launch strategy or qualification bypass.
 */
export function localEndpoint(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new Error("VSCODE_LOOPBACK_ENDPOINT_REQUIRED"); }
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("VSCODE_LOOPBACK_ENDPOINT_REQUIRED");
  }
  return url.origin;
}
function assertOwnership(ownership: SettingsOwnership, selected: Harness): void {
  if (ownership.schemaVersion !== 1 || ownership.harness !== selected) throw new Error("VSCODE_OWNERSHIP_MISMATCH");
  const prefix = prefixes[selected];
  const allowed = ownedSuffixes.map((suffix) => prefix + suffix);
  if (Object.keys(ownership.values).length !== allowed.length ||
      Object.keys(ownership.values).some((key) => !allowed.includes(key))) {
    throw new Error("VSCODE_INVALID_OWNERSHIP");
  }
  if (ownership.values[prefix + "enabled"] !== true ||
      ownership.values[prefix + "exporterType"] !== "otlp-http" ||
      typeof ownership.values[prefix + "captureContent"] !== "boolean" ||
      typeof ownership.values[prefix + "otlpEndpoint"] !== "string") {
    throw new Error("VSCODE_INVALID_OWNERSHIP");
  }
  localEndpoint(ownership.values[prefix + "otlpEndpoint"] as string);
}
function edit(text: string, key: string, value: Value | undefined): string {
  return applyEdits(text, modify(text, [key], value, {
    formattingOptions: { insertSpaces: true, tabSize: 2, eol: text.includes("\r\n") ? "\r\n" : "\n" },
  }));
}
function has(object: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(object, key);
}
function assertNoOtherExporters(current: Record<string, unknown>, selected: Harness, ownership?: SettingsOwnership): void {
  const prefix = prefixes[selected];
  for (const [key, value] of Object.entries(current)) {
    if (!Object.values(prefixes).some((p) => key.startsWith(p))) continue;
    if (ownership && has(ownership.values, key)) continue;
    // Even a matching unowned value belongs to the user, not this installer.
    if (key.startsWith(prefix) && ownedSuffixes.some((s) => key === prefix + s)) {
      throw new Error("VSCODE_EXISTING_TELEMETRY_SETTINGS");
    }
    if (value !== false && value !== "" && value !== null) {
      throw new Error("VSCODE_OTHER_TELEMETRY_CONFIGURATION");
    }
  }
  if (current["telemetry.telemetryLevel"] === "off") throw new Error("VSCODE_TELEMETRY_DISABLED");
}

/** Pure, credential-free merge foundation. Not called by install until the editor gate clears. */
export function planSettings(text: string | undefined, selected: Harness, endpoint: string, captureContent: boolean,
                             previous?: SettingsOwnership): SettingsPlan {
  if (!Object.prototype.hasOwnProperty.call(prefixes, selected)) throw new Error("VSCODE_INVALID_HARNESS");
  if (typeof captureContent !== "boolean") throw new Error("VSCODE_INVALID_CAPTURE_POLICY");
  const current = parseSettings(text);
  if (previous) {
    assertOwnership(previous, selected);
    for (const [key, value] of Object.entries(previous.values)) {
      if (!has(current, key) || current[key] !== value) throw new Error("VSCODE_OWNED_SETTING_CHANGED");
    }
  }
  assertNoOtherExporters(current, selected, previous);
  const prefix = prefixes[selected];
  const values: Record<string, Value> = {
    [prefix + "enabled"]: true,
    [prefix + "exporterType"]: "otlp-http",
    [prefix + "otlpEndpoint"]: localEndpoint(endpoint),
    [prefix + "captureContent"]: captureContent,
  };
  let next = text ?? "{}\n";
  for (const [key, value] of Object.entries(values)) {
    if (current[key] !== value) next = edit(next, key, value);
  }
  return { text: next, ownership: { schemaVersion: 1, harness: selected, values }, expected: text === undefined ? null : contentHash(text) };
}
export function planRemoval(text: string | undefined, ownership: SettingsOwnership): { text: string; conflicts: string[] } {
  assertOwnership(ownership, ownership.harness);
  const current = parseSettings(text);
  let next = text ?? "{}\n";
  const conflicts: string[] = [];
  for (const [key, value] of Object.entries(ownership.values)) {
    if (!has(current, key)) continue;
    if (current[key] !== value) { conflicts.push(key); continue; }
    next = edit(next, key, undefined);
  }
  return { text: next, conflicts };
}
/** Internal protected writer, exercised on synthetic files only while editor install is held. */
export async function writeSettingsPlan(path: string, plan: SettingsPlan): Promise<void> {
  await writeProtected(path, plan.text, plan.expected, false);
}
export async function readSettingsFile(path: string): Promise<string | undefined> {
  return readProtected(path, false);
}
