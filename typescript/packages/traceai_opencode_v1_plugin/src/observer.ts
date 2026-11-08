import type { Hooks } from "@opencode-ai/plugin";
import { seedSecrets } from "@traceai/fi-core";
import {
  collectionEnabled, LIMITS,
  type ResolvedConfig, type TurnTracer, type TurnHandle, type TurnOutcome, type UsageInput,
} from "@traceai/agent-core";

// V1's actual step-start/step-finish events identify model steps. Assistant
// messages synthesized by /task have neither, and must not become LLM spans.
// These mappings originate in the recorded OpenCode 1.19.0 observer fixtures.
type Tool = {
  id: string; name: string; start?: number; end?: number; input?: string; output?: string;
  outcome?: TurnOutcome; childSession?: string; background?: boolean; notified?: boolean;
};
type Step = {
  id: string; start?: number; end?: number; model?: string; provider?: string;
  native: boolean; input?: string; text: Map<string, string>; tools: Map<string, Tool>;
  usage?: UsageInput; outcome?: TurnOutcome; compaction?: boolean;
};
type Turn = {
  id: string; session: string; start?: number; end?: number; input?: string;
  steps: Map<string, Step>; outcome?: TurnOutcome; idle: boolean;
  contextOverflow?: boolean; overflowStep?: string; errorStep?: string; awaitingErrorUpdate?: boolean; gap?: string;
  compactionStart?: number; compactionEnd?: number;
  observed: number; parent?: { turn: string; tool: string };
};
const id = (value: unknown): value is string => typeof value === "string" && /^[a-zA-Z0-9_-]{1,200}$/.test(value);
const time = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
const count = (value: unknown): number | undefined => Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : undefined;
const outcome = (error: any): TurnOutcome | undefined => !error ? undefined : error.name === "MessageAbortedError" ? "interrupted" : "error";
function usage(tokens: any, cost: unknown): UsageInput | undefined {
  if (!tokens || typeof tokens !== "object") return undefined;
  return {
    input: count(tokens.input), output: count(tokens.output), cacheRead: count(tokens.cache?.read),
    cacheWrite: count(tokens.cache?.write), reasoning: count(tokens.reasoning),
    // OpenCode normalizes these counters to EXCLUDE cached input / reasoning output.
    cacheInInput: false, reasoningInOutput: false,
    // V1 Session.getUsage calculates cost from host model pricing, not a billing response.
    ...(typeof cost === "number" && Number.isFinite(cost) && cost >= 0 ? { costUsd: cost, costSource: "host-estimate" as const } : {}),
  };
}

/** Bounded, memory-only observer. Hooks never await a network request or alter host output. */
export function observe(config: ResolvedConfig, tracer: TurnTracer): Hooks {
  const turns = new Map<string, Turn>();
  const current = new Map<string, string>();
  const parents = new Map<string, string>();
  const stepTurns = new Map<string, Turn>();
  const completed = new Set<string>();
  const contexts = new Map<string, string>();
  const settling = new Map<string, NodeJS.Timeout>();
  const SETTLE_MS = 1000;
  // Bind at observation time, not export time: task_id can reuse a session.
  const invocations = new Map<string, { turn: string; tool: string }>();
  const aliases = new Map<string, string>();
  const MAX_RETENTION_MS = 15 * 60 * 1000;
  const expiry = setInterval(() => {
    try {
      if (!collectionEnabled(config)) { clear(); return; }
      for (const t of [...turns.values()]) if (Date.now() - t.observed >= MAX_RETENTION_MS) interrupt(t, "incomplete");
      void tracer.flush();
    } catch { /* Retention cleanup must not affect the host. */ }
  }, 30000);
  expiry.unref();
  let retained = 0;
  let disposed = false;
  const MAX_CONTENT = 4 * 1024 * 1024;
  const credentials = config.credentials ? [config.credentials.apiKey, config.credentials.secretKey] : [];
  // Originals stay memory-only until core finalization: even a complete-value
  // PII rewrite here could split a credential registered between hook events.
  const bound = (text: string): string => text.length > LIMITS.ingress || Buffer.byteLength(text) > LIMITS.ingress
    ? "[REDACTED_SIZE_LIMIT]" : text;
  function capture(value: unknown, direction: "input" | "output"): string | undefined {
    if ((direction === "input" ? config.hideInputs : config.hideOutputs) || value === undefined) return;
    // Inspect complete data, not a 64-item projection that could drop privacy
    // context. Refuse an over-budget graph whole; never truncate a raw leaf/key.
    let budget = LIMITS.ingress, nodes = 20000;
    function materialize(v: unknown, depth = 0): unknown {
      if (--nodes < 0 || depth > 16) throw new Error("Content graph budget");
      if (typeof v === "string") {
        if (v.length > budget || (budget -= Buffer.byteLength(v)) < 0) throw new Error("Content byte budget");
        return v;
      }
      if (v === null || typeof v === "number" || typeof v === "boolean") return v;
      if (Array.isArray(v)) {
        if (v.length > nodes) throw new Error("Content array budget");
        return v.map(x => materialize(x, depth + 1));
      }
      if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [materialize(k, depth + 1), materialize(x, depth + 1)]));
      return undefined;
    }
    let text: string | undefined;
    try {
      const complete = typeof value === "string" ? value : JSON.stringify(materialize(value));
      text = complete === undefined ? undefined : bound(complete);
    } catch { text = "[REDACTED_SIZE_LIMIT]"; }
    try { seedSecrets(process.env, credentials); } catch { text = "[REDACTED_PRIVACY_FAILURE]"; }
    if (text === undefined || retained + Buffer.byteLength(text) > MAX_CONTENT) return;
    // Repeated updates count too; reset only after all turns settle.
    retained += Buffer.byteLength(text);
    return text;
  }
  function active(session: string): Turn | undefined { return turns.get(current.get(session) ?? ""); }
  function clear(): void {
    for (const timer of settling.values()) clearTimeout(timer);
    settling.clear();
    turns.clear(); current.clear(); parents.clear(); stepTurns.clear(); contexts.clear(); invocations.clear(); aliases.clear(); retained = 0;
  }
  function remember(key: string): void {
    completed.add(key);
    if (completed.size > 4096) completed.delete(completed.values().next().value!);
  }
  function forget(t: Turn): void {
    clearTimeout(settling.get(t.id)); settling.delete(t.id);
    turns.delete(t.id);
    for (const s of t.steps.values()) stepTurns.delete(s.id);
    for (const [key, turn] of aliases) if (turn === t.id) { aliases.delete(key); remember(key); }
    for (const [session, parent] of invocations) if (parent.turn === t.id) invocations.delete(session);
    if (current.get(t.session) === t.id) { current.delete(t.session); contexts.delete(t.session); }
    remember(t.id);
    if (!turns.size) { retained = 0; parents.clear(); }
  }
  function begin(message: any, parts: any[]): void {
    if (!id(message?.id) || !id(message?.sessionID) || completed.has(message.id) || turns.has(message.id) || aliases.has(message.id)) return;
    // V1 sends background results through chat.message too. Its synthetic task
    // envelope identifies the original invocation even if a later user turn is active.
    // Do not mistake legitimate file-only (or empty) submissions for notifications.
    if (parts.length && parts.every(p => p.type === "text" && p.synthetic === true)) {
      const session = parts.map(p => typeof p.text === "string" ? /^<task id="([a-zA-Z0-9_-]{1,200})" state="(?:completed|error)">/.exec(p.text)?.[1] : undefined).find(Boolean);
      let owner: Turn | undefined;
      if (session) {
        for (const t of turns.values()) if (t.session === message.sessionID) {
          for (const s of t.steps.values()) for (const tool of s.tools.values()) {
            if (tool.childSession === session && tool.background && !tool.notified) { tool.notified = true; owner = t; }
          }
        }
      } else owner = active(message.sessionID);
      if (owner && aliases.size < 512) {
        aliases.set(message.id, owner.id);
        owner.idle = false; owner.end = undefined;
        current.set(owner.session, owner.id);
      }
      return;
    }
    if (turns.size >= 64) return;
    const text = config.hideInputs ? undefined : bound(parts.slice(0, 64).filter(p => p.type === "text" && !p.synthetic).map(p => typeof p.text === "string" ? capture(p.text, "input") ?? "" : "").join("\n"));
    const parent = invocations.get(message.sessionID);
    const t: Turn = { id: message.id, session: message.sessionID, start: time(message.time?.created), input: text, steps: new Map(), idle: false, observed: Date.now(),
      parent: parent && turns.has(parent.turn) ? { ...parent } : undefined };
    turns.set(t.id, t);
    current.set(t.session, t.id);
  }
  function onMessage(info: any): void {
    if (!id(info?.id) || !id(info?.sessionID) || info.role !== "assistant") return;
    const t = turns.get(aliases.get(info.parentID) ?? info.parentID) ?? (!id(info.parentID) ? active(info.sessionID) : undefined);
    if (!t || t.session !== info.sessionID) return;
    if (!time(info.time?.created)) { t.gap = "UNKNOWN_ASSISTANT_SHAPE"; t.outcome = "incomplete"; }
    let step = t.steps.get(info.id);
    if (!step) {
      if (t.steps.size >= 128) { t.outcome = "incomplete"; return; }
      step = { id: info.id, start: time(info.time?.created), native: false, text: new Map(), tools: new Map(), input: contexts.get(t.session) ?? t.input };
      t.steps.set(step.id, step);
      stepTurns.set(step.id, t);
    }
    step.model = info.modelID; step.provider = info.providerID;
    step.end = time(info.time?.completed) ?? step.end;
    step.outcome = outcome(info.error) ?? step.outcome;
    step.compaction = info.summary === true || step.compaction;
    // Only completed messages own their usage; initial zero counters are placeholders.
    if (step.end) step.usage = usage(info.tokens, info.cost) ?? step.usage;
    if (step.end) t.awaitingErrorUpdate = false;
    if (info.error?.name === "ContextOverflowError") { t.contextOverflow = true; t.overflowStep = step.id; }
    else if (step.outcome === "interrupted" || step.outcome === "error") { t.outcome = step.outcome; t.errorStep = step.id; }
    if (t.idle && step.end) t.end = Math.max(t.end ?? 0, step.end);
  }
  function onPart(part: any): void {
    if (!id(part?.messageID)) return;
    const t = stepTurns.get(part.messageID);
    const step = t?.steps.get(part.messageID);
    if (!t || !step || part.sessionID !== t.session) return;
    if (part.type === "step-start" || part.type === "step-finish") {
      step.native = true;
      step.input ??= contexts.get(t.session);
      if (part.type === "step-finish") step.usage = usage(part.tokens, part.cost) ?? step.usage;
    } else if (part.type === "text" && id(part.id)) {
      if (step.text.size < 64 || step.text.has(part.id)) {
        const text = capture(part.text, "output");
        if (text !== undefined) step.text.set(part.id, text);
      }
    } else if (part.type === "tool" && id(part.callID) && !part.state?.time?.compacted) {
      const state = part.state ?? {};
      if (!["pending", "running", "completed", "error"].includes(state.status)) {
        t.gap = "UNKNOWN_TOOL_SHAPE"; t.outcome = "incomplete";
      }
      let tool = step.tools.get(part.callID);
      if (!tool) {
        if (step.tools.size >= 64) { t.outcome = "incomplete"; return; }
        tool = { id: part.callID, name: typeof part.tool === "string" ? part.tool : "tool" };
        step.tools.set(tool.id, tool);
      }
      tool.start = time(state.time?.start) ?? tool.start;
      if (state.status !== "pending") tool.input = capture(state.input, "input") ?? tool.input;
      if (tool.name === "task" && id(state.metadata?.sessionId) && !tool.childSession) {
        const childSession: string = state.metadata.sessionId;
        tool.childSession = childSession;
        const parent = { turn: t.id, tool: `${step.id}-${tool.id}` };
        invocations.set(childSession, parent);
        // Some hosts deliver metadata after the child prompt. Bind only the
        // unassigned turn; never reparent a previous invocation of this session.
        const child = [...turns.values()].reverse().find(c => c.session === childSession && !c.parent && parents.get(c.session) === t.session);
        if (child) child.parent = { ...parent };
      }
      if (state.metadata?.background === true) tool.background = true;
      if (state.status === "completed" || state.status === "error") {
        tool.end = time(state.time?.end) ?? Date.now();
        tool.outcome = state.metadata?.interrupted || state.error === "Tool execution aborted" || state.error === "Cancelled" ? "interrupted" : state.status === "error" ? "error" : "success";
        tool.output = state.status === "error" && config.hideErrorDetails ? undefined : capture(state.status === "error" ? state.error : state.output, "output");
      }
    }
  }
  function emit(t: Turn, root?: TurnHandle, parentId?: string, depth = 0): void {
    if (depth > 16 || !turns.has(t.id)) return;
    const own = !root;
    const handle = root ?? tracer.startTurn({ sessionId: t.session, turnId: t.id, input: t.input, startTime: t.start, usagePlan: "request" });
    const childId = `session-${t.id}`;
    const branch = own ? undefined : handle.child({ id: childId, kind: "agent", parentId, name: "subagent", input: t.input, startTime: t.start });
    const parent = own ? undefined : childId;
    let answer: string | undefined;
    if (t.compactionStart) {
      handle.child({ id: `compact-${t.id}`, kind: "compaction", parentId: parent, startTime: t.compactionStart })
        .end({ endTime: t.compactionEnd ?? t.end, outcome: t.compactionEnd ? "success" : "incomplete" });
    }
    for (const s of t.steps.values()) {
      // Subtask placeholder assistant messages can still contain genuine tools.
      const generation = s.native || s.outcome ? handle.child({ id: s.id, kind: s.native ? "llm" : "generation", parentId: parent,
        name: s.compaction ? "compaction-generation" : "generation", startTime: s.start, input: s.input,
        model: s.model, provider: s.provider, fidelity: s.native ? "native-request" : "reconstructed-message" }) : undefined;
      const text = bound([...s.text.values()].join("\n")) || undefined;
      if (text) answer = text;
      for (const tool of s.tools.values()) {
        const key = `${s.id}-${tool.id}`;
        const h = handle.child({ id: key, kind: "tool", toolCallId: tool.id, parentId: generation ? s.id : parent, name: tool.name, input: tool.input, startTime: tool.start });
        for (const child of [...turns.values()]) if (child.parent?.turn === t.id && child.parent.tool === key) emit(child, handle, key, depth + 1);
        h.end({ output: tool.output, outcome: tool.outcome ?? "incomplete", endTime: tool.end ?? t.end });
      }
      const output = text ?? (s.tools.size ? capture({ tool_calls: [...s.tools.values()].map(tool => ({ name: tool.name, arguments: tool.input })) }, "output") : undefined);
      generation?.end({ output, outcome: s.outcome ?? (s.end ? "success" : "incomplete"), endTime: s.end ?? t.end, usage: s.usage });
    }
    // Context overflow may be recovered by compaction. Only a later completed,
    // successful assistant generation proves recovery; idle alone proves nothing.
    const overflowOutcome = unresolvedOverflow(t) ? "error" as const : undefined;
    const last = [...t.steps.values()].at(-1);
    // Keep failed attempts as errors, but a later successful source generation
    // recovers the turn. An idle signal or a pre-error success is not recovery.
    const recovered = t.outcome === "error" && last?.native && last.end && last.id !== t.errorStep && !last.outcome && !t.awaitingErrorUpdate;
    const result = { output: answer, outcome: (recovered ? undefined : t.outcome) ?? overflowOutcome ?? (ready(t) ? "success" as const : "incomplete" as const), endTime: t.end, gap: t.gap };
    if (branch) branch.end(result); else handle.end(result);
    forget(t);
  }
  function unresolvedOverflow(t: Turn): boolean {
    const last = [...t.steps.values()].at(-1);
    return !!t.contextOverflow && !(last?.end && !last.outcome && last.id !== t.overflowStep);
  }
  function ready(t: Turn, depth = 0): boolean {
    if (depth > 16 || !t.idle || t.awaitingErrorUpdate) return false;
    if (!t.steps.size) return false;
    for (const s of t.steps.values()) {
      if (!s.end) return false;
      for (const tool of s.tools.values()) if (!tool.outcome || (tool.background && !tool.notified)) return false;
    }
    for (const child of turns.values()) if (child.parent?.turn === t.id && !ready(child, depth + 1)) return false;
    return true;
  }
  function interrupt(t: Turn, reason: "interrupted" | "incomplete"): void {
    if (!turns.has(t.id)) return;
    // Finalize the whole associated trace on expiry/deletion, never a child as
    // a new root. Shutdown/expiry explicitly records unfinished work.
    let root = t;
    for (let n = 0; n < 16 && root.parent; n++) {
      const parent = turns.get(root.parent.turn); if (!parent) break; root = parent;
    }
    root.outcome ??= unresolvedOverflow(root) ? "error" : reason;
    // Initial parent idle is not the end of a retained background trace.
    root.end = Date.now();
    emit(root);
  }
  function drainReady(): void {
    // Idle is only a pending signal. V1 publishes final assistant errors AFTER
    // idle; retain the turn until every observed message/tool has a terminal row.
    for (const root of [...turns.values()]) if (!root.parent && !parents.has(root.session) && ready(root)) emit(root);
    void tracer.flush();
  }
  function settle(session: string): void {
    const t = active(session);
    if (!t) return;
    t.idle = true; t.end = Date.now();
    if (!settling.has(t.id)) {
      const timer = setTimeout(() => {
        settling.delete(t.id);
        if (!collectionEnabled(config)) { clear(); return; }
        if (!turns.has(t.id) || !t.idle) return;
        // Pending background work owns a later continuation and the retention
        // deadline, not the short terminal-row deadline.
        if ([...t.steps.values()].some(s => [...s.tools.values()].some(tool => tool.background && !tool.notified))) return;
        if (!ready(t)) {
          t.gap ??= "MISSING_TERMINAL_UPDATE";
          if (unresolvedOverflow(t)) t.outcome ??= "error";
          interrupt(t, "incomplete");
        } else drainReady();
      }, SETTLE_MS);
      timer.unref(); settling.set(t.id, timer);
    }
  }
  const guard = <A extends unknown[]>(fn: (...args: A) => void) => async (...args: A): Promise<void> => {
    try {
      if (disposed) return;
      if (!collectionEnabled(config)) { clear(); return; }
      fn(...args);
      drainReady();
    } catch { /* A tracing callback must never fail a prompt or tool. */ }
  };
  return {
    "chat.message": guard((_input, output) => begin(output.message, output.parts)),
    "experimental.chat.messages.transform": guard((_input, output) => {
      // Context mixes prompt input with historical assistant/tool output (also
      // synthetic user-role task results). Omit it before traversing/capturing
      // anything when either privacy direction is hidden, including aliases.
      // Historical text can repeat a failure-result envelope in any role.
      if (config.hideInputs || config.hideOutputs || config.hideErrorDetails) return;
      const messages = output.messages;
      const session = messages.at(-1)?.info.sessionID;
      if (!session || !active(session)) return;
      const context = capture(messages.slice(-32).map(m => ({ role: m.info.role, parts: m.parts.slice(0, 32).filter(p => p.type === "text" || p.type === "tool").map(p => p.type === "text" ? { type: p.type, text: p.text } : p.type === "tool" ? { type: p.type, tool: p.tool, input: p.state.input, output: p.state.status === "completed" ? p.state.output : undefined } : {}) })), "input");
      if (context !== undefined) {
        contexts.set(session, context);
        // V1 creates the assistant message before applying this transform.
        const step = [...active(session)!.steps.values()].at(-1);
        if (step && !step.native) step.input = context;
      }
    }),
    "experimental.session.compacting": guard(input => { const t = active(input.sessionID); if (t) t.compactionStart = Date.now(); }),
    event: guard(({ event }: any) => {
      const p = event?.properties ?? {};
      if (event.type === "session.created" || event.type === "session.updated") {
        if (id(p.info?.id) && id(p.info?.parentID) && parents.size < 128) parents.set(p.info.id, p.info.parentID);
      } else if (event.type === "message.updated") onMessage(p.info);
      else if (event.type === "message.part.updated") onPart(p.part);
      else if (event.type === "session.compacted") { const t = active(p.sessionID); if (t) t.compactionEnd = Date.now(); }
      else if (event.type === "session.error") {
        const t = active(p.sessionID);
        if (t) {
          t.awaitingErrorUpdate = true;
          if (p.error?.name === "ContextOverflowError") {
            t.contextOverflow = true; t.overflowStep = [...t.steps.values()].at(-1)?.id;
          } else { t.outcome = outcome(p.error); t.errorStep = [...t.steps.values()].at(-1)?.id; }
        }
      } else if (event.type === "session.idle") settle(p.sessionID);
      else if (event.type === "session.deleted") {
        for (const t of [...turns.values()]) if (t.session === p.info?.id) interrupt(t, "interrupted");
        void tracer.flush();
      }
    }),
    async dispose() {
      if (disposed) return;
      // The host can dispose while its final event publication is still queued.
      // Keep accepting those events for a bounded window, without delaying hooks.
      const deadline = Date.now() + SETTLE_MS;
      while ([...turns.values()].some(t => t.idle && !ready(t)) && Date.now() < deadline && collectionEnabled(config)) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      disposed = true;
      clearInterval(expiry);
      if (collectionEnabled(config)) for (const t of [...turns.values()]) {
        if (ready(t)) emit(t); else interrupt(t, "incomplete");
      }
      clear();
      // Terminal flush is the only callback allowed to await the bounded exporter.
      await tracer.shutdown();
    },
  };
}
