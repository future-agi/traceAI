import {
  createTurnTracer,
  collectionEnabled,
  type ResolvedConfig,
  type TurnTracer,
  type TurnHandle,
  type ChildHandle,
  type UsageInput,
} from "@traceai/agent-core";
import { type CursorEvent } from "./events.js";
import {
  TranscriptReader,
  correlate,
  type TranscriptResult,
} from "./transcript.js";

interface ActiveTurn {
  handle: TurnHandle;
  prompt?: string;
  output?: string;
  usage?: UsageInput;
  model?: string;
  tools: Map<string, ChildHandle>;
  finished: Set<string>;
  children: Map<string, ChildHandle>;
  transcript?: string;
  reader: TranscriptReader;
  created: number;
  startedAt: number;
  missingPrompt: boolean;
  bytes: number;
  compactions: number;
}
export class CursorObserver {
  private readonly tracer: TurnTracer;
  private readonly active = new Map<string, ActiveTurn>();
  private readonly completed = new Set<string>();
  private closed = false;
  readonly gaps = new Set<string>();
  constructor(
    private readonly config: ResolvedConfig,
    tracer?: TurnTracer,
    private readonly prepared?: TranscriptResult,
    private readonly profile: "ide" | "cloud" = "ide",
  ) {
    this.tracer = tracer ?? createTurnTracer({ config });
  }
  private key(event: CursorEvent): string {
    return JSON.stringify([event.conversation, event.generation]);
  }
  private gap(code: string): void {
    if (this.gaps.size < 32) this.gaps.add(code);
  }
  async accept(e: CursorEvent): Promise<void> {
    if (this.closed || !collectionEnabled(this.config)) {
      this.gap("DISABLED");
      return;
    }
    for (const [key, item] of this.active)
      if (Date.now() - item.created > 30 * 60 * 1000) {
        item.handle.end({ outcome: "incomplete" });
        this.active.delete(key);
        this.completed.add(key);
        this.gap("MISSING_STOP");
      }
    if (e.event === "sessionEnd") {
      const turns = [...this.active].filter(([key]) => JSON.parse(key)[0] === e.conversation);
      // Session totals cannot be copied onto several unfinished turns. Preserve
      // their own response usage instead when no unique turn owns the terminal.
      const terminal = turns.length > 1 ? { ...e, usage: undefined } : e;
      if (turns.length > 1 && e.usage) this.gap("AMBIGUOUS_SESSION_USAGE");
      for (const [key, item] of turns)
        await this.finishTurn(key, item, terminal, true);
      // sessionEnd uses the conversation id as generation_id in the CLI, unlike stop.
      // Do not add an empty fallback after a source turn has already been exported.
      if (
        [...this.completed].some((key) => JSON.parse(key)[0] === e.conversation)
      ) {
        await this.tracer.flush();
        return;
      }
    }
    if (e.gap) this.gap(e.gap);
    const key = this.key(e);
    if (this.completed.has(key)) return;
    const missingPrompt = e.event !== "beforeSubmitPrompt";
    const orphan =
      !this.active.has(key) &&
      [
        "preToolUse",
        "postToolUse",
        "postToolUseFailure",
        "afterAgentResponse",
        "stop",
        "sessionEnd",
      ].includes(e.event);
    if (e.event === "beforeSubmitPrompt" || orphan) {
      if (this.active.has(key)) return;
      if (this.active.size >= 2) {
        this.gap("TURN_LIMIT");
        return;
      }
      const item: ActiveTurn = {
        handle: this.tracer.startTurn({
          sessionId: e.conversation,
          // A terminal-only session is identified by the host's conversation id, not an invented prompt.
          turnId:
            e.generation ?? JSON.stringify(["sessionEnd", e.conversation]),
          input: missingPrompt ? undefined : e.input,
          startTime: new Date(
            e.receivedAt - (missingPrompt ? (e.durationMs ?? 0) : 0),
          ),
          timing: missingPrompt && e.durationMs !== undefined ? "estimated" : "observed-event",
          usagePlan: this.profile === "cloud" ? "unknown" : "turn",
          ...(this.profile === "cloud" ? {
            unit: missingPrompt ? "run" as const : "turn" as const,
            gaps: ["EARLY_READ_ONLY_ACTIVITY_UNAVAILABLE", "MODEL_REQUESTS_UNAVAILABLE",
              ...(missingPrompt ? ["PROMPT_NOT_OBSERVED"] : [])],
          } : {}),
        }),
        prompt: missingPrompt ? undefined : e.input,
        model: e.model,
        tools: new Map(),
        finished: new Set(),
        children: new Map(),
        reader: new TranscriptReader(this.config),
        transcript: e.transcript,
        created: Date.now(),
        startedAt: e.receivedAt - (missingPrompt ? (e.durationMs ?? 0) : 0),
        missingPrompt,
        bytes: e.input?.length ?? 0,
        compactions: 0,
      };
      this.active.set(key, item);
      if (e.transcript && !missingPrompt && this.profile === "ide") await item.reader.read(e.transcript);
      if (!orphan) return;
    }
    const item = this.active.get(key);
    if (!item) {
      this.gap("MISSING_PROMPT_OR_SOURCE_ALIAS");
      return;
    }
    item.transcript ??= e.transcript;
    item.model = e.model ?? item.model;
    const cost = (e.input?.length ?? 0) + (e.output?.length ?? 0);
    if (item.bytes + cost > 1024 * 1024) {
      e = { ...e, input: undefined, output: undefined };
      this.gap("TURN_CONTENT_LIMIT");
    } else item.bytes += cost;
    if (
      ["preToolUse", "postToolUse", "postToolUseFailure"].includes(e.event) &&
      e.toolId
    ) {
      // Cursor reuses a Read's id for its paired Write. Full id + tool kind
      // is source-proven identity; never split newlines or dedupe by id alone.
      const call = JSON.stringify([e.toolId, e.tool]);
      if (item.finished.has(call)) return;
      let tool = item.tools.get(call);
      if (!tool) {
        if (item.tools.size >= 64) {
          this.gap("TOOL_LIMIT");
          return;
        }
        tool = item.handle.child({
          id: "tool:" + call,
          kind: "tool",
          name: e.tool,
          toolCallId: e.toolId,
          input: e.input,
          fidelity: "unknown",
          startTime: new Date(e.receivedAt - (e.durationMs ?? 0)),
          timing: e.durationMs === undefined ? "observed-event" : "estimated",
        });
        item.tools.set(call, tool);
      }
      if (e.event !== "preToolUse") {
        tool.end({
          output: e.output,
          outcome:
            e.outcome ??
            (e.event === "postToolUseFailure" ? "error" : "success"),
          endTime: new Date(e.receivedAt),
        });
        item.finished.add(call);
      }
    } else if (e.event === "afterAgentResponse") {
      item.output = e.output;
      item.usage = e.usage ?? item.usage;
    } else if (e.event === "subagentStart" || e.event === "subagentStop") {
      // No FIFO, tool-name or text matching. Nested ancestry requires BOTH source ids.
      if (
        !e.subagentId ||
        !e.parentToolId ||
        !item.tools.has(JSON.stringify([e.parentToolId, "Task"]))
      ) {
        this.gap("UNPROVEN_SUBAGENT_PARENT");
        return;
      }
      let child = item.children.get(e.subagentId);
      if (!child && item.children.size < 32) {
        child = item.handle.child({
          id: "agent:" + e.subagentId,
          parentId: "tool:" + JSON.stringify([e.parentToolId, "Task"]),
          kind: "agent",
          name: "subagent",
          input: e.input,
          startTime: new Date(e.receivedAt),
          timing: "observed-event",
        });
        item.children.set(e.subagentId, child);
      }
      if (e.event === "subagentStop")
        child?.end({
          output: e.output,
          outcome: e.outcome ?? "incomplete",
          endTime: new Date(e.receivedAt),
        });
      // Separate subagent transcript schemas/turn ids remain unqualified. Do not borrow the parent's prompt.
      if (e.childTranscript)
        this.gap("SUBAGENT_TRANSCRIPT_CORRELATION_REQUIRED");
    } else if (e.event === "preCompact") {
      // A pre-event proves initiation, not successful completion.
      if (item.compactions < 16)
        item.handle
          .child({
            id: "compact:" + item.compactions++,
            kind: "compaction",
            name: "compaction",
            startTime: new Date(e.receivedAt),
            timing: "observed-event",
          })
          .end({ outcome: "incomplete", endTime: new Date(e.receivedAt) });
    } else if (e.event === "stop" || e.event === "sessionEnd") {
      await this.finishTurn(key, item, e, e.event === "sessionEnd");
      await this.tracer.flush();
    }
  }
  private async finishTurn(
    key: string,
    item: ActiveTurn,
    e: CursorEvent,
    sessionEnd = false,
  ): Promise<void> {
    item.transcript ??= e.transcript;
    // sessionEnd finalizes active turns before the ordinary event merge. Its
    // model is still source metadata for the reconstructed messages.
    item.model = e.model ?? item.model;
    let gap = item.missingPrompt
      ? sessionEnd
        ? "MISSING_PROMPT_AND_STOP"
        : "MISSING_PROMPT"
      : sessionEnd
        ? "MISSING_STOP"
        : undefined;
    let generations = 0;
    // Print mode proves tool work, but no submitted prompt or model usage. Do not borrow a transcript turn.
    if (this.profile === "ide" && !item.missingPrompt && item.transcript) {
      const result =
        this.prepared ??
        (await item.reader.readSettled(
          item.transcript,
          item.prompt,
          item.output,
          {
            enabled: () => collectionEnabled(this.config),
          },
        ));
      const transcript = correlate(result, item.prompt, item.output);
      if (!transcript) gap = result.gap ?? "TRANSCRIPT_CORRELATION_REQUIRED";
      else {
        generations = transcript.messages.length;
        for (const [index, message] of transcript.messages.entries()) {
          // Cursor supplies order, not model request timestamps. Interpolate inside the observed
          // turn and explicitly label reconstructed timing, so sorting cannot shuffle the final usage owner.
          const time = (position: number): [number, number] => {
            const ms =
              item.startedAt +
              (Math.max(0, e.receivedAt - item.startedAt) * position) /
                generations;
            const sec = Math.floor(ms / 1000);
            return [sec, Math.round((ms - sec * 1000) * 1e6)];
          };
          item.handle
            .child({
              id: "message:" + message.id,
              kind: "generation",
              name: "assistant-message",
              input: message.input,
              fidelity: "reconstructed-message",
              model: item.model,
              startTime: time(index),
              timing: "reconstructed",
            })
            .end({ output: message.output, endTime: time(index + 1) });
        }
      }
    } else if (this.profile === "ide" && !item.missingPrompt) gap ??= "TRANSCRIPT_UNAVAILABLE";
    if (this.profile === "ide" && !generations && !item.missingPrompt)
      gap ??= "TRANSCRIPT_NO_GENERATIONS";
    if (gap) this.gap(gap);
    item.handle.end({
      output: item.output,
      // Print-mode hooks have neither aggregate source, so this remains absent. A missing
      // prompt must not discard real totals from afterAgentResponse/stop on other runs.
      usage: this.profile === "cloud" ? undefined : e.usage ?? item.usage,
      gap,
      outcome: this.profile === "cloud" && item.missingPrompt && (!e.outcome || e.outcome === "success")
        ? "incomplete" : e.outcome ?? "incomplete",
      endTime: new Date(e.receivedAt),
    });
    this.active.delete(key);
    this.completed.add(key);
    if (this.completed.size > 4096)
      this.completed.delete(this.completed.values().next().value!);
  }
  get deliveryError(): string | undefined {
    return this.tracer.state.lastError;
  }
  async close(discard = false): Promise<void> {
    this.closed = true;
    if (discard) await this.tracer.discard();
    else await this.tracer.shutdown();
    this.active.clear();
  }
}
