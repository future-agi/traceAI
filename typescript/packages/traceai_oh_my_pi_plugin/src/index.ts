import { createHash, randomUUID } from "node:crypto";
import {
  createTurnTracer,
  resolveConfig,
  collectionEnabled,
  redact,
  type TurnTracer,
  type TurnHandle,
  type ChildHandle,
  type TurnOutcome,
  type ResolvedConfig,
} from "@traceai/agent-core";
import { boundedJson, messageText, ompUsage } from "./content";

/** Structural subset of OMP 18.17.0's extension API; no runtime import of the host. */
export interface OmpContext {
  cwd: string;
  agent: { kind: "main" | "sub"; id: string; parentId?: string };
  sessionManager: { getSessionId(): string };
  model?: { id?: string; provider?: string };
}
export interface OmpExtensionAPI {
  on(
    event: string,
    handler: (event: any, context: OmpContext) => Promise<void>,
  ): void;
  events: {
    on(channel: string, handler: (data: any) => void): (() => void) | void;
  };
}
interface Scope {
  id?: string;
  handle?: ChildHandle;
  input?: string;
  next: number;
  tools: Map<string, { id: string; handle: ChildHandle }>;
  output?: string;
  outcome: TurnOutcome;
  seen: Set<string>;
}
interface Work {
  turn: TurnHandle;
  root: Scope;
  children: Map<string, Scope>;
  pendingChildren: Set<string>;
  terminal: boolean;
  ended: boolean;
}
const scope = (id?: string, handle?: ChildHandle): Scope => ({
  id,
  handle,
  next: 0,
  tools: new Map(),
  outcome: "success",
  seen: new Set(),
});
const validId = (v: unknown): v is string =>
  typeof v === "string" && v.length > 0 && v.length < 512;
const ms = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : undefined;

/** Each factory is rebound by OMP for children. All mutable trace state stays in this closure.
 * The main factory consumes OMP's propagated task events, which include authoritative parent
 * tool-call IDs. Child factories do not independently export those same generations. */
export default function futureAgi(api: OmpExtensionAPI): void {
  let tracer: TurnTracer | undefined, config: ResolvedConfig | undefined;
  let active: Work | undefined,
    preparation: { input?: string } | undefined,
    session = "",
    stopped = false;
  let serial = Promise.resolve();
  const works = new Set<Work>();
  const childOwners = new Map<string, { work: Work; scope: Scope }>();
  // Retain only bounded, verified ancestry, not completed work trees or captured content.
  const retained = new Map<
    string,
    {
      parentToolCallId: string;
      context: NonNullable<ChildHandle["context"]>;
    }
  >();
  const unsubscribers: Array<() => void> = [];
  const safe = (fn: () => void | Promise<void>): Promise<void> => {
    serial = serial.then(fn).catch(() => {
      /* observation must never change the host result */
    });
    return serial;
  };
  const on = (
    name: string,
    fn: (e: any, c: OmpContext) => void | Promise<void>,
  ) =>
    api.on(name, (e, c) =>
      safe(() => {
        if (c?.agent?.kind !== "main" || (stopped && name !== "session_start"))
          return;
        return fn(e, c);
      }),
    );
  function discard(): void {
    const previous = tracer;
    // Fence synchronously before shutdown can end open spans or drain buffered content.
    previous?.discard();
    tracer = undefined;
    config = undefined;
    active = undefined;
    preparation = undefined;
    works.clear();
    childOwners.clear();
    retained.clear();
    void previous?.shutdown();
  }
  function enabled(): boolean {
    if (config && tracer && collectionEnabled(config)) return true;
    // A disable generation fences the old provider forever, even after enable.
    discard();
    return false;
  }
  const capture = (v: unknown) =>
    config?.hideInputs ? undefined : boundedJson(v);
  function finish(w: Work, forced?: TurnOutcome): void {
    if (w.ended || (!forced && (!w.terminal || w.pendingChildren.size))) return;
    w.ended = true;
    w.turn.end({ outcome: forced ?? w.root.outcome, output: w.root.output });
    works.delete(w);
    for (const [id, owner] of childOwners)
      if (owner.work === w) childOwners.delete(id);
    if (active === w) active = undefined;
    void tracer?.flush();
  }
  async function configure(c: OmpContext): Promise<void> {
    const id = c.sessionManager.getSessionId();
    if (!validId(id)) return;
    if (session && id !== session) {
      for (const w of works) finish(w, "incomplete");
      preparation = undefined;
      retained.clear();
    }
    session = id;
    enabled();
    // Outstanding background children must not freeze the next prompt's privacy policy.
    // Preserve their provider only when the freshly resolved configuration is unchanged.
    try {
      const next = await resolveConfig({
        agent: "oh-my-pi",
        projectDir: c.cwd,
      });
      if (
        JSON.stringify(next) !== JSON.stringify(config) ||
        next.credentials?.apiKey !== config?.credentials?.apiKey ||
        next.credentials?.secretKey !== config?.credentials?.secretKey ||
        next.identityKey !== config?.identityKey
      ) {
        // Do not export old content on replacement, even when policy became stricter.
        discard();
        config = next;
        tracer = next.enabled ? createTurnTracer({ config: next }) : undefined;
      }
    } catch {
      discard();
    }
  }
  function beginExecution(): void {
    if (!preparation) return;
    const { input } = preparation;
    preparation = undefined;
    start(input);
  }
  function start(input?: string): void {
    if (!enabled() || !session) return;
    if (active) {
      active.terminal = true;
      finish(active);
    }
    active = undefined; // A capped new work item must not append events to an older background turn.
    if (works.size >= 16) return;
    active = {
      turn: tracer!.startTurn({
        sessionId: session,
        turnId: randomUUID(),
        input,
        usagePlan: "attempt",
      }),
      root: scope(),
      children: new Map(),
      pendingChildren: new Set(),
      terminal: false,
      ended: false,
    };
    active.root.input = input;
    active.turn.gap("AUXILIARY_GENERATIONS_UNAVAILABLE");
    works.add(active);
  }
  function child(
    w: Work,
    s: Scope,
    kind: "llm" | "generation" | "tool" | "compaction",
    name: string,
    input?: string,
    startTime?: number,
    model?: string,
    provider?: string,
    toolCallId?: string,
  ) {
    const id = `${s.id ?? "main"}/${kind}/${++s.next}`;
    return {
      id,
      handle: w.turn.child({
        id,
        parentId: s.id,
        kind,
        name,
        toolCallId,
        input,
        startTime,
        model,
        provider,
        fidelity:
          kind === "llm"
            ? "native-request"
            : kind === "generation"
              ? "reconstructed-message"
              : "unknown",
        timing:
          kind === "generation" || kind === "compaction"
            ? "reconstructed"
            : undefined,
      }),
    };
  }
  function appendContext(s: Scope, role: string, text?: string): void {
    if (config?.hideInputs || !text) return;
    // Partial observed context, not the full provider request. Bound retained memory.
    s.input = [s.input, `${role}: ${text}`]
      .filter(Boolean)
      .join("\n")
      .slice(-20000);
  }
  function event(w: Work, s: Scope, e: any, c?: OmpContext): void {
    if (w.ended || !enabled()) return;
    if (
      !e ||
      typeof e.type !== "string" ||
      (e.type === "message_end" && !e.message?.role) ||
      (e.type.startsWith("tool_execution_") && !validId(e.toolCallId))
    ) {
      w.turn.gap("UNKNOWN_OMP_EVENT_SHAPE");
      return;
    }
    if (e.type === "message_end" && e.message?.role === "user") {
      (s.handle ?? w.turn).images?.(e.message.content, { direction: "input", purpose: "model_input", role: "user" });
      appendContext(s, "user", capture(messageText(e.message.content)));
      return;
    }
    // OMP 18's provider callback carries only payload, not the request/agent ID.
    // Advisor requests use it too, but their completions are not message_end events.
    // Never guess by callback order or model: reconstruct only actual source messages.
    if (e.type === "before_provider_request") return;
    if (e.type === "message_end" && e.message?.role === "assistant") {
      const m = e.message;
      if (
        !Array.isArray(m.content) ||
        typeof m.model !== "string" ||
        ms(m.timestamp) === undefined ||
        typeof m.stopReason !== "string"
      ) {
        w.turn.gap("UNKNOWN_OMP_EVENT_SHAPE");
        return;
      }
      const identity = createHash("sha256")
        .update(
          JSON.stringify([
            m.timestamp,
            m.completedAt,
            m.model,
            m.stopReason,
            boundedJson(m.content),
          ]),
        )
        .digest("hex");
      if (s.seen.has(identity)) return;
      s.seen.add(identity);
      if (s.seen.size > 512) s.seen.delete(s.seen.values().next().value!);
      const request = child(
        w,
        s,
        "generation",
        "generation",
        s.input,
        ms(m.timestamp),
        m.model,
        m.provider,
      ).handle;
      const outcome: TurnOutcome =
        m.stopReason === "error"
          ? "error"
          : m.stopReason === "aborted"
            ? "interrupted"
            : "success";
      const text = messageText(m.content);
      const calls = m.content
        .filter((p: any) => p?.type === "toolCall")
        .map((p: any) => ({
          type: "toolCall",
          name: p.name,
          id: p.id,
          ...(config?.hideInputs ? {} : { arguments: p.arguments }),
        }));
      const output = [text, calls.length ? boundedJson(calls) : undefined]
        .filter(Boolean)
        .join("\n");
      request.end({
        output,
        outcome,
        usage: ompUsage(m.usage),
        endTime: ms(m.completedAt),
        timeToFirstTokenMs: ms(m.ttft),
      });
      s.output =
        config?.hideOutputs || (config?.hideErrorDetails && outcome === "error")
          ? undefined
          : redact(output).slice(0, 20000);
      appendContext(s, "assistant", s.output);
      // A recovered retry can finish successfully while the failed attempt remains an error.
      s.outcome = outcome;
    } else if (e.type === "tool_execution_start" && validId(e.toolCallId)) {
      if (s.tools.has(e.toolCallId) || s.tools.size >= 512) return;
      s.tools.set(
        e.toolCallId,
        child(w, s, "tool", e.toolName ?? "tool", capture(e.args), undefined, undefined, undefined, e.toolCallId),
      );
    } else if (e.type === "tool_execution_end" && validId(e.toolCallId)) {
      const output =
        config?.hideOutputs || (config?.hideErrorDetails && e.isError)
          ? undefined
          : boundedJson(e.result);
      s.tools.get(e.toolCallId)?.handle.images?.(e.result?.content, {
        direction: "output", purpose: "tool_result", role: "tool", toolCallId: e.toolCallId,
        capture: { modelInput: true, errorDetail: e.isError === true },
      });
      s.tools.get(e.toolCallId)?.handle.end({
        output,
        outcome: e.isError ? "error" : "success",
      });
      appendContext(s, "tool", output);
      // Keep the span ID until work completion: background children can start/settle after a
      // task tool returns its job ID. end() is idempotent in the shared core.
    }
  }
  on("session_start", async (_e, c) => {
    stopped = false;
    await configure(c);
  });
  for (const name of ["session_switch", "session_branch"])
    on(name, async (_e, c) => {
      for (const w of works) finish(w, "incomplete");
      preparation = undefined;
      retained.clear();
      await configure(c);
    });
  // OMP can retry preparation when another extension changes tools or policy. Keep only
  // the latest preparation; it is not a trace until agent_start (or an execution event).
  // Do not use `input`: print/RPC/ACP bypass it and queued input is not yet executing.
  on("before_agent_start", async (e, c) => {
    if (e.reason === "revive") return;
    await configure(c);
    preparation = {
      input: config?.hideInputs
        ? undefined
        : typeof e.prompt === "string"
          ? redact(e.prompt).slice(0, 20000)
          : undefined,
    };
  });
  on("agent_start", () => beginExecution());
  for (const name of [
    "before_provider_request",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
  ])
    on(name, (e, c) => {
      // Provider callbacks also belong to advisors; they are not a work-start receipt.
      if (name !== "before_provider_request") beginExecution();
      if (active) event(active, active.root, e, c);
    });
  // session_before_compact is a veto/replace hook: merely registering it disables OMP's
  // asynchronous compaction. Observe completion only, explicitly without measured timing.
  on("session_compact", () => {
    if (active && enabled())
      child(active, active.root, "compaction", "compaction").handle.end();
  });
  on("agent_end", (e) => {
    if (!active || e.willContinue || e.awaitingAsyncWork) return;
    active.terminal = true;
    finish(active);
  });
  const listen = (channel: string, fn: (data: any) => void) => {
    const off = api.events.on(channel, (data) => {
      void safe(() => {
        if (!stopped) fn(data);
      });
    });
    if (off) unsubscribers.push(off);
  };
  // `before_subagent_spawn` is a routing/preflight hook, not a child-start receipt: it
  // has no finalized child ID, and another extension may reject the spawn. The native
  // lifecycle frame below propagates the actual child and spawning-tool identity.
  listen("task:subagent:lifecycle", (data) => {
    if (!validId(data?.id) || !enabled()) return;
    if (data.status === "started") {
      // An identity can execute many wake turns. Only an outstanding execution is a duplicate.
      if (childOwners.has(data.id) || !validId(data.parentToolCallId)) return;
      const previous = retained.get(data.id);
      const ancestry =
        previous?.parentToolCallId === data.parentToolCallId
          ? previous
          : undefined;
      const open = (w: Work, parentId?: string) => {
        const id = `agent/${data.id}/${randomUUID()}`;
        const s = scope(
          id,
          w.turn.child({
            id,
            kind: "agent",
            parentId,
            name: data.agent ?? "subagent",
          }),
        );
        w.children.set(id, s);
        w.pendingChildren.add(data.id);
        childOwners.set(data.id, { work: w, scope: s });
      };
      // Locate the authoritative spawning tool, never the current prompt's most recent tool.
      for (const w of works) {
        if (w.children.size >= 128) continue;
        for (const parent of [w.root, ...w.children.values()]) {
          const tool = parent.tools.get(data.parentToolCallId);
          if (!tool?.handle.context) continue;
          if (
            ancestry &&
            (tool.handle.context.traceId !== ancestry.context.traceId ||
              tool.handle.context.spanId !== ancestry.context.spanId)
          )
            continue;
          retained.delete(data.id);
          retained.set(data.id, {
            parentToolCallId: data.parentToolCallId,
            context: tool.handle.context,
          });
          if (retained.size > 128)
            retained.delete(retained.keys().next().value!);
          open(w, tool.id);
          return;
        }
      }
      if (ancestry && works.size < 16) {
        // The original root is finalized: a linked continuation, not a child of an unrelated
        // subsequent prompt. OTLP links retain the verified spawning trace and tool span.
        const w: Work = {
          turn: tracer!.startTurn({
            sessionId: session,
            turnId: randomUUID(),
            usagePlan: "attempt",
            continuationOf: ancestry.context,
          }),
          root: scope(),
          children: new Map(),
          pendingChildren: new Set(),
          terminal: true,
          ended: false,
        };
        works.add(w);
        open(w);
      }
    } else {
      const owner = childOwners.get(data.id),
        w = owner?.work,
        s = owner?.scope;
      if (!w || !s || !w.pendingChildren.has(data.id)) return;
      s.handle?.end({
        output: s.output,
        outcome:
          data.status === "completed"
            ? s.outcome
            : data.status === "aborted"
              ? "interrupted"
              : "error",
      });
      w.pendingChildren.delete(data.id);
      childOwners.delete(data.id);
      finish(w);
    }
  });
  listen("task:subagent:event", (data) => {
    const owner = childOwners.get(data?.id);
    if (owner && data.event) event(owner.work, owner.scope, data.event);
  });
  on("session_shutdown", async () => {
    stopped = true;
    preparation = undefined;
    for (const w of works) finish(w, "incomplete");
    for (const off of unsubscribers) off();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        tracer?.shutdown(),
        new Promise<void>((r) => {
          timer = setTimeout(r, 1400);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  });
}
