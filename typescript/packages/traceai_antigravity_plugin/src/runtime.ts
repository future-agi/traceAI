import { promises as fs } from 'node:fs';
import { dirname, join, isAbsolute } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes } from 'node:crypto';
import type { ResolveOptions, ResolvedConfig, DeliveryState } from '@traceai/agent-core';
import { resolveConfig, destinationId, deliveryStatePath } from '@traceai/agent-core/config';
import { collectionEnabled } from '@traceai/agent-core/fence';
import { ensurePrivateDirectory, readProtected, writeProtectedUnderLock, updateJson, contentHash } from '@traceai/agent-core/files';
import { applyEvent, emitTurn, EVENTS, integer, isCli, type Conversation, type EventName, type Payload, type Turn } from './mapper.js';
import { enrichTranscript, settleTranscript } from './transcript.js';
// Pinned, maintained lock implementation: bounded retry plus stale-lock recovery.
const lockfile = require('proper-lockfile') as {lock(path: string, options: {
  realpath: boolean; lockfilePath: string; stale: number; update: number;
  retries: {retries: number; minTimeout: number; maxTimeout: number; factor: number};
  onCompromised: () => void;
}): Promise<() => Promise<void>>};

const CONTENT_BUDGET = 42000, MAX_STATE_BYTES = 720000, MAX_ENVELOPE_BYTES = 1024 * 1024, MAX_CONVERSATIONS = 128, TTL = 24 * 60 * 60 * 1000;
function key(config: ResolvedConfig): Buffer {
  return createHash('sha256').update('futureagi-antigravity-state-v1\0').update(config.identityKey ??
    JSON.stringify([config.credentials?.apiKey, config.credentials?.secretKey])).digest();
}
function binding(config: ResolvedConfig): string {
  return JSON.stringify([destinationId(config.endpoint, config.projectName), config.generation,
    config.captureContent, config.hideInputs, config.hideOutputs, config.hideErrorDetails]);
}
export function seal(state: Conversation, config: ResolvedConfig): string {
  const plain = JSON.stringify(state);
  if (Buffer.byteLength(plain) > MAX_STATE_BYTES) throw new Error('STATE_LIMIT');
  const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key(config), iv);
  cipher.setAAD(Buffer.from(binding(config)));
  const data = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final()]);
  return JSON.stringify({version: 1, iv: iv.toString('hex'), tag: cipher.getAuthTag().toString('hex'), data: data.toString('base64')});
}
export function unseal(raw: string, config: ResolvedConfig): Conversation {
  const v = JSON.parse(raw);
  if (v.version !== 1 || !/^[0-9a-f]{24}$/.test(v.iv) || !/^[0-9a-f]{32}$/.test(v.tag) || typeof v.data !== 'string') throw new Error('INVALID_STATE');
  const decipher = createDecipheriv('aes-256-gcm', key(config), Buffer.from(v.iv, 'hex'));
  decipher.setAAD(Buffer.from(binding(config))); decipher.setAuthTag(Buffer.from(v.tag, 'hex'));
  const state = JSON.parse(Buffer.concat([decipher.update(Buffer.from(v.data, 'base64')), decipher.final()]).toString('utf8')) as Conversation;
  if (state.schemaVersion !== 1 || !Array.isArray(state.completed) || state.completed.length > 128 || !Number.isFinite(state.touched)) throw new Error('INVALID_STATE');
  return state;
}
async function diagnostic(config: ResolvedConfig, code: string): Promise<void> {
  await updateJson<DeliveryState>(deliveryStatePath(config), old => ({
    pending: 0, exported: old?.exported ?? 0, dropped: (old?.dropped ?? 0) + 1,
    activatedAt: old?.activatedAt ?? new Date().toISOString(), lastAcceptedAt: old?.lastAcceptedAt, lastError: code
  })).catch(() => {});
}
async function boundedDirectory(directory: string, target: string): Promise<void> {
  const entries = (await fs.readdir(directory)).filter(x => /^[0-9a-f]{64}\.json$/.test(x));
  let count = 0;
  for (const name of entries) {
    const path = join(directory, name), st = await fs.lstat(path);
    if (st.isSymbolicLink() || !st.isFile()) throw new Error('UNSAFE_STATE');
    if (Date.now() - st.mtimeMs > TTL) await fs.unlink(path);
    else count++;
  }
  if (count >= MAX_CONVERSATIONS && !entries.includes(target)) throw new Error('STATE_LIMIT');
}
/** Select only an explicit single host workspace, never the plugin hook's cwd.
 * Ambiguous multi-root sessions use the user configuration instead of choosing a repository.
 */
function project(payload: Payload): string | undefined {
  const values = payload.workspacePaths;
  return Array.isArray(values) && values.length === 1 && typeof values[0] === 'string' && isAbsolute(values[0]) ? values[0] : undefined;
}
export interface HostIdentity {pid: number; start: string}
interface RuntimeOptions extends Partial<ResolveOptions> {host?: HostIdentity}
function conversationPath(config: ResolvedConfig, conversationId: string): string {
  const id = createHmac('sha256', key(config)).update(JSON.stringify([
    destinationId(config.endpoint, config.projectName), conversationId
  ])).digest('hex');
  return join(config.paths.stateDir, 'antigravity-conversations', `${id}.json`);
}
/** Join only explicitly correlated children. Atomic checkpoint reads are snapshots;
 * don't claim/delete a child before the parent tombstone is durable. Children never
 * export independently, so a crash here can be retried without replaying their usage.
 */
async function attachChildren(turn: Turn, config: ResolvedConfig, seen: Set<string>, joined: Set<string>, deadline: number, exiting: boolean): Promise<void> {
  for (const relation of turn.children ?? []) {
    if (seen.has(relation.conversationId) || seen.size >= 16) { turn.capped = true; continue; }
    seen.add(relation.conversationId);
    try {
      let child: Conversation | undefined;
      do {
        const raw = await readProtected(conversationPath(config, relation.conversationId), true, MAX_ENVELOPE_BYTES);
        child = raw ? unseal(raw, config) : undefined;
        if (child && (!child.active || exiting)) break;
        if (Date.now() >= deadline) break;
        await delay(100);
      } while (true);
      if (!child || child.origin !== 'unresolved') throw new Error('CHILD_STATE_UNAVAILABLE');
      const turns = [...(child.pending ?? [])];
      if (child.active) turns.push({...child.active, end: Date.now(), outcome: 'incomplete'});
      if (!turns.length) throw new Error('CHILD_STATE_UNAVAILABLE');
      const home = dirname(dirname(dirname(config.paths.user)));
      const payload = {conversationId: relation.conversationId, transcriptPath: join(home, '.gemini', 'antigravity-cli', 'brain', relation.conversationId, '.system_generated', 'logs', 'transcript_full.jsonl')};
      try { await enrichTranscript(child, turns, payload, config); }
      catch { await diagnostic(config, 'SUBAGENT_TRANSCRIPT_UNAVAILABLE'); }
      let attached = true;
      for (const nested of turns) {
        await attachChildren(nested, config, seen, joined, deadline, exiting);
        const prefix = `subagent:${relation.conversationId}:${nested.id}`;
        if (turn.activities.some(a => a.id === prefix)) continue;
        if (turn.activities.length + nested.activities.length + 1 > 480) { turn.capped = true; attached = false; break; }
        turn.activities.push({id: prefix, kind: 'agent', name: 'subagent', parentId: relation.toolId,
          start: nested.start, end: nested.end, input: relation.input, output: nested.output, outcome: nested.outcome});
        for (const activity of nested.activities) turn.activities.push({...activity,
          id: `${prefix}:${activity.id}`, parentId: activity.parentId ? `${prefix}:${activity.parentId}` : prefix});
        turn.end = Math.max(turn.end ?? turn.start, nested.end ?? nested.start);
      }
      if (attached) joined.add(relation.conversationId);
    } catch {
      if (turn.outcome !== 'error' && turn.outcome !== 'interrupted') turn.outcome = 'incomplete';
      await diagnostic(config, 'SUBAGENT_PENDING_OR_UNAVAILABLE');
    }
  }
}
/** Read-only diagnostics, including orphan children that cannot yet be attributed. */
export async function pendingTurns(config: ResolvedConfig): Promise<number> {
  const directory = join(config.paths.stateDir, 'antigravity-conversations');
  const states: {path: string; state: Conversation}[] = [];
  const names = (await fs.readdir(directory).catch(() => [])).filter(n => /^[0-9a-f]{64}\.json$/.test(n)).slice(0, MAX_CONVERSATIONS);
  for (const name of names) {
    try {
      const path = join(directory, name), raw = await readProtected(path, true, MAX_ENVELOPE_BYTES);
      if (raw) states.push({path, state: unseal(raw, config)});
    } catch { /* another policy/destination is not readable in this context */ }
  }
  const linked = new Set(states.flatMap(s => (s.state.joinedChildren ?? []).map(id => conversationPath(config, id))));
  return states.reduce((total, {path, state}) => total + (state.active ? 1 : 0) + (!linked.has(path) ? state.pending?.length ?? 0 : 0), 0);
}
/** Only a lease timeout is retryable: callers must not replay ambiguous exports. */
export async function handleEvent(event: string, payload: Payload, options: RuntimeOptions = {}): Promise<'retry-lock' | void> {
  if ((!EVENTS.includes(event as EventName) && event !== 'HostExit') || !isCli(payload) || typeof payload.conversationId !== 'string' ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(payload.conversationId)) return;
  let config: ResolvedConfig;
  try {
    config = await resolveConfig({...options, agent: 'antigravity', projectDir: options.projectDir ?? project(payload)});
  } catch { return; }
  if (!collectionEnabled(config)) return;
  const invocationShape = integer(payload.initialNumSteps) !== undefined &&
    (payload.invocationNum === undefined || integer(payload.invocationNum) !== undefined);
  if ((['PreInvocation', 'PostInvocation'].includes(event) && !invocationShape) ||
    (event === 'PostToolUse' && (integer(payload.stepIdx) === undefined || !payload.toolCall || typeof payload.toolCall !== 'object')) ||
    (event === 'Stop' && typeof payload.fullyIdle !== 'boolean')) {
    await diagnostic(config, 'UNKNOWN_HOOK_SHAPE');
    return;
  }
  const directory = join(config.paths.stateDir, 'antigravity-conversations');
  const path = conversationPath(config, payload.conversationId), name = path.slice(directory.length + 1);
  const lockPath = path.replace(/\.json$/, '.event-lock-v2');
  let release: (() => Promise<void>) | undefined;
  let compromised = false;
  try {
    await ensurePrivateDirectory(directory);
    release = await lockfile.lock(path, {realpath: false, lockfilePath: lockPath, stale: 30000, update: 1000,
      // Wait for the lock for nearly the whole hook budget (hook.ts: 8 s, Stop 25 s) so
      // bursts of overlapping hooks queue instead of dropping events.
      retries: {retries: event === 'Stop' ? 460 : 145, minTimeout: 50, maxTimeout: 50, factor: 1},
      onCompromised: () => { compromised = true; }});
    await boundedDirectory(directory, name);
    const raw = await readProtected(path, true, MAX_ENVELOPE_BYTES);
    let state: Conversation = {schemaVersion: 1, touched: Date.now(), generation: config.generation, completed: []};
    if (raw) {
      try {
        state = unseal(raw, config);
        if (Date.now() - state.touched > TTL || state.generation !== config.generation) throw new Error('STALE');
      } catch {
        await diagnostic(config, 'STATE_EXPIRED_OR_POLICY_CHANGED');
        state = {schemaVersion: 1, touched: Date.now(), generation: config.generation, completed: []};
      }
    }
    if (event === 'HostExit' && (!options.host || !state.active ||
        state.host?.pid !== options.host.pid || state.host?.start !== options.host.start)) return;
    if (event === 'PreInvocation') {
      // Read native USER_INPUT identity before deciding whether invocation zero
      // starts a prompt or continues the same prompt after background work.
      try {
        if (await enrichTranscript(state, state.active ? [state.active] : [], payload, config, true))
          await diagnostic(config, 'TRANSCRIPT_BACKLOG_SKIPPED');
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') await diagnostic(config, 'TRANSCRIPT_UNAVAILABLE_OR_GAP');
      }
      if (options.host) state.host = options.host;
    }
    const finished = event === 'HostExit' ? applyEvent(state, 'Stop', {...payload, fullyIdle: true, terminationReason: 'HOST_EXIT_WITHOUT_STOP'}, config)
      : applyEvent(state, event as EventName, payload, config);
    if (event === 'PostInvocation' || event === 'Stop' || event === 'HostExit') {
      try {
        const turns = [...finished, ...(state.active ? [state.active] : [])];
        try {
          if (await enrichTranscript(state, turns, payload, config))
            await diagnostic(config, 'TRANSCRIPT_BACKLOG_SKIPPED');
        } catch (error) {
          if (!finished.length || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
        }
        if (finished.length && !await settleTranscript(state, finished, payload, config))
          await diagnostic(config, 'TRANSCRIPT_INCOMPLETE');
      } catch { await diagnostic(config, 'TRANSCRIPT_UNAVAILABLE_OR_GAP'); }
    }
    const observed = [...finished, ...(state.active ? [state.active] : [])];
    state.linkedChildren = [...new Set([...(state.linkedChildren ?? []), ...observed.flatMap(t => t.children?.map(c => c.conversationId) ?? [])])].slice(-128);
    if (state.origin === 'unresolved' && finished.length) {
      state.pending = [...(state.pending ?? []), ...finished.splice(0)].slice(-8);
    }
    const deadline = Date.now() + 6000;
    const joined = new Set(state.joinedChildren ?? []);
    for (const turn of finished) await attachChildren(turn, config, new Set([payload.conversationId]), joined, deadline, event === 'HostExit');
    state.joinedChildren = [...joined].slice(-128);
    if (state.active?.capped || finished.some(turn => turn.capped)) await diagnostic(config, 'STATE_ACTIVITY_LIMIT');
    // Bound the whole journal, not just individual fields. Preserve structure if content fills it.
    if (Buffer.byteLength(JSON.stringify(state)) > CONTENT_BUDGET) {
      for (const turn of [...(state.pending ?? []), ...(state.active ? [state.active] : [])]) {
        delete turn.input; delete turn.output; delete turn.context;
        for (const a of turn.activities) { delete a.input; delete a.output; }
        for (const child of turn.children ?? []) delete child.input;
      }
      await diagnostic(config, 'STATE_CONTENT_LIMIT');
    }
    if (!collectionEnabled(config)) return;
    if (compromised) throw new Error('HOOK_LOCK_COMPROMISED');
    // Tombstone before sending: never replay ambiguous or accepted finalized spans on a repeated Stop.
    await writeProtectedUnderLock(path, seal(state, config), raw === undefined ? null : contentHash(raw), { maxBytes: MAX_ENVELOPE_BYTES });
    await release(); release = undefined; // Network delivery must not hold up the next hook.
    if (finished.length) {
      const { createTurnTracer } = await import('@traceai/agent-core');
      const tracer = createTurnTracer({config});
      try {
        for (const turn of finished) emitTurn(tracer, payload.conversationId, turn);
        await tracer.flush();
      } finally { await tracer.shutdown(); }
    }
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code === 'ELOCKED' ? 'HOOK_LOCK_TIMEOUT' :
      (e instanceof Error && ['STATE_LIMIT', 'UNSAFE_STATE', 'HOOK_LOCK_COMPROMISED'].includes(e.message) ? e.message : 'HOOK_STATE_UNAVAILABLE');
    await diagnostic(config, code);
    if (code === 'HOOK_LOCK_TIMEOUT') return 'retry-lock';
  } finally {
    if (release) await release().catch(() => {});
  }
}
