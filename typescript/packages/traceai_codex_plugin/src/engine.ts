import {
  createTurnTracer,
  type TurnTracer,
  type ChildHandle,
  type ResolvedConfig,
  type UsageInput,
  type TurnOutcome,
} from "@traceai/agent-core";
import { type Event } from "./events";
import { RolloutReader } from "./rollout";
interface Turn {
  events: Event[];
  readers: Map<string, RolloutReader>;
  gap: boolean;
}
const sourceKey = (e: Event): string =>
  JSON.stringify([
    e.source_session_id ?? e.session_id,
    e.source_turn_id ?? e.turn_id,
  ]);
const usage = (e: Event): UsageInput | undefined =>
  e.token_usage
    ? {
        input: e.token_usage.input_tokens,
        output: e.token_usage.output_tokens,
        cacheRead: e.token_usage.cached_input_tokens,
        reasoning: e.token_usage.reasoning_output_tokens,
        cacheInInput: true,
        reasoningInOutput: true,
      }
    : undefined;
/** Hooks are the authoritative request/attempt boundaries; rollout messages only enrich content.
 * A real Stop/Interrupt finalizes the immutable turn. Idle/session-end is incomplete, never success.
 */
export class CodexEngine {
  private turns = new Map<string, Turn>();
  private completed = new Set<string>();
  readonly tracer: TurnTracer;
  constructor(
    private config: ResolvedConfig,
    tracer?: TurnTracer,
    private settledReaders: RolloutReader[] = [],
  ) {
    this.tracer = tracer ?? createTurnTracer({ config });
  }
  async accept(event: Event): Promise<void> {
    if (event.hook_event_name === "SessionEnd") {
      await this.close();
      return;
    }
    if (!event.turn_id || event.hook_event_name === "SessionStart") return;
    const key = event.session_id + ":" + event.turn_id;
    if (this.completed.has(key)) return;
    let state = this.turns.get(key);
    if (!state) {
      if (this.turns.size >= 16) return;
      state = {
        events: [],
        readers: new Map(),
        gap: event.hook_event_name !== "UserPromptSubmit",
      };
      this.turns.set(key, state);
    }
    state.events.push(event);
    state.gap ||= !!event.gap;
    const sourceSession = event.source_session_id ?? event.session_id;
    const sourceTurn = event.source_turn_id ?? event.turn_id;
    if (event.transcript_path && !state.readers.has(sourceKey(event))) {
      if (state.readers.size >= 16) state.gap = true;
      else
        state.readers.set(
          sourceKey(event),
          // The async observer already paid the bounded history scan. Keep its
          // sanitized target-turn content instead of rescanning from byte zero.
          this.settledReaders.find((r) =>
            r.matches(event.transcript_path!, sourceSession, sourceTurn),
          ) ??
            new RolloutReader(
              event.transcript_path,
              sourceSession,
              this.config,
              sourceTurn,
            ),
        );
    }
    if (
      event.hook_event_name === "Stop" ||
      event.hook_event_name === "Interrupt"
    ) {
      // Drain bounded chunks; settling stays inside the host's async Stop hook.
      for (let i = 0; i < 20; i++) {
        await Promise.all([...state.readers.values()].map((r) => r.read()));
        if ([...state.readers.values()].some((r) => r.hasMore)) continue;
        if (i >= 2) break;
        await new Promise((r) => setTimeout(r, 40));
      }
      // Exhausting enrichment's byte budget is not a complete successful read.
      state.gap ||= [...state.readers.values()].some(
        (r) => r.hasMore || !r.targetSeen,
      );
      this.finish(
        key,
        state,
        event.hook_event_name === "Interrupt" ? "interrupted" : "success",
      );
      await this.tracer.flush();
      await Promise.all(
        [...state.readers.values()].map((r) => r.checkpoint().catch(() => {})),
      );
    }
  }
  private finish(key: string, state: Turn, outcome: TurnOutcome): void {
    this.turns.delete(key);
    this.completed.add(key);
    if (this.completed.size > 512)
      this.completed.delete(this.completed.values().next().value!);
    const first = state.events[0],
      last = state.events.at(-1)!;
    const prompt = state.events.find(
      (e) => e.hook_event_name === "UserPromptSubmit" && !e.agent_id,
    );
    const aggregateOnly = state.events.filter(e => ["PreModelRequest", "PreToolUse", "SubagentStart", "PreCompact"].includes(e.hook_event_name)).length > 240;
    const root = this.tracer.startTurn({
      sessionId: first.session_id,
      turnId: first.turn_id!,
      input: prompt?.prompt,
      startTime: first.receivedAt,
      timing: "observed-event",
      usagePlan: aggregateOnly ? "turn" : "attempt",
    });
    if (aggregateOnly) {
      const totals: UsageInput = { cacheInInput: true, reasoningInOutput: true };
      const requests = state.events.filter(e => e.hook_event_name === "PostModelRequest");
      for (const request of requests) {
        const counters = usage(request);
        for (const field of ["input", "output", "cacheRead", "reasoning"] as const)
          if (counters?.[field] !== undefined) totals[field] = (totals[field] ?? 0) + counters[field]!;
      }
      const finalRequest = requests.filter(e => !e.agent_id).at(-1);
      root.end({ output: last.last_assistant_message, usage: totals, gap: "SPAN_DETAIL_LIMIT",
        outcome: outcome === "interrupted" ? outcome : finalRequest?.status === "failed" ? "error" : outcome,
        endTime: last.receivedAt });
      return;
    }
    if (state.events.some((e) => e.content_truncated))
      root.event({ name: "codex.content_truncated" });
    const models = new Map<
      string,
      { handle: ChildHandle; start: Event; index: number }
    >();
    const tools = new Map<
      string,
      { handle: ChildHandle; input?: string; start: Event }
    >();
    const toolKey = (e: Event) => JSON.stringify([sourceKey(e), e.tool_use_id]);
    const children = new Map<string, ChildHandle>();
    const compacts = new Map<string, ChildHandle>();
    const laneOf = (e: Event) =>
      e.agent_id
        ? e.agent_id + (e.source_turn_id ? `:${e.source_turn_id}` : "")
        : "main";
    const lanes = new Map<string, Event>();
    const parents = new Map<string, string>();
    for (const e of state.events) {
      if (e.agent_id && !lanes.has(laneOf(e))) lanes.set(laneOf(e), e);
      if (e.parent_agent_id)
        parents.set(
          laneOf(e),
          e.parent_agent_id + (e.parent_turn_id ? `:${e.parent_turn_id}` : ""),
        );
    }
    // Native spawn/send results identify the actual executing parent, even when
    // SubagentStart reports the root session and arrives before that tool result.
    for (const e of state.events) {
      for (const [lane, child] of lanes) {
        if (
          e.spawned_agent_id === child.agent_id ||
          (e.dispatch_target === child.agent_id &&
            e.dispatch_turn === child.source_turn_id)
        )
          parents.set(lane, laneOf(e));
      }
    }
    const ensureAgent = (lane: string, visiting = new Set<string>()): void => {
      if (lane === "main" || children.has(lane)) return;
      if (visiting.has(lane) || !lanes.has(lane)) {
        state.gap = true;
        return;
      }
      visiting.add(lane);
      const parent = parents.get(lane);
      if (parent && parent !== "main") ensureAgent(parent, visiting);
      children.set(
        lane,
        root.child({
          id: `agent:${lane}`,
          kind: "agent",
          name: "codex.subagent",
          parentId:
            parent && parent !== "main" && children.has(parent)
              ? `agent:${parent}`
              : undefined,
          startTime: lanes.get(lane)!.receivedAt,
          timing: "observed-event",
        }),
      );
    };
    let attempt = 0;
    for (const e of state.events) {
      const lane = laneOf(e);
      const reader = state.readers.get(sourceKey(e));
      // send_input starts another execution of the same lifetime agent without
      // another SubagentStart. Each source turn gets its own nested parent.
      if (e.agent_id) ensureAgent(lane);
      const parentId = children.has(lane) ? `agent:${lane}` : undefined;
      switch (e.hook_event_name) {
        case "SubagentStart":
          break;
        case "SubagentStop":
          children.get(lane)?.end({
            output: e.last_assistant_message,
            outcome: e.status === "cancelled" ? "interrupted" : "success",
            endTime: e.receivedAt,
          });
          break;
        case "PreModelRequest": {
          models
            .get(lane)
            ?.handle.end({ outcome: "incomplete", endTime: e.receivedAt });
          // Input is explicitly the user prompt, not a fabricated full provider request body.
          const handle = root.child({
            id: `attempt:${++attempt}`,
            parentId,
            kind: "llm",
            name: "codex.model_attempt",
            model:
              e.model ??
              reader?.modelAt(e.receivedAt, e.source_turn_id ?? e.turn_id!),
            startTime: e.receivedAt,
            timing: "observed-event",
            sourceTiming: "hook-interval",
            fidelity: "native-request",
            contentSource: "reconstructed-rollout",
            input:
              state.events.find(
                (p) =>
                  p.hook_event_name === "UserPromptSubmit" &&
                  p.agent_id === e.agent_id &&
                  (p.source_turn_id ?? p.turn_id) ===
                    (e.source_turn_id ?? e.turn_id),
              )?.prompt ??
              reader?.messages.find(
                (m) =>
                  m.role === "user" &&
                  m.turnId === (e.source_turn_id ?? e.turn_id),
              )?.text ??
              (e.agent_id ? undefined : prompt?.prompt),
          });
          models.set(lane, { handle, start: e, index: attempt });
          break;
        }
        case "PostModelRequest": {
          const m = models.get(lane);
          if (!m) {
            state.gap = true;
            break;
          }
          // Only messages within this source interval; retries with no messages stay empty.
          const output =
            reader?.messages
              .filter(
                (r) =>
                  r.role === "assistant" &&
                  (!r.turnId || r.turnId === (e.source_turn_id ?? e.turn_id)) &&
                  r.at >= m.start.receivedAt &&
                  r.at <= e.receivedAt + 50,
              )
              .map((r) => r.text)
              .filter(Boolean)
              .join("\n") || undefined;
          m.handle.end({
            model: e.model,
            output: e.status === "failed" ? e.error : output,
            usage: usage(e),
            outcome:
              e.status === "cancelled"
                ? "interrupted"
                : e.status === "failed"
                  ? "error"
                  : "success",
            endTime: e.receivedAt,
          });
          models.delete(lane);
          break;
        }
        case "PreToolUse": {
          if (!e.tool_use_id) break;
          const input =
            e.tool_input ?? reader?.toolCalls.get(e.tool_use_id)?.text;
          tools.set(toolKey(e), {
            start: e,
            input,
            handle: root.child({
              id: `tool:${toolKey(e)}`,
              parentId,
              kind: "tool",
              name: e.tool_name,
              toolCallId: e.tool_use_id,
              input,
              startTime: e.receivedAt,
              timing: "observed-event",
              sourceTiming: "hook-interval",
            }),
          });
          break;
        }
        case "PostToolUse": {
          const tool = e.tool_use_id ? tools.get(toolKey(e)) : undefined;
          if (tool) {
            const result = reader?.toolResults.get(e.tool_use_id!);
            const failed = e.failed || result?.failed;
            const output =
              failed && this.config.hideErrorDetails
                ? undefined
                : (e.tool_response ?? result?.text);
            let gap: string | undefined;
            if (tool.input === undefined || output === undefined) {
              if (!this.config.captureContent) gap = "TOOL_CONTENT_DISABLED";
              else if (
                (tool.input === undefined && this.config.hideInputs) ||
                (output === undefined && this.config.hideOutputs)
              )
                gap = "TOOL_CONTENT_HIDDEN";
              else if (failed && this.config.hideErrorDetails)
                gap = "TOOL_ERROR_DETAILS_HIDDEN";
              else if (
                result?.limited ||
                reader?.toolCalls.get(e.tool_use_id!)?.limited
              )
                gap = "ROLLOUT_TOOL_CONTENT_LIMIT";
              else if (reader?.unsupportedTools.has(e.tool_use_id!))
                gap = "UNSUPPORTED_ROLLOUT_TOOL_CONTENT";
              else if (tool.start.content_truncated || e.content_truncated)
                gap = "HOOK_CONTENT_EVICTED_ROLLOUT_UNAVAILABLE";
              else gap = "TOOL_CONTENT_NOT_PROVIDED";
            }
            tool.handle.end({
              output,
              gap,
              outcome: failed ? "error" : "success",
              endTime: e.receivedAt,
            });
            tools.delete(toolKey(e));
          } else state.gap = true;
          break;
        }
        case "PreCompact":
          compacts.set(
            lane,
            root.child({
              id: `compact:${state.events.indexOf(e)}`,
              parentId,
              kind: "compaction",
              name: "codex.compaction",
              startTime: e.receivedAt,
              timing: "observed-event",
            }),
          );
          break;
        case "PostCompact":
          compacts.get(lane)?.end({ endTime: e.receivedAt });
          compacts.delete(lane);
          break;
      }
    }
    root.end({
      output: last.last_assistant_message,
      outcome:
        (state.gap || [...state.readers.values()].some((r) => r.gap)) &&
        outcome === "success"
          ? "incomplete"
          : outcome,
      endTime: last.receivedAt,
    });
  }
  async close(): Promise<void> {
    for (const [key, state] of this.turns)
      this.finish(key, state, "incomplete");
    await this.tracer.flush();
    await this.tracer.shutdown();
  }
}
