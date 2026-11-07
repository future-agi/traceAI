import { execFile } from 'node:child_process';
import { constants, promises as fs } from 'node:fs';
import { delimiter, dirname, isAbsolute, join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import {
  readJson, readProtected, writeProtected, ensurePrivateDirectory, safeParents, contentHash,
  type AdapterContext, type AdapterInspection, type CoreAdapter
} from '@traceai/agent-core';
import { EVENTS } from './mapper.js';

export const PLUGIN_NAME = 'futureagi-tracing';
export const VERSION = '0.1.0'; // x-release-please-version
export const TESTED_HOST_VERSION = '1.5.2';
interface Plugin {name: string; enabled: boolean; path: string; version?: string}
interface Receipt {schemaVersion: 1; hostVersion: string; installedPath: string; hashes: Record<string, string>; node?: string; runtimePath?: string}
export const supportedHostVersion = (version: string): boolean => version === version.trim() && /^1\.\d+\.\d+$/.test(version);
/** Keep a stable PATH symlink (e.g. a package-manager node link), never copy Node. */
export async function pinnedNode(execPath = process.execPath, searchPath = process.env.PATH ?? ''): Promise<string> {
  const target = await fs.realpath(execPath);
  for (const directory of searchPath.split(delimiter)) {
    if (!isAbsolute(directory)) continue;
    const candidate = join(directory, 'node');
    if (candidate.includes('/_npx/') || candidate.includes('/.npm/_')) continue;
    try {
      if (await fs.realpath(candidate) === target) { await fs.access(candidate, constants.X_OK); return candidate; }
    } catch { /* not this interpreter */ }
  }
  if (execPath.includes('/_npx/') || execPath.includes('/.npm/_')) throw new Error('PERSISTENT_NODE_REQUIRED');
  return execPath;
}
async function runtimeAvailable(receipt: Receipt | undefined): Promise<boolean> {
  if (!receipt?.node) return true; // Older receipts are still ownership-checkable.
  try { await fs.access(receipt.node, constants.X_OK); return true; } catch { return false; }
}
async function pruneRuntime(c: AdapterContext, receipt: Receipt | undefined, keep?: string): Promise<void> {
  if (!receipt) return;
  const root = join(c.config.paths.configDir, 'runtime', 'antigravity');
  const path = receipt.runtimePath ?? join(root, VERSION);
  if (path === keep || dirname(path) !== root || !/^\d+\.\d+\.\d+(?:-[a-f0-9]{16})?$/.test(path.slice(root.length + 1))) return;
  try {
    const names = await fs.readdir(path);
    if (names.length !== 3 || !names.every(name => ['plugin.json', 'hooks.json', 'hook.cjs'].includes(name))) return;
    for (const name of names) if (await fileHash(join(path, name)) !== receipt.hashes[name]) return;
    // Only unchanged files covered by the previous protected ownership receipt.
    for (const name of names) await fs.unlink(join(path, name));
    await fs.rmdir(path);
  } catch { /* preserve modified, absent or unsafe runtimes */ }
}
/** agy leaves this directory after uninstall. Never recurse or follow symlinks. */
async function pruneEmptyAppData(c: AdapterContext): Promise<void> {
  const directory = join(homePath(c), '.gemini', 'antigravity', 'plugin_data', PLUGIN_NAME);
  try {
    await safeParents(dirname(directory));
    const stat = await fs.lstat(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (process.getuid && stat.uid !== process.getuid())) return;
    await fs.rmdir(directory);
  } catch { /* absent, nonempty or unsafe directories belong to the user */ }
}
const quote = (value: string): string => `'${value.replace(/'/g, `'\\''`)}'`;
const hash = (bytes: Uint8Array): string => createHash('sha256').update(bytes).digest('hex');
const receiptPath = (c: AdapterContext): string => join(c.config.paths.configDir, 'antigravity-registration.json');
const homePath = (c: AdapterContext): string => dirname(dirname(dirname(c.config.paths.user)));
function command(c: AdapterContext, args: string[]): Promise<string> {
  const env: NodeJS.ProcessEnv = {...process.env, HOME: homePath(c)};
  delete env.FI_API_KEY; delete env.FI_SECRET_KEY;
  return new Promise((done, fail) => execFile('agy', args, {env, timeout: 15000, maxBuffer: 128 * 1024},
    (error, stdout) => error ? fail(new Error('AGY_COMMAND_FAILED')) : done(stdout)));
}
async function plugins(c: AdapterContext): Promise<Plugin[]> {
  const data: unknown = JSON.parse(await command(c, ['plugin', 'list', '--json']));
  if (!Array.isArray(data) || data.some(p => !p || typeof p.name !== 'string' || typeof p.path !== 'string')) throw new Error('INVALID_PLUGIN_LIST');
  return data as Plugin[];
}
export function hooks(node: string, configDirectory: string, stateHome?: string): Record<string, unknown> {
  const events: Record<string, unknown> = {};
  for (const event of EVENTS) {
    const fallback = event === 'Stop' ? '{"decision":""}' : '{}';
    const handler = {type: 'command', command:
      `${stateHome ? `XDG_STATE_HOME=\${XDG_STATE_HOME:-${quote(stateHome)}} ` : ''}FI_AGENT_CONFIG_DIR=\${FI_AGENT_CONFIG_DIR:-${quote(configDirectory)}} ${quote(node)} ./hook.cjs ${event} 2>/dev/null || printf '%s\\n' ${quote(fallback)}`, timeout: event === 'Stop' ? 26 : 10};
    events[event] = event === 'PostToolUse' ? [{matcher: '.*', hooks: [handler]}] : [handler];
  }
  return {[PLUGIN_NAME]: events};
}
async function fileHash(path: string): Promise<string> {
  await safeParents(dirname(path));
  const handle = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await handle.stat();
    if (!st.isFile() || st.nlink !== 1 || st.size > 8 * 1024 * 1024 ||
      (process.getuid && st.uid !== process.getuid())) throw new Error('UNSAFE_PLUGIN_FILE');
    return hash(await handle.readFile());
  } finally { await handle.close(); }
}
async function owned(c: AdapterContext, plugin: Plugin, receipt: Receipt | undefined): Promise<boolean> {
  const expected = join(dirname(c.config.paths.user), 'plugins', PLUGIN_NAME);
  if (!receipt || receipt.schemaVersion !== 1 || resolve(plugin.path) !== expected || receipt.installedPath !== expected) return false;
  for (const file of ['plugin.json', 'hooks.json', 'hook.cjs']) {
    if (!receipt.hashes[file] || receipt.hashes[file] !== await fileHash(join(expected, file))) return false;
  }
  return true;
}
async function legacyDuplicate(c: AdapterContext): Promise<boolean> {
  const legacy = /fi-hook-to-otlp|futureagi-tracing|antigravity-plugin/;
  const user = await readProtected(join(dirname(c.config.paths.user), 'hooks.json'), false);
  if (user && legacy.test(user)) return true;
  // Hook metadata is untrusted data, not a source of credentials/configuration.
  // Bounded plain reads allow external-volume ancestors without trusting them.
  const path = join(c.projectDir ?? process.cwd(), '.agents', 'hooks.json');
  let handle: Awaited<ReturnType<typeof fs.open>> | undefined;
  try {
    handle = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size > 65536) throw new Error('WORKSPACE_HOOKS_UNREADABLE');
    const bytes = Buffer.alloc(65537), {bytesRead} = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead > 65536) throw new Error('WORKSPACE_HOOKS_UNREADABLE');
    return legacy.test(bytes.subarray(0, bytesRead).toString('utf8'));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw new Error('WORKSPACE_HOOKS_UNREADABLE');
  } finally { await handle?.close(); }
}
export const antigravityAdapter: CoreAdapter = {
  id: 'antigravity', version: VERSION, requiresWorker: false,
  diagnostics: {
    PENDING_TURNS: 'Unfinalized turns remain in the encrypted checkpoint. A live session may still be running; exited CLI sessions are recovered as incomplete. Unknown child ownership is retained rather than exported as a false user trace.',
    USER_PLUGIN_REQUIRED_USE_PROJECT_CONFIG_OVERRIDE: 'Install with user scope (omit --scope project); use .futureagi/agents.json for workspace overrides.',
    REMOVE_LEGACY_FUTUREAGI_HOOKS: 'Remove only the old Future AGI tracing entries from user or workspace hooks.json, then run setup again.',
    UNOWNED_PLUGIN_CONFLICT: 'Inspect agy plugin list --json. Back up and remove the conflicting futureagi-tracing plugin yourself before setup; unrelated plugins are preserved.',
    ENABLE_PLUGIN_IN_AGY: 'Run agy plugin enable futureagi-tracing, then run fi-agent doctor antigravity from your workspace.',
    PLUGIN_NOT_INSTALLED: 'Run fi-agent setup antigravity --project <name> to install the native plugin.',
    UNEXPECTED_INSTALL_LOCATION: 'Inspect agy plugin list --json and use supported agy 1.x on Linux or macOS; setup did not verify the installed path.',
    INSTALLED_CONTENT_MISMATCH: 'The installed plugin differs from this package. Back it up and resolve the conflict before running setup again.',
    PERSISTENT_NODE_REQUIRED: 'Install Node.js persistently (not inside an npx cache), then run setup again.',
    PINNED_NODE_MISSING: 'The installed Node interpreter is missing. Install Node.js and run setup again to repair the hook command.',
    WORKSPACE_HOOKS_UNREADABLE: 'Check .agents/hooks.json in this workspace; setup cannot rule out duplicate tracing until it is a readable regular file.',
    RUNTIME_VERSION_CONFLICT: 'The saved runtime differs from this package version. Preserve it for inspection and reinstall from a clean user configuration directory.',
    RUNTIME_LIMIT: 'The packaged hook exceeds the supported size. Reinstall an official supported package version.',
    AGY_COMMAND_FAILED: 'Ensure agy is installed on PATH and agy plugin list --json works in this shell, then retry.',
    INVALID_PLUGIN_LIST: 'Use the supported agy CLI version and check that agy plugin list --json returns a plugin list.',
    UNSAFE_PLUGIN_FILE: 'Check installed plugin ownership and file types; symlinks, hardlinks and files owned by another user are not accepted.',
    HOST_VERSION_NOT_QUALIFIED: 'Use agy 1.x on Linux or macOS (CLI tested with 1.5.2; desktop App 2.28.2 with agy 1.4.2); other agy major versions require qualification.',
    UNSUPPORTED_PLATFORM: 'Use Antigravity on Linux or macOS. Windows setup is not yet qualified.',
  },
  async detect(c) {
    if (process.platform !== 'linux' && process.platform !== 'darwin')
      return {supported: false, reason: 'UNSUPPORTED_PLATFORM'};
    const version = (await command(c, ['--version'])).trim();
    return {supported: supportedHostVersion(version), version,
      reason: supportedHostVersion(version) ? undefined : 'HOST_VERSION_NOT_QUALIFIED'};
  },
  async install(c) {
    // Native agy installs user-global. Project configuration is resolved from workspacePaths;
    // never pretend the native installer has a project flag or grant workspace trust.
    if (c.scope === 'project') return {registered: false, detail: 'USER_PLUGIN_REQUIRED_USE_PROJECT_CONFIG_OVERRIDE'};
    if (await legacyDuplicate(c)) return {registered: false, duplicate: true, detail: 'REMOVE_LEGACY_FUTUREAGI_HOOKS'};
    const existing = (await plugins(c)).filter(p => p.name === PLUGIN_NAME);
    const receipt = await readJson<Receipt>(receiptPath(c));
    if (existing.length && (existing.length !== 1 || !await owned(c, existing[0], receipt)))
      return {registered: false, detail: 'UNOWNED_PLUGIN_CONFLICT'};
    const node = await pinnedNode();
    const source = resolve(__dirname, '../plugin');
    const manifest = await fs.readFile(join(source, 'plugin.json'), 'utf8');
    const hookText = JSON.stringify(hooks(node, c.config.paths.configDir, dirname(dirname(c.config.paths.stateDir))), null, 2) + '\n';
    const bundle = await fs.readFile(join(source, 'hook.cjs'));
    if (bundle.length > 8 * 1024 * 1024) throw new Error('RUNTIME_LIMIT');
    const revision = contentHash(manifest + hookText + hash(bundle)).slice(0, 16);
    const stage = join(c.config.paths.configDir, 'runtime', 'antigravity', `${VERSION}-${revision}`);
    await ensurePrivateDirectory(stage);
    await writeProtected(join(stage, 'plugin.json'), manifest);
    await writeProtected(join(stage, 'hooks.json'), hookText);
    const target = join(stage, 'hook.cjs');
    // Exclusive creation; update only the exact runtime installed by this package version.
    try {
      const handle = await fs.open(target, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
      try { await handle.writeFile(bundle); } finally { await handle.close(); }
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== 'EEXIST' || await fileHash(target) !== hash(bundle)) throw new Error('RUNTIME_VERSION_CONFLICT');
    }
    if (existing.length) {
      const expectedHooks = receipt?.hashes['hooks.json'] === contentHash(hookText);
      if (expectedHooks && receipt?.hashes['hook.cjs'] === hash(bundle) && existing[0].enabled) return this.inspect(c);
      // Updating an owned enabled plugin is safe; do not silently re-enable a host-disabled plugin.
      if (!existing[0].enabled) return {registered: false, detail: 'ENABLE_PLUGIN_IN_AGY'};
    }
    await command(c, ['plugin', 'install', stage]);
    const installed = (await plugins(c)).find(p => p.name === PLUGIN_NAME);
    const expected = join(dirname(c.config.paths.user), 'plugins', PLUGIN_NAME);
    if (!installed || resolve(installed.path) !== expected) return {registered: false, detail: 'UNEXPECTED_INSTALL_LOCATION'};
    const hashes = {'plugin.json': contentHash(manifest), 'hooks.json': contentHash(hookText), 'hook.cjs': hash(bundle)};
    for (const [file, digest] of Object.entries(hashes)) if (await fileHash(join(expected, file)) !== digest)
      return {registered: false, detail: 'INSTALLED_CONTENT_MISMATCH'};
    await writeProtected(receiptPath(c), JSON.stringify({schemaVersion: 1, installedPath: expected,
      hostVersion: (await command(c, ['--version'])).trim(), hashes, node, runtimePath: stage}));
    await pruneRuntime(c, receipt, stage);
    return this.inspect(c);
  },
  async inspect(c): Promise<AdapterInspection> {
    const list = (await plugins(c)).filter(p => p.name === PLUGIN_NAME);
    const receipt = await readJson<Receipt>(receiptPath(c));
    const legacy = await legacyDuplicate(c);
    const duplicate = list.length > 1 || legacy;
    const verified = list.length === 1 && await owned(c, list[0], receipt);
    const interpreter = await runtimeAvailable(receipt);
    const {pendingTurns: inspectPending} = await import('./runtime.js');
    const pendingTurns = await inspectPending(c.config);
    const detail = legacy ? 'REMOVE_LEGACY_FUTUREAGI_HOOKS'
      : !list.length ? 'PLUGIN_NOT_INSTALLED'
      : !verified ? 'UNOWNED_PLUGIN_CONFLICT'
      : !interpreter ? 'PINNED_NODE_MISSING'
      : !list[0].enabled ? 'ENABLE_PLUGIN_IN_AGY' : pendingTurns ? 'PENDING_TURNS' : 'CLI_APP_TRANSCRIPT_GENERATIONS';
    return {registered: verified && list[0].enabled && !duplicate && interpreter, duplicate, pendingTurns,
      hostVersion: (await command(c, ['--version'])).trim(), trust: 'unknown', detail};
  },
  async uninstall(c) {
    const list = (await plugins(c)).filter(p => p.name === PLUGIN_NAME);
    const receipt = await readJson<Receipt>(receiptPath(c));
    if (list.length && (list.length !== 1 || !await owned(c, list[0], receipt)))
      return {registered: true, detail: 'UNOWNED_PLUGIN_PRESERVED'};
    if (list.length) await command(c, ['plugin', 'uninstall', PLUGIN_NAME]);
    const registered = (await plugins(c)).some(p => p.name === PLUGIN_NAME);
    if (!registered) {
      if (receipt) await pruneEmptyAppData(c);
      await pruneRuntime(c, receipt);
      await fs.unlink(receiptPath(c)).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
    return {registered};
  }
};
