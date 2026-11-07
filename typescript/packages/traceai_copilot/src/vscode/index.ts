import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { access, readFile, realpath } from 'node:fs/promises';
import { constants } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:net';
import { dirname, join } from 'node:path';
import { contentHash, readProtected, readJson, removeProtected, updateJson,
  type AdapterInspection, type AdapterContext, type CoreAdapter } from '@traceai/agent-core';
import { EXTENSION_ID, installationPath, heartbeatPath, bootstrapPath, readBootstrap, readInstallation, synchronize, parse, plan, readSettings,
  type Installation, type Heartbeat, type Bootstrap } from '@traceai/vscode-extension';
import { discoverSettings, type DiscoveryOptions } from './discovery.js';
export { inspectEditor, inspectSettings, discoverSettings } from './discovery.js';
export { EDITOR_HOLD, qualifiedEditorVersions } from './settings.js';
export type { Harness } from './settings.js';
export type { EditorInspection, EditorLocation, DiscoveryOptions } from './discovery.js';

const exec = promisify(execFile);
export interface VscodeOptions extends DiscoveryOptions {
  /** Dependency injection for synthetic host tests; never a CLI flag or repository setting. */
  runCommand?: (cli: string, args: string[]) => Promise<string>;
}
export async function findCode(options: DiscoveryOptions = {}) {
  const env = options.env ?? process.env, platform = options.platform ?? process.platform;
  const insiders = env.FI_COPILOT_VSCODE_CHANNEL === 'insiders';
  const name = insiders ? 'code-insiders' : 'code';
  const app = `/Applications/Visual Studio Code${insiders ? ' - Insiders' : ''}.app/Contents/Resources/app`;
  const candidates = [
    ...(platform === 'darwin' ? [join(app, 'bin', name)] : []),
    ...(env.PATH ?? '').split(':').filter(p => p.startsWith('/')).map(p => join(p, name))
  ];
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      // Applications is owned by root: bundle metadata is public, not a private user config file.
      let version: string | undefined;
      if (candidate.startsWith(app)) {
        const pkg = JSON.parse(await readFile(join(app, 'package.json'), 'utf8'));
        if (typeof pkg.version === 'string' && /^\d+\.\d+\.\d+/.test(pkg.version)) version = pkg.version;
      }
      return { cli: candidate, version };
    } catch { /* Try the next host location without surfacing filesystem text. */ }
  }
  throw new Error('VSCODE_CLI_NOT_FOUND');
}
async function freePort(): Promise<number> {
  const server = createServer();
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = (server.address() as { port: number }).port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}
export function createVscodeAdapter(options: VscodeOptions = {}): CoreAdapter {
  const env = options.env ?? process.env;
  const file = installationPath(options);
  const run = options.runCommand ?? (async (cli: string, args: string[]) => {
    try { return (await exec(cli, args, { env, timeout: 30000, maxBuffer: 256 * 1024, windowsHide: true })).stdout; }
    catch { throw new Error('VSCODE_CLI_FAILED'); }
  });
  const args = (r: Pick<Installation, 'userData'>, tail: string[]) => r.userData ? ['--user-data-dir', r.userData, ...tail] : tail;
  const bundledVersion: string = require('@traceai/vscode-extension/package.json').version;
  async function installedVersion(r: Pick<Installation, 'cli'|'userData'>): Promise<string | undefined> {
    const line = (await run(r.cli, args(r, ['--list-extensions', '--show-versions']))).split(/\r?\n/)
      .find(line => line.toLowerCase().split('@')[0] === EXTENSION_ID);
    return line?.split('@')[1];
  }
  const installed = async (r: Pick<Installation, 'cli'|'userData'>) => !!await installedVersion(r);
  async function inspect(): Promise<AdapterInspection> {
    try {
      const r = await readInstallation(file);
      if (!r) return { registered: false, activated: false, issues: ['VSCODE_EXTENSION_NOT_INSTALLED'] };
      const version = await installedVersion(r), present = !!version;
      const settings = parse(await readSettings(r.settingsPath));
      const ignored = settings['settingsSync.ignoredSettings'];
      const protectedSync = r.ownership && Array.isArray(ignored) && Object.keys(r.ownership.values).every(k => ignored.includes(k) && !ignored.includes('-' + k));
      const match = r.applied && r.ownership && protectedSync && Object.entries(r.ownership.values).every(([k,v]) => JSON.stringify(settings[k]) === JSON.stringify(v));
      const hb = await readJson<Heartbeat>(heartbeatPath(options));
      const live = !!hb && hb.id === r.id && Date.now() - Date.parse(hb.at) < 10000 && Date.now() >= Date.parse(hb.at) && hb.active;
      const issues: string[] = [];
      if (!present) issues.push('VSCODE_EXTENSION_NOT_INSTALLED');
      if (present && version !== bundledVersion) issues.push('VSCODE_EXTENSION_VERSION_MISMATCH');
      if (r.applied && !match) issues.push('VSCODE_OWNED_SETTING_CHANGED');
      if (r.applied && !live) issues.push('VSCODE_RELOAD_REQUIRED');
      if (hb?.id === r.id && hb.lastError && /^[A-Z][A-Z0-9_]{0,80}$/.test(hb.lastError)) issues.push(hb.lastError);
      return { registered: present && (r.applied ? !!match : r.configured === true), activated: live, trust: 'unknown', issues,
        detail: live ? 'VSCODE_RELAY_ACTIVE' : 'VSCODE_RELOAD_REQUIRED' };
    } catch { return { registered: false, activated: false, issues: ['VSCODE_INSPECTION_FAILED'] }; }
  }
  async function install(context: AdapterContext): Promise<AdapterInspection> {
    const location = discoverSettings(options);
    if (location.profile !== 'default') throw new Error('VSCODE_DEFAULT_PROFILE_REQUIRED');
    const host = await findCode(options);
    let r = await readInstallation(file);
    const projectDir = context.scope === 'project' && context.projectDir ? await realpath(context.projectDir) : undefined;
    if (context.scope === 'project' && !projectDir) throw new Error('PROJECT_DIRECTORY_REQUIRED');
    if (r && (r.settingsPath !== location.userSettings || r.projectDir !== projectDir || r.scope !== context.scope)) throw new Error('VSCODE_DIFFERENT_SCOPE_INSTALLED');
    const capture = context.config.captureContent && !context.config.hideInputs && !context.config.hideOutputs && !context.config.hideErrorDetails;
    if (!r) {
      r = { version: 1, id: randomBytes(16).toString('hex'), token: randomBytes(32).toString('hex'), port: await freePort(),
        settingsPath: location.userSettings, scope: context.scope, projectDir, channel: location.channel, cli: host.cli,
        userData: env.FI_COPILOT_VSCODE_USER_DATA_DIR, extensionOwned: false, applied: false, captureContent: capture };
      // Validate BEFORE installing an extension or recording ownership.
      plan(await readSettings(r.settingsPath), r.port, r.token, capture);
      r.extensionOwned = !await installed(r);
      await updateJson<Installation>(file, old => { if (old) throw new Error('VSCODE_CONCURRENT_SETUP'); return r!; });
    }
    if (await installedVersion(r) !== bundledVersion) {
      const vsix = join(dirname(dirname(require.resolve('@traceai/vscode-extension'))), 'extension.vsix');
      await run(r.cli, args(r, ['--install-extension', vsix]));
      if (await installedVersion(r) !== bundledVersion) throw new Error('VSCODE_EXTENSION_INSTALL_FAILED');
    }
    const bootstrap = bootstrapPath(options.home);
    await updateJson<Bootstrap>(bootstrap, old => {
      if (old && old.id !== r!.id) throw new Error('VSCODE_DIFFERENT_INSTALLATION_ACTIVE');
      return { version: 1, id: r!.id, configDir: context.config.paths.configDir, stateDir: context.config.paths.stateDir };
    });
    await synchronize(file, true, capture);
    return inspect();
  }
  async function uninstall(): Promise<AdapterInspection> {
    const r = await readInstallation(file);
    if (!r) return { registered: false, activated: false };
    const conflicts = await synchronize(file, false);
    if (conflicts.length) return { registered: true, activated: false, issues: ['VSCODE_OWNED_SETTING_CHANGED'] };
    if (r.extensionOwned && await installed(r)) await run(r.cli, args(r, ['--uninstall-extension', EXTENSION_ID]));
    if (r.extensionOwned && await installed(r)) return { registered: true, issues: ['VSCODE_EXTENSION_REMOVE_FAILED'] };
    const text = await readProtected(file);
    if (text) await removeProtected(file, contentHash(text));
    if ((await readBootstrap(options.home))?.id === r.id) {
      const bpath = bootstrapPath(options.home), btext = await readProtected(bpath);
      if (btext) await removeProtected(bpath, contentHash(btext));
    }
    return { registered: false, activated: false };
  }
  return {
    id: 'copilot-vscode', version: '0.1.0', requiresWorker: false,
    diagnostics: {
      VSCODE_RELAY_ACTIVE: 'The installed extension has a fresh relay heartbeat. Last export is reported separately from activation.',
      VSCODE_CLI_NOT_FOUND: 'Install Visual Studio Code or set FI_COPILOT_VSCODE_CHANNEL=insiders for Code Insiders. On macOS the application bundle CLI is detected without a shell PATH change.',
      VSCODE_DEFAULT_PROFILE_REQUIRED: 'Copilot telemetry settings are application-scoped. Use the Default VS Code profile.',
      VSCODE_EXTENSION_INSTALL_FAILED: 'The bundled extension was not installed successfully. Verify that the VS Code CLI can install local VSIX packages.',
      VSCODE_RELOAD_REQUIRED: 'Reload VS Code after setup or changing tracing. Both native harnesses use the extension-host relay.',
      WORKSPACE_ATTRIBUTION_REQUIRED: 'This native exporter did not identify its workspace. Project-scoped traffic was dropped; use an explicitly user-scoped installation to trace all local windows.',
      VSCODE_OWNED_SETTING_CHANGED: 'A managed tracing setting was edited. Restore that setting or remove it manually before retrying uninstall.'
    },
    async detect() {
      try {
        if (discoverSettings(options).profile !== 'default') return { supported: false, reason: 'VSCODE_DEFAULT_PROFILE_REQUIRED' };
        const host = await findCode(options); return { supported: true, version: host.version };
      } catch { return { supported: false, reason: 'VSCODE_CLI_NOT_FOUND' }; }
    },
    install, update: install, inspect, uninstall,
    uninstallUnconfigured: uninstall,
    async onEnabledChange(_options, enabled) {
      // Core already fences export before calling this hook. Keep the native
      // loopback destination stable; changing it restarts the Copilot host.
      if (enabled && (await synchronize(file, true)).length) throw new Error('VSCODE_OWNED_SETTING_CHANGED');
    }
  };
}
export const vscodeAdapter = createVscodeAdapter();
