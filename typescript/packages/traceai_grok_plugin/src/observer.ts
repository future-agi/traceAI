import type {
  ChildHandle,
  ResolvedConfig,
  TurnHandle,
  TurnTracer,
  UsageInput,
} from "@traceai/agent-core";
import { record, id, text, turnUsage, failedResult, type GrokEvent } from "./events.js";

interface Active {
  root: TurnHandle;
  handle: ChildHandle;
  parentId?: string;
  promptId?: string;
  toolStarts: Map<string, GrokEvent>;
  input?: string;
  context?: string;
  models: Map<
    string,
    { input?: string; output: string; start: number; end: number; model?: string; failed?: boolean }
  >;
  model?: string;
  transcriptPromptId?: string;
  usage?: UsageInput;
  terminal?: GrokEvent;
  completed?: boolean;
}
/** Process-local bounded state. No content journal, mutable exported roots or model-boundary guesses. */
export class GrokObserver {
  readonly active = new Map<string, Active>();
  private readonly seen = new Set<string>();
  private modelChars = 0;
  constructor(
    readonly tracer: TurnTracer,
    readonly config: ResolvedConfig,
  ) {}
  private once(key: string): boolean {
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    if (this.seen.size > 8192)
      this.seen.delete(this.seen.values().next().value!);
    return true;
  }
  handle(e: GrokEvent): void {
    let a = this.active.get(e.sessionId);
    if (e.event === "UserPromptSubmit" && e.promptId) {
      if (
        a?.promptId === e.promptId ||
        !this.once(`prompt:${e.sessionId}:${e.promptId}`)
      )
        return;
      if (!a?.parentId) {
        if (a) this.finish(e.sessionId, { ...e, outcome: "incomplete" });
        // Child sessions without observed SubagentStart must not become independent user turns.
        if (e.subagentType || this.active.size >= 128) return;
        const root = this.tracer.startTurn({
          sessionId: e.sessionId,
          turnId: e.promptId,
          input: e.input,
          startTime: e.time,
          usagePlan: "turn",
        });
        a = { root, handle: root, toolStarts: new Map(), models: new Map() };
        this.active.set(e.sessionId, a);
      }
      a.promptId = e.promptId;
      a.input = e.input;
      a.context = e.input;
      return;
    }
    if (!a) return;
    if (e.promptId && a.promptId && e.promptId !== a.promptId) return;
    if (
      e.event === "SubagentStart" &&
      e.subagentId &&
      !this.active.has(e.subagentId) &&
      this.active.size < 128
    ) {
      const parentId = `subagent:${e.subagentId}`;
      const handle = a.root.child({
        id: parentId,
        parentId: a.parentId,
        kind: "agent",
        name: "subagent",
        input: e.input,
        startTime: e.time,
      });
      this.active.set(e.subagentId, {
        root: a.root,
        handle,
        parentId,
        toolStarts: new Map(),
        models: new Map(),
      });
    } else if (e.event === "PreToolUse" && e.toolId) {
      if (a.toolStarts.size < 512 && !a.toolStarts.has(e.toolId))
        a.toolStarts.set(e.toolId, e);
    } else if (
      ["PostToolUse", "PostToolUseFailure", "PermissionDenied"].includes(
        e.event,
      ) &&
      e.toolId
    ) {
      if (!this.once(`tool:${e.sessionId}:${a.promptId}:${e.toolId}`)) return;
      const pre = a.toolStarts.get(e.toolId);
      a.toolStarts.delete(e.toolId);
      const child = a.root.child({
        id: `tool:${e.sessionId}:${e.toolId}`,
        parentId: a.parentId,
        kind: "tool",
        toolCallId: e.toolId,
        sourceTiming: "hook-interval",
        name: e.toolName,
        input: e.input ?? pre?.input,
        startTime:
          e.duration === undefined
            ? (pre?.time ?? e.time)
            : Math.max(0, e.time - e.duration),
      });
      child.end({ output: e.output, outcome: e.outcome, endTime: e.time });
    } else if (e.event === "PostCompact") {
      if (this.once(`compact:${e.sessionId}:${e.time}`))
        a.root
          .child({
            id: `compact:${e.sessionId}:${e.time}`,
            kind: "compaction",
            parentId: a.parentId,
            startTime: e.time,
          })
          .end({ endTime: e.time });
    } else if (
      ["Stop", "StopFailure", "StopCancelled", "SubagentStop"].includes(e.event)
    ) {
      if (
        !e.promptId ||
        (e.event === "SubagentStop" && e.phase && e.phase !== "gate")
      )
        return;
      // Stop hooks precede the persisted turn_completed record. Runtime settles the transcript before finalizing.
      if (
        !a.terminal ||
        a.terminal.event === "SessionEnd" ||
        e.event === "StopCancelled" ||
        e.event === "StopFailure"
      )
        a.terminal = e;
    } else if (e.event === "SessionEnd" && !a.terminal) {
      a.terminal = { ...e, outcome: "incomplete" };
    }
  }
  transcript(value: unknown): void {
    const row = record(value),
      p = record(row.params),
      u = record(p.update),
      m = record(p._meta),
      a = this.active.get(p.sessionId);
    if (!a || !a.promptId) return;
    if (u.sessionUpdate === "turn_completed") {
      if (u.prompt_id === a.promptId) {
        a.usage = turnUsage(u.usage);
        a.completed = true;
        // The persisted successful turn boundary can recover an earlier failed attempt.
        if (u.stop_reason === "end_turn" && (!a.terminal || a.terminal.event === "StopFailure"))
          a.terminal = { sessionId: p.sessionId, promptId: a.promptId, time: m.agentTimestampMs ?? Date.now(), ...a.terminal, event: "Stop", outcome: "success" };
        if (["error", "failed", "failure", "model_error", "api_error"].includes(u.stop_reason))
          a.terminal = {
            ...(a.terminal ?? { event: "StopFailure", sessionId: p.sessionId, promptId: a.promptId, time: m.agentTimestampMs ?? Date.now() }),
            outcome: "error",
          };
        if (
          [
            "cancelled",
            "user_interrupt",
            "max_turns",
            "permission_rejected",
            "permission_cancelled",
          ].includes(u.stop_reason) &&
          a.terminal
        )
          a.terminal = { ...a.terminal, outcome: "interrupted" };
      }
      return;
    }
    if (
      u.sessionUpdate === "hook_execution" &&
      u.event_name === "user_prompt_submit"
    ) {
      a.transcriptPromptId = id(u.prompt_id);
      return;
    }
    if (u.sessionUpdate === "user_message_chunk") {
      // Grok omits promptId here; its immediately preceding prompt-hook record supplies the anchor.
      if ((m.promptId ?? a.transcriptPromptId) === a.promptId) {
        a.model = id(record(u._meta).modelId);
        if (a.input === undefined) {
          a.input = text(record(u.content).text, this.config);
          a.context = a.input;
        }
      }
      return;
    }
    if (m.promptId === a.promptId && u.sessionUpdate === "tool_call_update") {
      const output = text(u.rawOutput ?? u.content, this.config, true, u.status === "failed" || failedResult(u.rawOutput ?? u.content));
      if (output !== undefined && !this.config.hideInputs)
        a.context = `${a.context ?? ""}\nTool result: ${output}`.slice(-20000);
      return;
    }
    if (
      m.promptId !== a.promptId ||
      !["agent_message_chunk", "agent_thought_chunk", "tool_call"].includes(
        u.sessionUpdate,
      )
    )
      return;
    if (
      !Number.isSafeInteger(m.streamStartMs) ||
      !Number.isSafeInteger(m.agentTimestampMs) ||
      !id(m.eventId) ||
      !this.once(`update:${m.eventId}`)
    )
      return;
    const key = `message:${p.sessionId}:${m.streamStartMs}`;
    if (!a.models.has(key) && a.models.size >= 256) return;
    const model = a.models.get(key) ?? {
      input: this.config.hideInputs ? undefined : a.context,
      output: "",
      failed: false,
      start: m.streamStartMs,
      end: m.agentTimestampMs,
      model: a.model,
    };
    const content =
      u.sessionUpdate === "tool_call"
        // Arguments are tool input even when embedded in a generation's output.
        ? { tool: u.title, ...(this.config.hideInputs ? {} : { arguments: u.rawInput }) }
        : record(u.content).text;
    model.failed ||= failedResult(content);
    const addition = (text(content, this.config, true, model.failed) ?? "").slice(
      0,
      Math.max(
        0,
        Math.min(20000 - model.output.length, 1024 * 1024 - this.modelChars),
      ),
    );
    model.output += addition;
    if (addition && !this.config.hideInputs)
      a.context = `${a.context ?? ""}\n${addition}`.slice(-20000);
    this.modelChars += addition.length;
    model.end = Math.max(model.end, m.agentTimestampMs);
    a.models.set(key, model);
  }
  finish(sessionId: string, terminal?: GrokEvent): void {
    const a = this.active.get(sessionId);
    if (!a) return;
    const e = terminal ?? a.terminal;
    if (!e) return;
    for (const [key, m] of a.models) {
      this.modelChars -= m.output.length;
      a.root
        .child({
          id: key,
          parentId: a.parentId,
          kind: "generation",
          name: "assistant-message",
          fidelity: "reconstructed-message",
          timing: "reconstructed",
          contentSource: "reconstructed-rollout",
          sourceTiming: "message-interval",
          input: m.input,
          model: m.model,
          startTime: m.start,
        })
        .end({
          output: m.output || undefined,
          endTime: m.end,
          outcome: m.failed || (key === [...a.models.keys()].at(-1) && e.outcome === "error") ? "error" : "success",
        });
    }
    for (const [key, pre] of a.toolStarts) {
      a.root
        .child({
          id: `tool:${sessionId}:${key}`,
          parentId: a.parentId,
          kind: "tool",
          toolCallId: key,
          sourceTiming: "hook-interval",
          name: pre.toolName,
          input: pre.input,
          startTime: pre.time,
        })
        .end({ outcome: "incomplete", endTime: e.time });
    }
    a.handle.end({
      output: e.output,
      outcome:
        (a.toolStarts.size || !a.completed) && e.outcome === "success" ? "incomplete" : e.outcome,
      gap: !a.completed ? "MISSING_TURN_COMPLETION" : undefined,
      usage: a.parentId ? undefined : a.usage,
      endTime: Math.max(e.time, ...[...a.models.values()].map((m) => m.end)),
    });
    this.active.delete(sessionId);
    // A parent terminal boundary fences unresolved descendants; do not create orphan roots later.
    if (!a.parentId)
      for (const [sid, child] of this.active)
        if (child.root === a.root) {
          for (const m of child.models.values())
            this.modelChars -= m.output.length;
          this.active.delete(sid);
        }
  }
}
