import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';
import { contentHash, controlPaths, readJson, removeProtected, updateJson, withFileLock, type ResolveOptions } from '@traceai/agent-core';
import { plan, removal, readSettings, save, type Ownership } from './settings';
export const EXTENSION_ID = 'future-agi.vscode-extension';
export interface Bootstrap { version: 1; id: string; configDir: string; stateDir: string; }
/** Non-secret pointer, not another key store. Dock launches need to locate CLI-selected XDG paths. */
export const bootstrapPath = (home = homedir()) => join(home, '.config', 'futureagi', 'agents', 'vscode-relay-bootstrap.json');
export async function readBootstrap(home?: string): Promise<Bootstrap | undefined> {
  const b = await readJson<Bootstrap>(bootstrapPath(home));
  if (b && (b.version !== 1 || !/^[a-f0-9]{32}$/.test(b.id) || !isAbsolute(b.configDir) || !isAbsolute(b.stateDir))) throw new Error('VSCODE_INVALID_BOOTSTRAP');
  return b;
}
export interface Installation {
  version: 1;
  id: string;
  port: number;
  token: string;
  settingsPath: string;
  projectDir?: string;
  scope: 'user' | 'project';
  channel: 'stable' | 'insiders';
  cli: string;
  userData?: string;
  extensionOwned: boolean;
  applied: boolean;
  configured?: boolean;
  ownership?: Ownership;
  captureContent: boolean;
}
export interface Heartbeat {
  id: string;
  at: string;
  active: boolean;
  lastExport?: string;
  exported: number;
  dropped: number;
  lastError?: string;
}
export function installationPath(options: Pick<ResolveOptions, 'home'|'env'> = {}) {
  return join(controlPaths({ ...options, agent: 'copilot-vscode' }).configDir, 'hosts', 'copilot-vscode', 'relay.json');
}
export function heartbeatPath(options: Pick<ResolveOptions, 'home'|'env'> = {}) {
  return join(controlPaths({ ...options, agent: 'copilot-vscode' }).stateDir, 'copilot-vscode-relay.json');
}
export async function readInstallation(path: string): Promise<Installation | undefined> {
  const r = await readJson<Installation>(path);
  if (r && (r.version !== 1 || !/^[a-f0-9]{64}$/.test(r.token) || !/^[a-f0-9]{32}$/.test(r.id) ||
    !Number.isInteger(r.port) || r.port < 1024 || r.port > 65535 || !['user','project'].includes(r.scope) ||
    !['stable','insiders'].includes(r.channel) || !r.settingsPath?.endsWith('/User/settings.json') ||
    typeof r.cli !== 'string' || r.scope === 'project' && !r.projectDir)) throw new Error('VSCODE_INVALID_INSTALLATION');
  return r;
}
/** Journal ownership before host edits, so interrupted setup can always be restored. */
export async function synchronize(path: string, enabled: boolean, capture?: boolean): Promise<string[]> {
  return withFileLock(path + '.lifecycle', async () => {
    const r = await readInstallation(path);
    if (!r) return [];
    const old = await readSettings(r.settingsPath);
    if (!enabled) {
      if (!r.ownership) return [];
      const next = removal(old, r.ownership);
      if (r.ownership.fileExisted === false && !next.conflicts.length && /^\s*\{\s*\}\s*$/.test(next.text)) {
        if (old !== undefined) await removeProtected(r.settingsPath, contentHash(old), false);
      } else await save(r.settingsPath, old, next.text);
      if (!next.conflicts.length) await updateJson<Installation>(path, () => ({ ...r, applied: false, ownership: undefined }));
      return next.conflicts;
    }
    const next = plan(old, r.port, r.token, capture ?? r.captureContent, r.ownership);
    if (r.applied && next.text === old && r.captureContent === (capture ?? r.captureContent)) return [];
    await updateJson<Installation>(path, () => ({ ...r, ownership: next.ownership, captureContent: capture ?? r.captureContent }));
    await save(r.settingsPath, old, next.text);
    await updateJson<Installation>(path, () => ({ ...r, ownership: next.ownership, captureContent: capture ?? r.captureContent, applied: true, configured: true }));
    return [];
  });
}
