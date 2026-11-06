import { constants, promises as fs } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { safeParents } from '@traceai/agent-core/files';
import type { ResolvedConfig, UsageInput } from '@traceai/agent-core';
import { setTimeout as delay } from 'node:timers/promises';
import { integer, text, type Conversation, type Payload, type Turn } from './mapper.js';

const MAX_READ = 1024 * 1024;
function transcriptPath(payload: Payload, config: ResolvedConfig): string | undefined {
  if (typeof payload.transcriptPath !== 'string' || typeof payload.conversationId !== 'string') return undefined;
  const home = dirname(dirname(dirname(config.paths.user)));
  const expected = join(home, '.gemini', 'antigravity-cli', 'brain', payload.conversationId,
    '.system_generated', 'logs', 'transcript_full.jsonl');
  return resolve(payload.transcriptPath) === expected ? expected : undefined;
}
/** agy has no turn_ended row. Require a DONE response for each successful observed
 * invocation rather than assuming the transcript exists when Stop arrives. */
export async function settleTranscript(state: Conversation, turns: Turn[], payload: Payload, config: ResolvedConfig): Promise<boolean> {
  if (!transcriptPath(payload, config)) return true;
  const missing = (): boolean => turns.some(turn => turn.activities.some(a =>
    a.kind === 'agent' && a.outcome === 'success' && !turn.activities.some(g => g.kind === 'llm' && g.parentId === a.id)));
  const deadline = Date.now() + 6000;
  while (missing() && Date.now() < deadline) {
    await delay(100);
    try { await enrichTranscript(state, turns, payload, config); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  return !missing();
}
/** Recorded agy 1.5.2 transcript_full.jsonl schema. Per-PLANNER_RESPONSE tokens are
 * per-response, NOT the cumulative result. Never also add stream result totals.
 * Only the exact host-reported conversation transcript can be opened.
 */
export async function enrichTranscript(state: Conversation, turns: Turn[], payload: Payload, config: ResolvedConfig, beforeInvocation = false): Promise<boolean> {
  const expected = transcriptPath(payload, config);
  if (!expected) return false;
  await safeParents(dirname(expected));
  const handle = await fs.open(expected, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await handle.stat();
    if (!st.isFile() || st.nlink !== 1 || (process.getuid && st.uid !== process.getuid())) throw new Error('UNSAFE_TRANSCRIPT');
    const previous = state.transcript;
    if (previous && (previous.inode !== st.ino || st.size < previous.offset)) throw new Error('TRANSCRIPT_TRUNCATED');
    const unreadOffset = previous?.offset ?? 0;
    const backlog = st.size - unreadOffset > MAX_READ;
    // Resynchronize on complete records in a bounded tail, rather than pinning the
    // cursor forever behind historical or oversized tool output. At most 1 MiB is
    // read per hook. A split/oversized line is discarded, never parsed as a record.
    const offset = backlog ? st.size - MAX_READ : unreadOffset;
    const skippingLine = backlog || previous?.skippingLine === true;
    const buffer = Buffer.alloc(Math.min(MAX_READ, st.size - offset));
    const {bytesRead} = await handle.read(buffer, 0, buffer.length, offset);
    const chunk = buffer.subarray(0, bytesRead), end = chunk.lastIndexOf(10);
    if (end < 0) {
      if (skippingLine || bytesRead === MAX_READ) {
        state.transcript = {offset: offset + bytesRead, inode: st.ino, skippingLine: true};
        return true;
      }
      return false; // A normal partial final line remains available to the next hook.
    }
    const start = skippingLine ? chunk.indexOf(10) + 1 : 0;
    let consumed = start;
    // Persist resynchronization even if a later malformed record fails enrichment.
    if (skippingLine) state.transcript = {offset: offset + start, inode: st.ino};
    for (const line of chunk.subarray(start, end + 1).toString('utf8').split('\n').slice(0, -1)) {
      consumed += Buffer.byteLength(line) + 1;
      let row: Payload;
      try { row = JSON.parse(line); } catch { throw new Error('TRANSCRIPT_MALFORMED'); }
      const step = integer(row.step_index);
      if (step === undefined) continue;
      // A PreInvocation scan only observes prior source identity/context. Leave
      // the upcoming response for the invocation that is about to be attached.
      if (beforeInvocation && step >= (integer(payload.initialNumSteps) ?? 0)) {
        consumed -= Buffer.byteLength(line) + 1;
        break;
      }
      if (row.status !== 'DONE' && row.status !== 'INVALID' && row.status !== 'ERROR') {
        consumed -= Buffer.byteLength(line) + 1;
        break;
      }
      if (row.source === 'USER_EXPLICIT' && row.type === 'USER_INPUT' && row.status === 'DONE') {
        state.latestUserStep = Math.max(state.latestUserStep ?? -1, step);
        // A source row may arrive after its PreInvocation hook. Backfill identity
        // without starting or finalizing a turn from transcript text alone.
        const owner = turns.find(t => step > (t.userStep ?? -1) && step < Number(t.id.slice(5)));
        if (owner) owner.userStep = step;
        // Leave a new prompt's responses unread until its PreInvocation attaches
        // the new turn. Old turns cannot claim generations after this boundary.
        if (state.active && step >= Number(state.active.id.slice(5))) break;
      }
      const turn = turns.find(t => step >= Number(t.id.slice(5)) && !state.completed.includes(t.id)) ??
        turns.find(t => step >= Number(t.id.slice(5)));
      if (!turn) continue;
      // Only response records tied to an observed invocation become generations.
      const invocation = turn.activities.filter(a => a.kind === 'agent' && /^inv-\d+-\d+$/.test(a.id) &&
        Number(a.id.split('-')[1]) <= step).sort((a, b) => Number(b.id.split('-')[1]) - Number(a.id.split('-')[1]))[0];
      if (row.source === 'MODEL' && row.type === 'PLANNER_RESPONSE' && row.status === 'DONE' && invocation) {
        const id = `generation-${step}`;
        if (!turn.activities.some(a => a.id === id) && turn.activities.length >= 48) turn.capped = true;
        if (!turn.activities.some(a => a.id === id) && turn.activities.length < 48) {
          const usage: UsageInput = {cacheInInput: true, reasoningInOutput: true};
          for (const [field, target] of [['input_tokens','input'],['output_tokens','output'],['cache_read_tokens','cacheRead'],['thinking_tokens','reasoning']] as const) {
            const value = integer(row[field]); if (value !== undefined) usage[target] = value;
          }
          const stamp = typeof row.created_at === 'string' ? Date.parse(row.created_at) : NaN;
          turn.activities.push({id, kind:'llm', name:'generation', parentId:invocation.id,
            start:Number.isFinite(stamp)?stamp:invocation.start, end:Number.isFinite(stamp)?stamp:invocation.start,
            model:invocation.model, outcome:'success', usage,
            // This is reconstructed context, not a claim to capture the exact wire/system prompt.
            input: text(turn.context ?? turn.input, config),
            output:text(row.tool_calls ? {content:row.content, tool_calls:row.tool_calls} : row.content, config, true)});
        }
      }
      const tool = turn.activities.find(a => a.id === `tool-${step}`);
      if (tool && row.type !== 'PLANNER_RESPONSE') tool.output = text(row.error || row.content, config, true, tool.outcome === 'error') ?? tool.output;
      if (tool?.name === 'invoke_subagent' && row.type === 'GENERIC' && row.source === 'MODEL' && row.status === 'DONE' && typeof row.content === 'string') {
        // Qualified agy result format. Parse only the native result at this observed
        // tool step, not IDs mentioned in arbitrary model/tool/user content.
        const result = /(?:^|\n)Created the following subagent:\s*(\{[\s\S]*?\})\s*(?:\n|$)/.exec(row.content);
        if (result) {
          try {
            const child = JSON.parse(result[1]) as Payload;
            if (typeof child.conversationId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(child.conversationId) &&
                child.conversationId !== payload.conversationId && !turn.children?.some(c => c.conversationId === child.conversationId)) {
              if ((turn.children?.length ?? 0) < 8) (turn.children ??= []).push({conversationId: child.conversationId, toolId: tool.id, input: tool.input});
              else turn.capped = true;
            }
          } catch { /* malformed native result is not a lineage assertion */ }
        }
      }
      const content = (!config.hideOutputs && !(config.hideErrorDetails && row.error)) ? text(row.content, config) : undefined;
      if (content) turn.context = `${turn.context ?? turn.input ?? ''}\n${content}`.slice(-20000);
      // Transcript catches rejected tool calls that do not emit PostToolUse.
      if (row.source === 'MODEL' && (row.status === 'INVALID' || row.status === 'ERROR') && row.error &&
          !turn.activities.some(a=>a.id===`tool-${step}`) && turn.activities.length < 48) {
        turn.activities.push({id:`tool-${step}`,kind:'tool',name:'rejected_tool',parentId:turn.currentInvocation,
          start:Date.now(),end:Date.now(),outcome:'error',output:text(row.error,config,true,true)});
      }
      if (row.source === 'MODEL' && row.type === 'PLANNER_RESPONSE' && typeof row.content === 'string')
        turn.output = text(row.content, config, true);
    }
    state.transcript = {offset:offset + consumed, inode:st.ino};
    return skippingLine;
  } finally { await handle.close(); }
}
