import type { ResolvedConfig, TurnOutcome, TurnTracer, UsageInput } from '@traceai/agent-core';
import { redact, redactStructured } from '@traceai/agent-core/privacy';

export const EVENTS = ['SessionStart', 'PreInvocation', 'PostInvocation', 'PostToolUse', 'Stop'] as const;
export type EventName = typeof EVENTS[number];
export type Payload = Record<string, unknown>;
export interface Activity {
  id: string;
  kind: 'agent' | 'tool' | 'llm';
  usage?: UsageInput;
  name: string;
  parentId?: string;
  start: number;
  end?: number;
  input?: string;
  output?: string;
  model?: string;
  outcome?: TurnOutcome;
}
export interface Turn {
  /** Explicit native invoke_subagent result, never inferred from prompt text. */
  children?: {conversationId: string; toolId: string; input?: string}[];
  capped?: boolean;
  context?: string;
  id: string;
  /** Native USER_EXPLICIT/USER_INPUT step, not an invocation-number reset. */
  userStep?: number;
  start: number;
  input?: string;
  output?: string;
  end?: number;
  outcome?: TurnOutcome;
  currentInvocation?: string;
  activities: Activity[];
}
export interface Conversation {
  schemaVersion: 1;
  touched: number;
  generation: number;
  active?: Turn;
  /** Promptless native child hooks may precede their parent's correlation record. */
  pending?: Turn[];
  origin?: 'user' | 'unresolved';
  linkedChildren?: string[];
  joinedChildren?: string[];
  host?: {pid: number; start: string};
  completed: string[];
  latestUserStep?: number;
  completedUserStep?: number;
  transcript?: {offset: number; inode: number; skippingLine?: boolean};
}
export function integer(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}
export function isCli(payload: Payload): boolean {
  const paths = [payload.transcriptPath, payload.artifactDirectoryPath].filter((x): x is string => typeof x === 'string');
  return paths.some(x => x.replace(/\\/g, '/').includes('/.gemini/antigravity-cli/')) &&
    !paths.some(x => /\/\.gemini\/antigravity(?:-ide)?\//.test(x.replace(/\\/g, '/')));
}
export function text(value: unknown, config: ResolvedConfig, output = false, error = false): string | undefined {
  if (!config.captureContent || (output ? config.hideOutputs : config.hideInputs) || (error && config.hideErrorDetails)) return undefined;
  if (value === undefined || value === null) return undefined;
  const clean = redactStructured(value);
  let raw = typeof clean === 'string' ? clean : JSON.stringify(clean);
  if (raw === undefined) return undefined;
  // Replace complete known credentials before truncating so a cap cannot expose a key prefix.
  for (const secret of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (secret) raw = raw.split(secret).join('[REDACTED]');
  return redact(raw).slice(0, 20000);
}
const label = (v: unknown, fallback: string): string => typeof v === 'string' && /^[a-zA-Z0-9_.:/-]{1,128}$/.test(v) && !v.includes('://') ? v : fallback;
const invocationId = (p: Payload): string | undefined => {
  const step = integer(p.initialNumSteps), invocation = p.invocationNum === undefined ? 0 : integer(p.invocationNum);
  return step === undefined || invocation === undefined ? undefined : `inv-${step}-${invocation}`;
};
export function neutral(event: string): string {
  return event === 'Stop' ? '{"decision":""}\n' : '{}\n';
}
export function stopOutcome(p: Payload): TurnOutcome {
  if (p.error || String(p.terminationReason).toLowerCase() === 'error') return 'error';
  const reason = String(p.terminationReason).toLowerCase();
  if (['user_canceled', 'user_cancelled', 'cancelled', 'canceled', 'interrupted'].includes(reason)) return 'interrupted';
  return ['no_tool_call', 'model_stop', 'completed'].includes(reason) ? 'success' : 'incomplete';
}
/** Hooks enclose an invocation AND its tools, not a proven provider request.
 * This hook mapper never guesses generations/usage; qualified transcript records
 * are enriched separately. Cost and subagent lineage remain absent without evidence.
 */
export function applyEvent(state: Conversation, event: EventName, p: Payload, config: ResolvedConfig, now = Date.now()): Turn[] {
  const finished: Turn[] = [];
  state.touched = now;
  if (event === 'PreInvocation') {
    const invocation = p.invocationNum === undefined ? 0 : integer(p.invocationNum);
    const step = integer(p.initialNumSteps), id = invocationId(p);
    if (invocation === undefined || step === undefined || !id) return finished;
    if (invocation === 0) {
      const turnId = `turn-${step}`;
      if (state.completed.includes(turnId)) return finished;
      const userStep = state.latestUserStep;
      // Background/system continuations reset invocationNum to zero too. Only a
      // newly observed explicit USER_INPUT can split an unfinished user turn.
      const newPrompt = userStep !== undefined && state.active !== undefined &&
        userStep > (state.active.userStep ?? Number(state.active.id.slice(5)) - 1);
      if (!state.active && userStep !== undefined && userStep <= (state.completedUserStep ?? -1)) return finished;
      if (!state.active || newPrompt) {
        if (state.active) {
          state.active.end = now; state.active.outcome = 'incomplete';
          finished.push(state.active); state.completed.push(state.active.id);
        }
        // A native subagent starts at step zero with no USER_INPUT. Keep it pending
        // until a parent's explicit invoke_subagent record supplies ownership.
        state.origin = !p.lastUserInput && (step === 0 || state.origin === 'unresolved') ? 'unresolved' : 'user';
        state.active = {id: turnId, userStep, start: now, input: text(p.lastUserInput, config), activities: []};
      }
    }
    const turn = state.active;
    if (!turn) return finished;
    if (turn.activities.length >= 48) { turn.capped = true; return finished; }
    turn.currentInvocation = id;
    if (!turn.activities.some(a => a.id === id)) turn.activities.push({
      id, kind: 'agent', name: 'invocation', start: now,
      model: label(p.modelName, 'unknown'), input: text(p.lastUserInput, config)
    });
  } else if (event === 'PostInvocation') {
    const activity = state.active?.activities.find(a => a.id === invocationId(p));
    if (activity && activity.end === undefined) {
      activity.end = now; activity.outcome = p.error ? 'error' : 'success';
      activity.output = text(p.modelOutput, config, true, !!p.error);
    }
  } else if (event === 'PostToolUse') {
    const turn = state.active, step = integer(p.stepIdx);
    if (!turn || step === undefined || turn.activities.some(a => a.id === `tool-${step}`)) return finished;
    if (turn.activities.length >= 48) { turn.capped = true; return finished; }
    const call = p.toolCall && typeof p.toolCall === 'object' ? p.toolCall as Payload : {};
    // PostToolUse has no source start timestamp. A zero-duration observation does not invent execution duration.
    turn.activities.push({id: `tool-${step}`, kind: 'tool', name: label(call.name, 'tool'),
      parentId: turn.currentInvocation, start: now, end: now, outcome: p.error ? 'error' : 'success',
      input: text(call.args, config), output: text(p.error || p.result, config, true, !!p.error)});
  } else if (event === 'Stop' && p.fullyIdle === true && state.active) {
    const turn = state.active;
    turn.end = now; turn.outcome = stopOutcome(p);
    turn.output = text(p.error || p.finalModelOutput, config, true, !!p.error) ?? turn.output;
    finished.push(turn); state.completed.push(turn.id);
    if (turn.userStep !== undefined) state.completedUserStep = turn.userStep;
    delete state.active;
  }
  state.completed = state.completed.slice(-128);
  return finished;
}
export function emitTurn(tracer: TurnTracer, conversationId: string, turn: Turn): void {
  const root = tracer.startTurn({sessionId: conversationId, turnId: turn.id, input: turn.input,
    usagePlan: turn.activities.some(a => a.kind === 'llm') ? 'request' : 'unknown', startTime: new Date(turn.start)});
  for (const a of turn.activities) {
    const child = root.child({id: a.id, kind: a.kind === 'llm' ? 'generation' : a.kind, name: a.name, parentId: a.parentId,
      input: a.input, model: a.model, provider: a.kind === 'llm' && a.model?.startsWith('gemini') ? 'google' : undefined,
      startTime: new Date(a.start), timing: a.kind === 'agent' ? 'source' : 'reconstructed',
      fidelity: a.kind === 'llm' ? 'reconstructed-message' : 'aggregate-turn'});
    // Leave missing completions open: core closes them as incomplete and records
    // MISSING_CHILD_STOP on the root without replacing a known terminal failure.
    if (a.end !== undefined)
      child.end({output: a.output, usage: a.usage, outcome: a.outcome ?? 'incomplete', endTime: new Date(a.end)});
  }
  root.end({output: turn.output, outcome: turn.capped && (!turn.outcome || turn.outcome === 'success') ? 'incomplete' : turn.outcome,
    endTime: new Date(turn.end ?? turn.start)});
}
