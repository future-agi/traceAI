import { promises as fs } from 'node:fs';
import { basename, join } from 'node:path';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { resolveConfig } from '@traceai/agent-core/config';
import { collectionEnabled } from '@traceai/agent-core/fence';
import { ensurePrivateDirectory } from '@traceai/agent-core/files';
import { handleEvent, type HostIdentity } from './runtime.js';
import { isCli, type Payload } from './mapper.js';

const lockfile = require('proper-lockfile') as {lock(path: string, options: object): Promise<() => Promise<void>>};
interface ProcessIdentity extends HostIdentity {parent: number; live: boolean}
/** Linux-only process metadata, never argv/environment or agent home data. Start
 * ticks fence PID reuse. A zombie has exited even if its parent has not reaped it. */
async function identity(pid: number): Promise<ProcessIdentity | undefined> {
  if (!Number.isSafeInteger(pid) || pid <= 1 || process.platform !== 'linux') return;
  try {
    const raw = await fs.readFile(`/proc/${pid}/stat`, 'utf8');
    if (raw.length > 8192) return;
    const fields = raw.slice(raw.lastIndexOf(')') + 2).split(' ');
    if (!/^\d+$/.test(fields[19])) return;
    return {pid, start: fields[19], parent: Number(fields[1]), live: !['Z', 'X'].includes(fields[0])};
  } catch { return; }
}
export async function nativeHost(): Promise<HostIdentity | undefined> {
  let pid = process.ppid;
  for (let depth = 0; depth < 8; depth++) {
    const current = await identity(pid);
    if (!current?.live) return;
    const executable = await fs.readlink(`/proc/${pid}/exe`).catch(() => '');
    if (basename(executable) === 'agy') return {pid, start: current.start};
    pid = current.parent;
  }
}
export function startExitRecovery(entry: string, host: HostIdentity, payload: Payload): void {
  // One session-scoped finisher wins the lease; duplicates immediately exit.
  // Only structural paths/IDs cross stdin. No prompt, output or keys in argv/state.
  const child = spawn(process.execPath, [entry, '--recover-exit', String(host.pid), host.start], {
    detached: true, stdio: ['pipe', 'ignore', 'ignore'], env: process.env,
  });
  child.on('error', () => {});
  child.stdin.on('error', () => {});
  child.stdin.end(JSON.stringify({conversationId: payload.conversationId, transcriptPath: payload.transcriptPath,
    artifactDirectoryPath: payload.artifactDirectoryPath, workspacePaths: payload.workspacePaths}));
  child.unref();
}
/** agy 1.x emits no Stop for some headless exits. A per-session, finite finisher
 * observes actual process exit and finalizes the existing checkpoint as incomplete.
 * It is not a daemon, doesn't intercept signals/permissions, and never starts agy.
 */
export async function recoverOnExit(host: HostIdentity, payload: Payload): Promise<void> {
  if (!isCli(payload) || typeof payload.conversationId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(payload.conversationId) ||
      !Number.isSafeInteger(host.pid) || host.pid <= 1 || !/^\d{1,24}$/.test(host.start)) return;
  const projectDir = Array.isArray(payload.workspacePaths) && payload.workspacePaths.length === 1 && typeof payload.workspacePaths[0] === 'string'
    ? payload.workspacePaths[0] : undefined;
  const config = await resolveConfig({agent: 'antigravity', projectDir});
  if (!collectionEnabled(config)) return;
  const directory = join(config.paths.stateDir, 'antigravity-recovery');
  await ensurePrivateDirectory(directory);
  const id = createHash('sha256').update(JSON.stringify([host, payload.conversationId])).digest('hex');
  let release: (() => Promise<void>) | undefined, compromised = false;
  try {
    release = await lockfile.lock(join(directory, id), {realpath: false, stale: 30000, update: 1000, retries: 0,
      onCompromised: () => { compromised = true; }});
    const deadline = Date.now() + 24 * 60 * 60 * 1000;
    let exitDeadline: number | undefined;
    while (Date.now() < deadline && !compromised && collectionEnabled(config)) {
      const current = await identity(host.pid);
      if (!current?.live || current.start !== host.start) {
        // Allow already-launched terminal hooks to drain; the event lock and durable
        // tombstone arbitrate races with Stop and with another exit observer.
        await delay(250);
        // An abruptly killed writer's lease remains fresh for 30 seconds. Keep
        // this finite observer alive across retryable acquisition failures; never
        // backdate or steal a live heartbeat-maintained event lock.
        exitDeadline ??= Date.now() + 45000;
        if (await handleEvent('HostExit', payload, {host}) !== 'retry-lock' || Date.now() >= exitDeadline) return;
        continue;
      }
      await delay(250);
    }
  } finally { await release?.().catch(() => {}); }
}
