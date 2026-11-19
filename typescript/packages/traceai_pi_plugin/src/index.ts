import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import {
  createTurnTracer,
  resolveConfig,
  inspectStatus,
  collectionEnabled,
  isSensitiveField,
  redact,
  type TurnTracer,
  type TurnHandle,
  type ChildHandle,
  type ResolvedConfig,
  type TurnOutcome,
  type UsageInput,
} from "@traceai/agent-core";
import { messageText, toolResultText } from "@traceai/pi-coding-agent/content";
import { SUBAGENT_CHANNEL, type SubagentEvent } from "./subagent";

// JSON payloads may contain images or arbitrary metadata. Export only bounded textual
// provider input, never authorization headers, binary images, or the entire event object.
export function boundedJson(value: unknown): string | undefined {
  try {
    let budget = 20000;
    const visit = (v: unknown, depth: number): unknown => {
      if (budget <= 0 || depth > 12) return "[truncated]";
      if (typeof v === "string") {
        const s = redact(v).slice(0, budget);
        budget -= s.length;
        return s;
      }
      if (v === null || typeof v === "boolean" || typeof v === "number")
        return v;
      if (Array.isArray(v))
        return v.slice(0, 128).map((x) => visit(x, depth + 1));
      if (v && typeof v === "object") {
        const o = v as Record<string, unknown>;
        if (
          ["image", "image_url", "input_image", "base64"].includes(
            String(o.type),
          )
        )
          return "[image]";
        return Object.fromEntries(
          Object.entries(o)
            .slice(0, 128)
            .filter(
              ([k]) =>
                !isSensitiveField(k) &&
                !/^(headers|data|inlineData|fileData|signature|thinkingSignature)$/i.test(
                  k,
                ),
            )
            .map(([k, x]) => [k.slice(0, 128), visit(x, depth + 1)]),
        );
      }
      return undefined;
    };
    return JSON.stringify(visit(value, 0))?.slice(0, 20000);
  } catch {
    return undefined;
  }
}
export function piUsage(value: unknown): UsageInput | undefined {
  if (!value || typeof value !== "object") return undefined;
  const u = value as Record<string, any>;
  const result: UsageInput = {
    cacheInInput: false,
    reasoningInOutput: true,
    costSource: "host-price-table",
  };
  let any = false;
  for (const k of [
    "input",
    "output",
    "cacheRead",
    "cacheWrite",
    "reasoning",
  ] as const) {
    if (typeof u[k] === "number" && Number.isFinite(u[k]) && u[k] >= 0) {
      result[k] = u[k];
      any = true;
    }
  }
  // Pi's pricing comes from the model catalog, not an invoice. Zero also means unpriced.
  if (
    typeof u.cost?.total === "number" &&
    Number.isFinite(u.cost.total) &&
    u.cost.total > 0
  ) {
    result.costUsd = u.cost.total;
    any = true;
  }
  return any ? result : undefined;
}
const validId = (v: unknown): v is string =>
  typeof v === "string" && v.length > 0 && v.length < 256;
const uiAllowed = (ctx: ExtensionContext) => ctx.hasUI && ctx.mode === "tui";

/** Pinned native API: Pi 1.5.0. All handlers are observation-only and return undefined. */
export default function futureAgi(pi: ExtensionAPI): void {
  // Each extension receives a different wrapper around the shared host bus.
  // Pi 1.5 invokes bus listeners synchronously; a local claim handshake therefore
  // coordinates even two separate installed copies without process-global state.
  const ownershipChannel = "futureagi:pi-plugin:owner:v1";
  const claim = { owned: false };
  pi.events.emit(ownershipChannel, claim);
  if (claim.owned) return;
  const releaseOwnership = pi.events.on(ownershipChannel, (data) => {
    if (data && typeof data === "object" && "owned" in data)
      (data as { owned: boolean }).owned = true;
  });
  let tracer: TurnTracer | undefined, config: ResolvedConfig | undefined;
  let turn: TurnHandle | undefined,
    session = "";
  let request:
    | { handle: ChildHandle; start: number; ttft?: number }
    | undefined;
  let compaction: ChildHandle | undefined,
    sequence = 0;
  let outcome: TurnOutcome = "success",
    output: string | undefined;
  const coalesced: TurnHandle[] = [];
  const observedUsers = new Set<string>();
  const tools = new Map<string, ChildHandle>();
  const children = new Map<string, ChildHandle>();
  let lastTraceId: string | undefined;
  let historySession: string | undefined;
  const failedToolCalls = new Set<string>();
  let errorHistory = false;
  function rememberError(message: {
    role: string;
    [key: string]: unknown;
  }): void {
    if (message.role === "assistant" && message.stopReason === "error")
      errorHistory = true;
    if (message.role === "toolResult" && message.isError === true) {
      errorHistory = true;
      if (validId(message.toolCallId) && failedToolCalls.size < 4096)
        failedToolCalls.add(message.toolCallId);
    }
  }

  const safe =
    <A extends unknown[]>(fn: (...args: A) => void | Promise<void>) =>
    async (...args: A): Promise<void> => {
      try {
        await fn(...args);
      } catch {
        /* Never affect Pi output, exit status or actionable hook decisions. */
      }
    };
  const enabled = () => !!config && !!tracer && collectionEnabled(config);
  function status(ctx: ExtensionContext): void {
    if (uiAllowed(ctx))
      ctx.ui.setStatus(
        "futureagi",
        enabled() ? "Future AGI: tracing" : "Future AGI: off",
      );
  }
  async function configure(ctx: ExtensionContext): Promise<void> {
    if (turn) return;
    const currentSession = ctx.sessionManager.getSessionId();
    const branch = ctx.sessionManager.getBranch();
    if (historySession !== currentSession) {
      historySession = currentSession;
      failedToolCalls.clear();
      errorHistory = false;
      observedUsers.clear();
      // Resumed history is not a new delivery. Leave only an unsettled trailing
      // user batch eligible for capture (before_provider_request sees persistence).
      let historyEnd = -1;
      for (let i = 0; i < branch.length; i++) {
        const entry = branch[i];
        if (entry.type === "compaction" || (entry.type === "message" && entry.message.role === "assistant")) historyEnd = i;
      }
      for (const entry of branch.slice(Math.max(0, historyEnd - 4095), historyEnd + 1))
        if (entry.type === "message" && entry.message.role === "user") observedUsers.add(entry.id);
    }
    // Persisted history matters on resume too. Unknown/compacted history can
    // contain earlier failures, so prefer omission to replaying hidden details.
    if (branch.length > 4096) errorHistory = true;
    for (const entry of branch.slice(-4096)) {
      if (entry.type === "message")
        rememberError(
          entry.message as unknown as { role: string; [key: string]: unknown },
        );
      if (entry.type === "compaction") errorHistory = true;
    }
    let next: ResolvedConfig;
    try {
      next = await resolveConfig({ agent: "pi", projectDir: ctx.cwd });
    } catch {
      const old = tracer;
      tracer = undefined;
      config = undefined;
      void old?.shutdown();
      status(ctx);
      return;
    }
    const unchanged =
      config &&
      JSON.stringify(config) === JSON.stringify(next) &&
      config.credentials?.apiKey === next.credentials?.apiKey &&
      config.credentials?.secretKey === next.credentials?.secretKey;
    if (!unchanged) {
      // Retired batches keep their original destination. Never wait for a remote
      // export on the model request path, including during a config change.
      const old = tracer;
      config = next;
      tracer = next.enabled ? createTurnTracer({ config: next }) : undefined;
      void old?.shutdown();
    }
    status(ctx);
  }
  function finish(result: TurnOutcome = outcome): void {
    turn?.end({ outcome: result, output });
    for (const prompt of coalesced) prompt.end({ outcome: result, output });
    coalesced.length = 0;
    turn = undefined;
    request = undefined;
    compaction = undefined;
    tools.clear();
    children.clear();
    output = undefined;
    outcome = "success";
    sequence = 0;
    void tracer?.flush();
  }
  function ensureTurn(ctx: ExtensionContext): boolean {
    if (!enabled()) return false;
    // User message_end precedes persistence. Provider callbacks and assistant
    // message_end both see the persisted user entry, including routing failures
    // that never reach a provider. Use that entry's id, never a synthetic turn.
    const entries = ctx.sessionManager.getBranch().slice(-1024).filter(
      (e) => e.type === "message" && e.message.role === "user" && !observedUsers.has(e.id),
    );
    if (!entries.length) return !!turn;
    if (turn) finish(); // A delivered follow-up/steering batch starts new prompt roots.
    session = ctx.sessionManager.getSessionId();
    const entry = entries.at(-1)!;
    if (entry.type !== "message" || entry.message.role !== "user") return false;
    turn = tracer!.startTurn({
      sessionId: session,
      turnId: entry.id,
      input: config!.hideInputs ? undefined : messageText(entry.message.content),
      usagePlan: "attempt",
    });
    // Pi can send several queued prompts in one model request. Each source user
    // entry keeps its identity; only the final root owns the shared model/tool
    // work. OTel links make the other roots navigable without duplicating usage.
    for (const earlier of entries.slice(0, -1)) {
      if (earlier.type !== "message" || earlier.message.role !== "user") continue;
      coalesced.push(tracer!.startTurn({
        sessionId: session,
        turnId: earlier.id,
        input: config!.hideInputs ? undefined : messageText(earlier.message.content),
        usagePlan: "unknown",
        continuationOf: turn.context,
        gaps: ["COALESCED_PROMPT"],
      }));
    }
    for (const entry of entries) observedUsers.add(entry.id);
    while (observedUsers.size > 4096) observedUsers.delete(observedUsers.values().next().value!);
    lastTraceId = turn.context?.traceId;
    return true;
  }
  pi.on(
    "session_start",
    safe(async (_event, ctx) => {
      await configure(ctx);
    }),
  );
  pi.on(
    "before_agent_start",
    safe(async (_event, ctx) => {
      await configure(ctx);
    }),
  );
  pi.on(
    "before_provider_request",
    safe((event, ctx) => {
      if (!ensureTurn(ctx)) return;
      // Some providers invoke this again for an HTTP retry. An unfinished attempt is
      // explicitly incomplete; never assign the eventual message's usage to every retry.
      request?.handle.end({ outcome: "incomplete" });
      const payload = event.payload as Record<string, unknown> | undefined;
      const knownPayload =
        payload &&
        typeof payload === "object" &&
        [payload.messages, payload.input, payload.contents].some(
          (value) => Array.isArray(value) || typeof value === "string",
        );
      const input =
        // Provider payloads mix prompts with replayed outputs and summaries in
        // provider-specific shapes. Omit that mixed input under output privacy or
        // after any failure under error privacy; the root still has the user input.
        !config!.hideInputs &&
        !config!.hideOutputs &&
        !(
          config!.hideErrorDetails &&
          (errorHistory || failedToolCalls.size > 0)
        ) &&
        knownPayload
          ? boundedJson({
              messages: payload.messages,
              input: payload.input,
              system: payload.system,
              tools: payload.tools,
              contents: payload.contents,
              // Google's config also contains transport/options. Select only
              // prompt fields; boundedJson still strips credentials and images.
              ...(payload.config && typeof payload.config === "object"
                ? {
                    systemInstruction: (
                      payload.config as Record<string, unknown>
                    ).systemInstruction,
                    tools:
                      (payload.config as Record<string, unknown>).tools ??
                      payload.tools,
                  }
                : {}),
            })
          : undefined;
      request = {
        start: performance.now(),
        handle: turn!.child({
          id: `request:${++sequence}`,
          kind: "llm",
          fidelity: "native-request",
          name: "pi.generation",
          gap: knownPayload ? undefined : "UNKNOWN_PROVIDER_PAYLOAD",
          model: ctx.model?.id,
          provider: ctx.model?.provider,
          input,
        }),
      };
    }),
  );
  pi.on(
    "message_update",
    safe((event) => {
      if (
        request &&
        request.ttft === undefined &&
        ["text_delta", "thinking_delta"].includes(
          event.assistantMessageEvent.type,
        )
      )
        request.ttft = performance.now() - request.start;
    }),
  );
  pi.on(
    "message_end",
    safe((event, ctx) => {
      rememberError(
        event.message as unknown as { role: string; [key: string]: unknown },
      );
      if (event.message.role !== "assistant" || !ensureTurn(ctx)) return;
      const m = event.message;
      const result: TurnOutcome =
        m.stopReason === "aborted" ||
        (m.stopReason === "error" && ctx.signal?.aborted)
          ? "interrupted"
          : m.stopReason === "error"
            ? "error"
            : "success";
      outcome = result;
      const text =
        config!.hideOutputs || (config!.hideErrorDetails && result === "error")
          ? undefined
          : result === "error"
            ? m.errorMessage?.slice(0, 20000)
            : boundedJson(
                m.content.map((part) =>
                  part.type === "toolCall" && config!.hideInputs
                    ? { type: part.type, id: part.id, name: part.name }
                    : part,
                ),
              );
      // A message without a request callback is not proof of a model request.
      request?.handle.end({
        outcome: result,
        model: m.model,
        provider: m.provider,
        output: text,
        usage: piUsage(m.usage),
        timeToFirstTokenMs: request.ttft,
      });
      request = undefined;
      output = text;
    }),
  );
  pi.on(
    "tool_execution_start",
    safe((event) => {
      if (!turn || !enabled() || tools.size >= 256) return;
      const parentId =
        event.parentToolCallId && tools.has(event.parentToolCallId)
          ? `tool:${event.parentToolCallId}`
          : undefined;
      tools.set(
        event.toolCallId,
        turn.child({
          id: `tool:${event.toolCallId}`,
          kind: "tool",
          parentId,
          name: event.toolName,
          toolCallId: event.toolCallId,
          input: config!.hideInputs ? undefined : boundedJson(event.args),
        }),
      );
    }),
  );
  pi.on(
    "tool_execution_end",
    safe((event) => {
      if (event.isError) {
        errorHistory = true;
        if (failedToolCalls.size < 4096) failedToolCalls.add(event.toolCallId);
      }
      tools.get(event.toolCallId)?.end({
        outcome: event.isError ? "error" : "success",
        output:
          config?.hideOutputs || (event.isError && config?.hideErrorDetails)
            ? undefined
            : redact(toolResultText(event.result)).slice(0, 20000),
      });
      tools.delete(event.toolCallId);
    }),
  );
  pi.on(
    "session_before_compact",
    safe(() => {
      if (turn && enabled())
        compaction = turn.child({
          id: `compaction:${++sequence}`,
          kind: "compaction",
          name: "pi.compaction",
          fidelity: "aggregate-turn",
          usageContribution: "exclusive",
        });
    }),
  );
  pi.on(
    "session_compact",
    safe((event) => {
      compaction?.end({
        usage: piUsage(event.compactionEntry.usage),
        output:
          config?.hideOutputs || (config?.hideErrorDetails && errorHistory)
            ? undefined
            : redact(event.compactionEntry.summary).slice(0, 20000),
      });
      compaction = undefined;
    }),
  );
  pi.on(
    "session_compact_failed",
    safe((event) => {
      compaction?.end({ outcome: event.aborted ? "interrupted" : "error" });
      compaction = undefined;
    }),
  );
  pi.on(
    "agent_settled",
    safe((event, ctx) => {
      // An abort may settle after the prompt was persisted but before a request was sent.
      if (!turn && event.aborted) ensureTurn(ctx);
      finish(event.aborted ? "interrupted" : outcome);
      status(ctx);
    }),
  );
  pi.on(
    "session_shutdown",
    safe(async (_event, ctx) => {
      if (turn) finish(ctx.signal?.aborted ? "interrupted" : "incomplete");
      await tracer?.shutdown();
      tracer = undefined;
      unsubscribe();
      releaseOwnership();
    }),
  );
  const unsubscribe = pi.events.on(SUBAGENT_CHANNEL, (data) => {
    try {
      if (!turn || !enabled() || !data || typeof data !== "object") return;
      const event = data as SubagentEvent;
      if (!validId(event.id)) return;
      const id = `subagent:${event.id}`;
      if (
        event.type === "start" &&
        tools.has(event.parentToolCallId) &&
        !children.has(id) &&
        children.size < 128
      ) {
        children.set(
          id,
          turn.child({
            id,
            kind: "agent",
            parentId: `tool:${event.parentToolCallId}`,
            name: event.name,
            input: config!.hideInputs ? undefined : event.input,
          }),
        );
      } else if (
        event.type === "request" &&
        children.has(`subagent:${event.parentId}`) &&
        !children.has(id) &&
        children.size < 128
      ) {
        children.set(
          id,
          turn.child({
            id,
            kind: "llm",
            parentId: `subagent:${event.parentId}`,
            fidelity: "native-request",
            model: event.model,
            provider: event.provider,
            input: config!.hideInputs ? undefined : event.input,
          }),
        );
      } else if (event.type === "end") {
        children.get(id)?.end(event.result);
        children.delete(id);
      }
    } catch {
      /* fail open; a third-party extension cannot change the transport */
    }
  });
  pi.registerCommand("futureagi", {
    description: "Show local tracing status (no credentials or model call)",
    handler: safe(async (_args, ctx) => {
      // Pi reports hasUI=true in RPC as well: interactive mode is an additional guard.
      if (!uiAllowed(ctx)) return;
      const s = await inspectStatus({ agent: "pi", projectDir: ctx.cwd });
      ctx.ui.notify(
        `Future AGI: ${s.enabled ? "enabled" : "off"} · ${s.projectName}\n${s.destination}\nExports: ${s.delivery.exported}; dropped: ${s.delivery.dropped}\nLast accepted: ${s.delivery.lastAcceptedAt ?? "none"}${lastTraceId ? `\nLast trace ID: ${lastTraceId}` : ""}\nOpen Observe: https://app.futureagi.com\nSetup/status: npx @traceai/agents status pi --check`,
        "info",
      );
    }),
  });
}
