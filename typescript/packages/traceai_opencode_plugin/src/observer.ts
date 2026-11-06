import type { OpenCodeEvent } from "@opencode/client";
import {
  collectionEnabled, redact, isSensitiveField,
  type ResolvedConfig, type TurnTracer, type TurnHandle, type ChildHandle,
  type TurnOutcome, type UsageInput,
} from "@traceai/agent-core";

const LIMIT = 128;
const MAX_TEXT = 20_000;
interface Step {
  id: string;
  model?: { id: string; providerID: string };
  handle?: ChildHandle;
  output?: string;
  streamed?: number;
  attempt: number;
  input?: string;
  started?: number;
  toolCalls?: Array<{ name: string; arguments?: string }>;
  /** Prompt/notification roots included in this dispatch, without duplicating usage. */
  participants?: Conversation[];
}
interface Conversation {
  parent?: string;
  parentTool?: string;
  /** Exact parent work, not whichever prompt is currently active in its session. */
  parentWork?: Conversation;
  pending: Map<string, { text?: string; created: number }>;
  turn?: TurnHandle;
  branch?: ChildHandle;
  branchId?: string;
  turnId?: string;
  step?: Step;
  tools: Map<string, { handle: ChildHandle; id: string }>;
  toolNames: Map<string, string>;
  delegations: Map<string, { toolId: string; background: boolean }>;
  /** Child execution and delivery of its background result are separate lifecycles. */
  background: Map<string, TurnHandle>;
  notifications: Map<string, { childID: string; turn: TurnHandle }>;
  executing?: boolean;
  request?: string;
  requests: Array<{ input?: string }>;
  output?: string;
  compact?: { id: string; started: number; input?: string };
  outcome?: TurnOutcome;
}

/** Only public V2 events are accepted. Never fetch transcripts or consume model streams. */
export class Observer {
  private readonly sessions = new Map<string, Conversation>();
  /** Dormant turns can still own running children and undelivered notifications. */
  private readonly outstanding = new Map<string, Set<Conversation>>();
  private readonly seen = new Set<string>();
  private readonly contexts = new WeakSet<object>();
  private stopped = false;
  constructor(
    readonly tracer: TurnTracer,
    private readonly config: ResolvedConfig,
    private readonly location: { directory: string; workspaceID?: string },
  ) {}

  /** Snapshot only bounded, already privacy-filtered text; raw hook events are never retained. */
  text(value: unknown, output = false): string | undefined {
    if ((output ? this.config.hideOutputs : this.config.hideInputs) || !collectionEnabled(this.config)) return;
    let budget = MAX_TEXT + 256;
    let count = 0;
    const take = (v: unknown, depth: number): unknown => {
      if (budget <= 0 || ++count > 1024 || depth > 6) return "[truncated]";
      if (typeof v === "string") { const s = redact(v).slice(0, budget); budget -= s.length; return s; }
      if (typeof v === "number" || typeof v === "boolean" || v === null) return v;
      if (Array.isArray(v)) return v.slice(0, 128).map(x => take(x, depth + 1));
      if (v && typeof v === "object") {
        // Native tool-result errors can be scalars or objects. Suppress before
        // descending/retaining a context snapshot, without mutating host messages.
        const object = v as Record<string, unknown>;
        if (this.config.hideErrorDetails && object.type === "tool-result" &&
          object.result && typeof object.result === "object" &&
          (object.result as Record<string, unknown>).type === "error")
          return { type: "tool-result", result: { type: "error", value: "[ERROR_DETAILS_HIDDEN]" } };
        const out: Record<string, unknown> = {};
        for (const k of Object.keys(v).slice(0, 64)) {
          if (k.length > 128 || isSensitiveField(k) || /headers|authorization|api.?key|secret|password|(?:access|refresh|auth|id).?token|^token$|native|metadata|data|uri/i.test(k)) continue;
          budget -= k.length;
          out[k] = take((v as Record<string, unknown>)[k], depth + 1);
          if (budget <= 0) break;
        }
        return out;
      }
      return undefined;
    };
    const bounded = take(value, 0);
    let text = typeof bounded === "string" ? bounded : JSON.stringify(bounded);
    if (!text) return;
    for (const key of [this.config.credentials?.apiKey, this.config.credentials?.secretKey])
      if (key) text = text.split(key).join("[REDACTED]");
    return redact(text).slice(0, MAX_TEXT);
  }

  private works(id: string): Conversation[] {
    const active = this.sessions.get(id);
    return [...(active ? [active] : []), ...(this.outstanding.get(id) ?? [])];
  }

  private allWorks(): Array<[string, Conversation]> {
    return [...this.sessions.keys()].flatMap(id => this.works(id).map(s => [id, s] as [string, Conversation]));
  }

  private activate(id: string, work: Conversation): void {
    const previous = this.sessions.get(id);
    if (previous === work) return;
    if (previous?.turn) {
      if (!this.outstanding.has(id)) this.outstanding.set(id, new Set());
      this.outstanding.get(id)!.add(previous);
    }
    // Hooks may run before the corresponding durable inbox delivery. Unconsumed
    // request snapshots belong to the next dispatch, not to the dormant root.
    if (previous) work.requests.push(...previous.requests.splice(0).slice(0, 16 - work.requests.length));
    this.outstanding.get(id)?.delete(work);
    this.sessions.set(id, work);
  }

  private fresh(previous?: Conversation): Conversation {
    return { pending: previous?.pending ?? new Map(), notifications: previous?.notifications ?? new Map(),
      parent: previous?.parent, parentTool: previous?.parentTool, parentWork: previous?.parentWork,
      tools: new Map(), toolNames: new Map(), delegations: new Map(), background: new Map(), requests: [] };
  }

  private session(id: string): Conversation | undefined {
    if (!this.sessions.has(id)) {
      if (this.sessions.size >= LIMIT) {
        const idle = [...this.sessions].find(([sid, s]) => !s.turn && !s.pending.size && !this.outstanding.get(sid)?.size);
        if (!idle) return;
        this.sessions.delete(idle[0]);
        this.outstanding.delete(idle[0]);
      }
      this.sessions.set(id, this.fresh());
    }
    return this.sessions.get(id);
  }

  /** Context hooks are synchronous and neutral: no network, no prompt/model mutation. */
  context(sessionID: string, messages: unknown, system: unknown, kind = "primary"): void {
    if (this.stopped || !collectionEnabled(this.config)) return;
    if (messages && typeof messages === "object") {
      if (this.contexts.has(messages)) return;
      this.contexts.add(messages);
    }
    const s = this.session(sessionID);
    if (!s) return;
    // Histories mix user inputs with assistant/tool outputs. Never relabel hidden
    // outputs as request input (including compaction requests).
    const input = this.config.hideInputs || this.config.hideOutputs
      ? undefined : this.text({ system, messages });
    if (kind === "compaction") {
      // A context-length rejection never creates step.started. Compaction replaces
      // that abandoned primary request, before its durable start event is delivered.
      s.requests = [];
      s.request = input;
      if (s.compact) s.compact.input = input;
    } else if (s.requests.length < 16) s.requests.push({ input });
  }

  private startAttempt(s: Conversation, model = s.step?.model): void {
    const step = s.step;
    if (!s.turn || !step || step.handle) return;
    step.handle = s.turn.child({ id: `${step.id}:attempt:${step.attempt}`, kind: "llm",
      parentId: s.branchId, name: "generation", model: model?.id, provider: model?.providerID,
      input: step.input, startTime: step.started, fidelity: "native-request" });
  }

  toolBefore(sessionID: string, callID: string, name: string, input: unknown, messageID?: string): void {
    if (this.stopped || !collectionEnabled(this.config)) return;
    const s = (messageID ? this.works(sessionID).find(w => w.step?.id === messageID) : undefined) ?? this.sessions.get(sessionID);
    if (!s?.turn || s.tools.has(callID) || s.tools.size >= 512) return;
    const id = `${s.turnId}:${messageID ?? s.step?.id ?? "unattributed"}:tool:${callID}`;
    const parentId = s.step?.handle ? `${s.step.id}:attempt:${s.step.attempt}` : s.branchId;
    s.tools.set(callID, { id, handle: s.turn.child({ id, kind: "tool", parentId, name, toolCallId: callID, input: this.text(input) }) });
    if (s.step && (!messageID || s.step.id === messageID) && !this.config.hideOutputs) {
      s.step.toolCalls ??= [];
      if (s.step.toolCalls.length < 64) s.step.toolCalls.push({ name, arguments: this.text(input) });
    }
    if (name === "subagent" && s.delegations.size < 512) {
      const args = input && typeof input === "object" ? input as Record<string, unknown> : {};
      s.delegations.set(callID, { toolId: id, background: args.background === true });
    }
  }

  private delegation(sid: string, callID: string, childID: unknown): void {
    const parent = this.works(sid).find(w => w.delegations.has(callID)), call = parent?.delegations.get(callID);
    if (!parent?.turn || !call || typeof childID !== "string" || childID === sid) return;
    // Only the native subagent tool's progress/success metadata proves ancestry.
    // It is emitted before prompt delivery, including when resuming after reload.
    const child = this.session(childID);
    if (!child || (child.parent && child.parent !== sid)) return;
    if (!child.turn) { child.parent = sid; child.parentTool = call.toolId; child.parentWork = parent; }
    if (call.background && parent.background.size < LIMIT) parent.background.set(childID, parent.turn);
  }

  toolAfter(sessionID: string, callID: string, output: unknown, failed: boolean): void {
    const s = this.works(sessionID).find(w => w.tools.has(callID)), tool = s?.tools.get(callID);
    if (!tool) return;
    tool.handle.end({ output: failed && this.config.hideErrorDetails ? undefined : this.text(output, true), outcome: failed ? "error" : "success" });
    s!.tools.delete(callID);
  }

  event(event: OpenCodeEvent): void {
    try { this.accept(event); } catch { /* Observability must never break the agent. */ }
  }

  private accept(event: OpenCodeEvent): void {
    if (this.stopped || !collectionEnabled(this.config)) return;
    // Subscriptions are process-wide, even though hooks and plugin options are location-scoped.
    if (!("sessionID" in event.data) || typeof event.data.sessionID !== "string") return;
    if (event.location) {
      if (event.location.directory !== this.location.directory) return;
      if ("workspaceID" in event.location && event.location.workspaceID !== this.location.workspaceID) return;
    } else if (!this.sessions.has(event.data.sessionID)) return; // Execution/usage events omit location; only admitted local sessions qualify.
    if (this.seen.has(event.id)) return;
    this.seen.add(event.id);
    if (this.seen.size > 8192) this.seen.delete(this.seen.values().next().value!);
    const sid = event.data.sessionID;
    let s = this.session(sid);
    if (!s) return;
    if (!validShape(event)) {
      s.turn?.child({ id: `${s.turnId}:unsupported-shape`, kind: "agent", name: "unsupported-host-event" })
        .end({ outcome: "incomplete", gap: "UNSUPPORTED_EVENT_SHAPE" });
      return;
    }
    // Late terminal events address the source request/tool, not the latest prompt.
    if ("assistantMessageID" in event.data && event.type !== "session.step.started") {
      const messageID = event.data.assistantMessageID;
      s = this.works(sid).find(w => w.step?.id === messageID) ?? s;
    }
    if (event.type.startsWith("session.tool.") && "id" in event.data) {
      const callID = event.data.id;
      s = this.works(sid).find(w => w.tools.has(callID) || w.delegations.has(callID)) ?? s;
    }
    switch (event.type) {
      case "session.created":
        s.parent = event.data.parentID;
        s.parentWork = s.parent ? this.sessions.get(s.parent) : undefined;
        break;
      case "session.inbox.enqueued": {
        const item = event.data.item;
        if (item.type === "user" && s.pending.size < LIMIT)
          s.pending.set(event.data.inboxID, { text: this.text(item.payload.text), created: event.created });
        if (item.type === "synthetic" && s.notifications.size < LIMIT) {
          const metadata = item.payload.metadata;
          const childID = metadata?.source === "subagent" ? metadata.childID : undefined;
          const turn = typeof childID === "string" ? this.works(sid).find(w => w.background.has(childID))?.turn : undefined;
          if (turn && typeof childID === "string") s.notifications.set(event.data.inboxID, { childID, turn });
        }
        break;
      }
      case "session.inbox.cancelled": {
        s.pending.delete(event.data.inboxID);
        const notice = s.notifications.get(event.data.inboxID);
        s.notifications.delete(event.data.inboxID);
        const owner = notice && this.works(sid).find(w => w.turn === notice.turn);
        if (owner && notice) {
          owner.background.delete(notice.childID);
          this.finish(sid, "incomplete", event.created, false, owner);
        }
        break;
      }
      case "session.inbox.delivered": {
        const notification = s.notifications.get(event.data.inboxID);
        if (notification) {
          s.notifications.delete(event.data.inboxID);
          const owner = this.works(sid).find(w => w.turn === notification.turn);
          if (owner) {
            this.activate(sid, owner);
            owner.background.delete(notification.childID);
            // Resume the original turn even when another user prompt intervened.
            owner.outcome = undefined;
            owner.executing = true;
          }
          break;
        }
        const prompt = s.pending.get(event.data.inboxID);
        if (!prompt) break; // Synthetic messages/compaction/resume are not new user prompts.
        s.pending.delete(event.data.inboxID);
        if (s.turn) {
          // A user prompt is not an interrupt. Native terminal events and child
          // notifications retain independent ownership of the earlier work.
          if (this.allWorks().filter(([, w]) => w.turn).length >= LIMIT) {
            s.turn.child({ id: `${event.id}:capacity`, kind: "agent" }).end({ outcome: "incomplete", gap: "OUTSTANDING_TURN_LIMIT" });
            break;
          }
          const next = this.fresh(s);
          this.activate(sid, next);
          s = next;
        }
        const parent = s.parentWork ?? (s.parent ? this.sessions.get(s.parent) : undefined);
        s.parentWork = parent;
        s.turnId = event.data.inboxID;
        s.output = undefined;
        s.outcome = undefined;
        s.executing = true;
        if (parent?.turn) {
          // Only source parentID plus the subagent tool's sessionID metadata establishes tool ancestry.
          s.turn = parent.turn;
          s.branchId = `${sid}:${s.turnId}:subagent`;
          s.branch = s.turn.child({ id: s.branchId, kind: "agent", parentId: s.parentTool ?? parent.branchId,
            name: "subagent", input: prompt.text, startTime: prompt.created });
        } else {
          s.turn = this.tracer.startTurn({ sessionId: sid, turnId: s.turnId,
            input: prompt.text, startTime: prompt.created, usagePlan: "attempt" });
        }
        break;
      }
      case "session.step.started":
        if (!s.turn) break;
        if (s.step?.handle) s.step.handle.end({ outcome: "incomplete" });
        s.step = { id: event.data.assistantMessageID, model: event.data.model,
          attempt: s.step?.id === event.data.assistantMessageID ? s.step.attempt : 0,
          input: s.requests.shift()?.input ?? (s.step?.id === event.data.assistantMessageID ? s.step.input : undefined),
          started: event.data.started };
        // The durable step is the native model-dispatch boundary, including WebSocket requests.
        this.startAttempt(s);
        // Multiple inbox items can be delivered at one boundary. The native model
        // dispatch belongs to one root, but answers all participating prompt roots.
        // Record source span linkage, never cloned generations or additive usage.
        s.step.participants = this.works(sid).filter(work => work.turn && work.executing);
        if (s.step.participants.length > 1) {
          const context = s.step.handle?.context;
          for (const work of s.step.participants) work.turn?.event({
            name: context ? `opencode.shared-generation.${context.traceId}.${context.spanId}` : "opencode.shared-generation",
            input: JSON.stringify({ ownerTurn: s.turnId, request: s.step.id }), time: event.created,
          });
        }
        break;
      case "session.text.ended":
        if (s.step?.id === event.data.assistantMessageID) {
          s.step.output = ((s.step.output ?? "") + (this.text(event.data.text, true) ?? "")).slice(0, MAX_TEXT);
          for (const work of s.step.participants ?? [s]) work.output = s.step.output;
        }
        break;
      case "session.step.streamed":
        if (s.step?.id === event.data.assistantMessageID) s.step.streamed = event.created;
        break;
      case "session.retry.scheduled":
        if (s.step?.id === event.data.assistantMessageID) {
          s.step.handle?.end({ outcome: "error", endTime: event.created });
          s.step.handle = undefined;
          s.step.attempt = event.data.attempt;
          s.step.output = undefined;
          s.step.streamed = undefined;
        }
        break;
      case "session.step.ended":
      case "session.step.failed":
        if (s.step?.id === event.data.assistantMessageID) {
          const output = s.step.toolCalls?.length
            ? this.text({ text: s.step.output, tool_calls: s.step.toolCalls }, true) : s.step.output;
          s.step.handle?.end({ output, usage: usage(event.data.tokens, event.data.cost),
            outcome: event.type === "session.step.failed" ? (event.data.error?.type === "aborted" ? "interrupted" : "error") : "success", endTime: s.step.streamed ?? event.created });
          s.step = undefined;
          s.request = undefined;
        }
        break;
      case "session.tool.input.started":
        if (s.toolNames.size < 512) s.toolNames.set(event.data.id, event.data.name);
        break;
      case "session.tool.called":
        // Includes provider-executed tools, which don't go through execute.before.
        this.toolBefore(sid, event.data.id, s.toolNames.get(event.data.id) ?? "tool", event.data.input, event.data.assistantMessageID);
        break;
      case "session.tool.progress":
        this.delegation(sid, event.data.id, event.data.metadata.sessionID);
        break;
      case "session.tool.success":
      case "session.tool.failed":
        if (event.type === "session.tool.success") {
          // session.background can turn an initially foreground subagent into a
          // background task. Only terminal tool metadata proves that transition;
          // progress.status=running is also emitted by ordinary foreground calls.
          const call = s.delegations.get(event.data.id);
          if (call && event.data.metadata?.status === "running") call.background = true;
          this.delegation(sid, event.data.id, event.data.metadata?.sessionID);
        }
        this.toolAfter(sid, event.data.id, event.data.content ?? ("error" in event.data ? event.data.error : undefined), event.type === "session.tool.failed");
        s.toolNames.delete(event.data.id);
        s.delegations.delete(event.data.id);
        break;
      case "session.compaction.started":
        if (s.turn && !s.compact) {
          const id = `${event.id}:compaction`;
          s.compact = { id, started: event.created, input: s.request };
        }
        break;
      case "session.compaction.ended":
      case "session.compaction.failed": {
        const failed = event.type === "session.compaction.failed";
        // V2 reports summary-template retry usage here, never in step.ended.
        // These exclusive counters are additive, but the activity remains an
        // aggregate: no fabricated model requests or duplicate generation usage.
        if (s.compact && s.turn) s.turn.child({ id: s.compact.id, kind: "compaction", parentId: s.branchId,
          startTime: s.compact.started, input: s.compact.input, fidelity: "aggregate-turn", usageContribution: "exclusive" }).end({
            outcome: failed ? "error" : "success", usage: usage(event.data.tokens, event.data.cost),
            output: "text" in event.data ? this.text(event.data.text, true) : undefined, endTime: event.created });
        s.compact = undefined;
        break;
      }
      // Execution.start precedes inbox delivery; only delivery identifies its owner.
      case "session.execution.started": break;
      case "session.execution.succeeded":
      case "session.execution.failed":
      case "session.execution.interrupted": {
        const outcome = event.type === "session.execution.succeeded" ? "success" : event.type === "session.execution.failed" ? "error" : "interrupted";
        for (const work of this.works(sid)) if (work.executing) this.finish(sid, outcome, event.created, false, work);
        break;
      }
      case "session.deleted":
        for (const work of this.works(sid)) this.finish(sid, "incomplete", event.created, true, work);
        this.sessions.delete(sid); this.outstanding.delete(sid); break;
    }
  }

  private finish(id: string, outcome: TurnOutcome, time?: number, force = false, s = this.sessions.get(id)): void {
    if (!s?.turn) return;
    s.executing = false;
    const children = this.allWorks().filter(([, child]) => child.parent === id && child.turn === s.turn);
    if (!force && (children.length || s.background.size)) {
      s.outcome = outcome;
      return;
    }
    s.outcome = undefined;
    for (const [childID, child] of children) this.finish(childID, "incomplete", time, true, child);
    if (s.compact) s.turn.child({ id: s.compact.id, kind: "compaction", parentId: s.branchId,
      startTime: s.compact.started, input: s.compact.input, fidelity: "aggregate-turn" }).end({ outcome: "incomplete", endTime: time });
    s.step?.handle?.end({ outcome: "incomplete", endTime: time });
    for (const t of s.tools.values()) t.handle.end({ outcome: "incomplete", endTime: time });
    if (s.branch) s.branch.end({ outcome, output: s.output, endTime: time });
    else s.turn.end({ outcome, output: s.output, endTime: time });
    for (const [inboxID, notice] of s.notifications) if (notice.turn === s.turn) s.notifications.delete(inboxID);
    this.outstanding.get(id)?.delete(s);
    s.turn = undefined;
    s.branch = undefined;
    s.branchId = undefined;
    s.parentTool = undefined;
    s.step = undefined;
    s.compact = undefined;
    s.request = undefined;
    s.requests = [];
    s.output = undefined;
    s.tools.clear();
    s.toolNames.clear();
    s.delegations.clear();
    s.background.clear();
    if (s.parent && !force) {
      const parent = s.parentWork;
      if (parent?.outcome && !parent.executing) this.finish(s.parent, parent.outcome, time, false, parent);
    }
    s.parentWork = undefined;
    void this.tracer.flush();
  }

  async close(): Promise<void> {
    this.stopped = true;
    for (const [id, s] of this.allWorks()) if (s.branch) this.finish(id, "incomplete", undefined, true, s);
    for (const [id, s] of this.allWorks()) this.finish(id, "incomplete", undefined, true, s);
    this.sessions.clear();
    this.outstanding.clear();
    await this.tracer.shutdown();
  }
}

/** OpenCode 2.2.5 counters are disjoint (TokenUsage.total adds every field). */
function usage(tokens: { input: number; output: number; reasoning: number; cache: { read: number; write: number } } | undefined, cost?: number): UsageInput | undefined {
  if (!tokens) return;
  return { input: tokens.input, output: tokens.output, reasoning: tokens.reasoning,
    cacheRead: tokens.cache.read, cacheWrite: tokens.cache.write, cacheInInput: false, reasoningInOutput: false,
    // Zero means pricing is unavailable for custom providers; do not imply a free model call.
    ...(cost !== undefined && cost > 0 && Number.isFinite(cost) ?
      { costUsd: cost, costSource: "estimated" as const, costEstimator: "opencode-2.2.5-catalog" } : {}) };
}

/** Validate the parts of the public event contract we consume on auto-updated hosts. */
function validShape(event: OpenCodeEvent): boolean {
  const d = event.data as unknown as Record<string, unknown>;
  const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
  if (typeof event.id !== "string" || !("created" in event) || !Number.isFinite(event.created)) return false;
  if (event.type.startsWith("session.inbox.")) {
    if (typeof d.inboxID !== "string") return false;
    if (event.type === "session.inbox.enqueued" && (!object(d.item) || !object(d.item.payload))) return false;
  }
  if (event.type.startsWith("session.step.") || event.type === "session.text.ended" || event.type === "session.retry.scheduled") {
    if (typeof d.assistantMessageID !== "string") return false;
    if (event.type === "session.text.ended" && typeof d.text !== "string") return false;
    if (event.type === "session.step.started" && (!object(d.model) || typeof d.model.id !== "string" || typeof d.model.providerID !== "string")) return false;
  }
  if (event.type.startsWith("session.tool.")) {
    if (typeof d.id !== "string") return false;
    if (event.type === "session.tool.input.started" && typeof d.name !== "string") return false;
    if (event.type === "session.tool.progress" && !object(d.metadata)) return false;
  }
  return true;
}
