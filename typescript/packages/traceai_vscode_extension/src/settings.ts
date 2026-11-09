import { applyEdits, modify, parseTree, getNodeValue, type ParseError } from 'jsonc-parser';
import { contentHash, readProtected, writeProtected } from '@traceai/agent-core';

export const PREFIXES = ['github.copilot.chat.otel.', 'chat.agentHost.otel.'] as const;
const SUFFIXES = ['enabled', 'exporterType', 'otlpEndpoint', 'captureContent', 'protocol', 'headers'] as const;
const KEYS = PREFIXES.flatMap(p => SUFFIXES.filter(s => s !== 'headers' || p === PREFIXES[0]).map(s => p + s));
const SYNC = 'settingsSync.ignoredSettings';
export interface Ownership {
  values: Record<string, unknown>;
  previous: Record<string, unknown>;
  syncAdded: string[];
  syncRemoved: string[];
  syncExisted: boolean;
  previousSync: string[];
  fileExisted: boolean;
}
export function parse(text: string | undefined): Record<string, unknown> {
  if (text === undefined) return {};
  if (Buffer.byteLength(text) > 65536) throw new Error('VSCODE_SETTINGS_TOO_LARGE');
  const errors: ParseError[] = [];
  const tree = parseTree(text, errors, { allowTrailingComma: true });
  if (!tree || tree.type !== 'object' || errors.length) throw new Error('VSCODE_INVALID_SETTINGS_JSONC');
  const walk = (node: typeof tree) => {
    if (node.type === 'object') {
      const keys = node.children?.map(c => c.children?.[0].value) ?? [];
      if (new Set(keys).size !== keys.length) throw new Error('VSCODE_DUPLICATE_SETTING_KEY');
    }
    node.children?.forEach(walk);
  };
  walk(tree);
  return getNodeValue(tree);
}
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
function edit(text: string, key: string, value: unknown): string {
  return applyEdits(text, modify(text, [key], value, {
    formattingOptions: { insertSpaces: true, tabSize: 2, eol: text.includes('\r\n') ? '\r\n' : '\n' }
  }));
}
function syncList(current: Record<string, unknown>): string[] {
  const list = current[SYNC] ?? [];
  if (!Array.isArray(list) || list.some(x => typeof x !== 'string')) throw new Error('VSCODE_INVALID_SYNC_SETTINGS');
  return list;
}
function validate(owned: Ownership): void {
  if (!owned || !owned.values || !owned.previous || !Array.isArray(owned.syncAdded) || !Array.isArray(owned.syncRemoved) ||
      !Array.isArray(owned.previousSync) || owned.previousSync.some(k => typeof k !== 'string') ||
      Object.keys(owned.values).length !== KEYS.length || Object.keys(owned.values).some(k => !KEYS.includes(k)) ||
      Object.keys(owned.previous).some(k => !KEYS.includes(k)) || owned.syncAdded.some(k => !KEYS.includes(k)) ||
      owned.syncRemoved.some(k => !KEYS.includes(k.slice(1)) || !k.startsWith('-'))) throw new Error('VSCODE_INVALID_OWNERSHIP');
}
/** Both harnesses share a keyless loopback destination, but their endpoint semantics differ. */
export function plan(text: string | undefined, port: number, token: string, capture: boolean, previous?: Ownership) {
  if (!Number.isInteger(port) || port < 1024 || port > 65535 || !/^[a-f0-9]{64}$/.test(token)) throw new Error('VSCODE_INVALID_RELAY_ADDRESS');
  const current = parse(text);
  if (current['telemetry.telemetryLevel'] === 'off') throw new Error('VSCODE_TELEMETRY_DISABLED');
  if (previous) {
    validate(previous);
    for (const [k, v] of Object.entries(previous.values)) if (!equal(current[k], v)) throw new Error('VSCODE_OWNED_SETTING_CHANGED');
  }
  // Never silently take over another exporter or copy header secrets into an ownership record.
  for (const p of PREFIXES) {
    for (const suffix of ['headers', 'resourceAttributes', 'outfile', 'dbSpanExporter.enabled', 'captureIdentity']) {
      const key = p + suffix, v = current[key];
      if (previous && key in previous.values) continue;
      if (v !== undefined && v !== false && v !== '' && v !== null && !equal(v, {})) throw new Error('VSCODE_OTHER_TELEMETRY_CONFIGURATION');
    }
    if (!previous && current[p + 'enabled'] === true) throw new Error('VSCODE_EXISTING_TELEMETRY_SETTINGS');
  }
  const values: Record<string, unknown> = {};
  const base = `http://127.0.0.1:${port}/${token}`;
  for (const p of PREFIXES) Object.assign(values, {
    [p + 'enabled']: true, [p + 'exporterType']: 'otlp-http',
    [p + 'otlpEndpoint']: base + (p === PREFIXES[1] ? '/v1/traces' : ''),
    [p + 'captureContent']: capture, [p + 'protocol']: 'http/protobuf',
    ...(p === PREFIXES[0] ? { [p + 'headers']: {} } : {})
  });
  const list = syncList(current);
  if (previous && list.some(k => k.startsWith('-') && KEYS.includes(k.slice(1)))) throw new Error('VSCODE_SYNC_PROTECTION_CHANGED');
  const ownership: Ownership = previous ? { ...previous, values } : {
    values, previous: Object.fromEntries(KEYS.filter(k => k in current).map(k => [k, current[k]])),
    syncAdded: KEYS.filter(k => !list.includes(k)), syncRemoved: list.filter(k => KEYS.includes(k.slice(1)) && k.startsWith('-')),
    syncExisted: SYNC in current, previousSync: [...list], fileExisted: text !== undefined
  };
  let next = text ?? '{}\n';
  for (const [k, v] of Object.entries(values)) if (!equal(current[k], v)) next = edit(next, k, v);
  const ignored = [...list.filter(k => !ownership.syncRemoved.includes(k))];
  for (const k of KEYS) if (!ignored.includes(k)) ignored.push(k);
  if (!equal(ignored, list)) next = edit(next, SYNC, ignored);
  return { text: next, ownership };
}
export function removal(text: string | undefined, owned: Ownership) {
  validate(owned);
  const current = parse(text), conflicts: string[] = [];
  let next = text ?? '{}\n';
  for (const [k, v] of Object.entries(owned.values)) {
    if (equal(current[k], owned.previous[k])) continue; // Interrupted cleanup is idempotent.
    if (!equal(current[k], v)) { conflicts.push(k); continue; }
    next = edit(next, k, owned.previous[k]);
  }
  // Keep sync protection on any user-modified telemetry entry until the conflict is resolved.
  const currentSync = syncList(current);
  const expectedSync = [...owned.previousSync.filter(k => !owned.syncRemoved.includes(k)), ...owned.syncAdded];
  const list = !conflicts.length && equal(currentSync, expectedSync) ? [...owned.previousSync] :
    currentSync.filter(k => !owned.syncAdded.includes(k) || conflicts.includes(k));
  for (const k of owned.syncRemoved) if (!conflicts.includes(k.slice(1)) && !list.includes(k)) list.push(k);
  if (!equal(list, current[SYNC])) next = edit(next, SYNC, list.length || owned.syncExisted ? list : undefined);
  return { text: next, conflicts };
}
export async function save(path: string, old: string | undefined, next: string) {
  if (old !== next) await writeProtected(path, next, old === undefined ? null : contentHash(old), false);
}
export const readSettings = (path: string) => readProtected(path, false);
