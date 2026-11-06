import { createHash, randomBytes } from "node:crypto";
import {
  ROOT_CONTEXT,
  SpanStatusCode,
  trace,
  type Span,
  type SpanContext,
  type TimeInput,
} from "@opentelemetry/api";
import { FITracer, FITracerProvider, ProjectType } from "@traceai/fi-core";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import {
  destinationId,
  TRANSPORT_CONTRACT,
  type ResolvedConfig,
} from "./config.js";
import { BoundedSpanProcessor, collectionEnabled } from "./delivery.js";
import {
  sessionId as stableSessionId,
  workId,
  spanId,
  traceId,
  opaqueId,
} from "./identity.js";
import { normalizeUsage, type Counters, type UsagePlan } from "./usage.js";
import { redact } from "./privacy.js";
import { VERSION } from "./version.js";
import type { DeliveryResult } from "./validation.js";

export type TurnOutcome = "success" | "error" | "interrupted" | "incomplete";
export type Fidelity =
  | "native-request"
  | "reconstructed-message"
  | "aggregate-turn"
  | "unknown";
export interface UsageInput extends Counters {
  cacheInInput: boolean;
  reasoningInOutput: boolean;
  costUsd?: number;
}
export interface EndInput {
  output?: string;
  outcome?: TurnOutcome;
  usage?: UsageInput;
  endTime?: TimeInput;
}
export interface TurnInput {
  sessionId: string;
  turnId: string;
  input?: string;
  startTime?: TimeInput;
  usagePlan?: UsagePlan;
}
export interface ChildInput {
  id: string;
  kind: "llm" | "tool" | "agent" | "compaction";
  parentId?: string;
  name?: string;
  input?: string;
  model?: string;
  provider?: string;
  startTime?: TimeInput;
  fidelity?: Fidelity;
}
export interface ChildHandle {
  end(value?: EndInput): void;
  context?: SpanContext;
}
export interface TurnHandle extends ChildHandle {
  child(value: ChildInput): ChildHandle;
}
export interface TurnTracer {
  startTurn(value: TurnInput): TurnHandle;
  flush(): Promise<void>;
  shutdown(): Promise<void>;
  readonly state: BoundedSpanProcessor["state"];
}
const noopChild: ChildHandle = { end() {} };
const noopTurn: TurnHandle = {
  end() {},
  child() {
    return noopChild;
  },
};
const label = (value: string | undefined): string | undefined =>
  value && /^[a-zA-Z0-9_.:/-]{1,128}$/.test(value) && !value.includes("://")
    ? value
    : undefined;
function content(
  value: string | undefined,
  credentials?: ResolvedConfig["credentials"],
): string | undefined {
  if (typeof value !== "string") return undefined;
  let bounded = value.slice(0, 20000);
  for (const key of [credentials?.apiKey, credentials?.secretKey])
    if (key) bounded = bounded.split(key).join("[REDACTED]");
  return redact(bounded).slice(0, 20000);
}

/** A private FI provider: no global tracer/context registration, host/process detectors or model gateway. */
export function createTurnTracer(options: {
  config: ResolvedConfig;
  identityKey?: Uint8Array;
  onDelivery?: (result: DeliveryResult) => void;
}): TurnTracer {
  const config: ResolvedConfig = {
    ...options.config,
    paths: { ...options.config.paths },
    tags: [...options.config.tags],
    credentials: options.config.credentials
      ? { ...options.config.credentials }
      : undefined,
    identityKey: options.config.identityKey,
  };
  const processor = new BoundedSpanProcessor(config, options.onDelivery);
  const key =
    options.identityKey ??
    (config.identityKey
      ? Buffer.from(config.identityKey, "hex")
      : config.credentials
        ? createHash("sha256")
            .update(
              JSON.stringify([
                "fi-agent-identity",
                config.credentials.apiKey,
                config.credentials.secretKey,
              ]),
            )
            .digest()
        : randomBytes(32));
  const namespace = `${config.agent}:${destinationId(config.endpoint, config.projectName)}`;
  let nextTrace = "",
    nextSpan = "";
  const provider = new FITracerProvider({
    resource: resourceFromAttributes({
      "service.name": `futureagi-agent-${config.agent}`,
      project_name: config.projectName,
      project_type: ProjectType.OBSERVE,
    }),
    spanProcessors: [processor],
    verbose: false,
    idGenerator: {
      generateTraceId: () => nextTrace,
      generateSpanId: () => nextSpan,
    },
  });
  const tracer = new FITracer({
    tracer: provider.getTracer("@traceai/agent-core", VERSION),
    traceConfig: {
      hideInputs: config.hideInputs,
      hideOutputs: config.hideOutputs,
      hideErrorDetails: config.hideErrorDetails,
    },
  });
  const active = new Map<string, TurnHandle>();
  const completed = new Set<string>();
  let closed = false;
  const canCollect = (): boolean => !closed && collectionEnabled(config);
  function usage(
    span: Span,
    input: UsageInput | undefined,
    additive: boolean,
  ): void {
    if (!input) return;
    try {
      const raw: Counters = {};
      for (const k of [
        "input",
        "output",
        "cacheRead",
        "cacheWrite",
        "reasoning",
      ] as const)
        if (input[k] !== undefined) raw[k] = input[k];
      const counters = normalizeUsage(raw, input);
      const keys: Record<keyof (Counters & { total?: number }), string> = {
        input: SC.LLM_TOKEN_COUNT_PROMPT,
        output: SC.LLM_TOKEN_COUNT_COMPLETION,
        total: SC.LLM_TOKEN_COUNT_TOTAL,
        cacheRead: SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
        cacheWrite: SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE,
        reasoning: SC.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
      };
      for (const [k, value] of Object.entries(counters))
        if (value !== undefined)
          span.setAttribute(
            additive ? keys[k as keyof typeof keys] : `fi.agent.summary.${k}`,
            value,
          );
      span.setAttribute("fi.agent.usage.additive", additive);
      span.setAttribute("fi.agent.usage.cache_in_input", input.cacheInInput);
      span.setAttribute(
        "fi.agent.usage.reasoning_in_output",
        input.reasoningInOutput,
      );
      if (
        input.costUsd !== undefined &&
        Number.isFinite(input.costUsd) &&
        input.costUsd >= 0
      ) {
        span.setAttribute(
          additive ? "gen_ai.usage.cost" : "fi.agent.summary.cost_usd",
          input.costUsd,
        );
        span.setAttribute("fi.agent.cost.source", "reported");
      }
    } catch {
      span.setAttribute("fi.agent.gap", "UNRESOLVED_USAGE");
    }
  }
  function finish(span: Span, value: EndInput, additive: boolean): void {
    const out =
      !config.hideOutputs &&
      !(config.hideErrorDetails && value.outcome === "error")
        ? content(value.output, config.credentials)
        : undefined;
    if (out !== undefined && canCollect())
      span.setAttribute(SC.OUTPUT_VALUE, out);
    if (canCollect()) usage(span, value.usage, additive);
    const outcome = value.outcome ?? "success";
    span.setAttribute("fi.agent.outcome", outcome);
    span.setStatus({
      code:
        outcome === "success"
          ? SpanStatusCode.OK
          : outcome === "error"
            ? SpanStatusCode.ERROR
            : SpanStatusCode.UNSET,
    });
    span.end(value.endTime);
  }
  function startTurn(value: TurnInput): TurnHandle {
    if (!canCollect() || active.size >= 128) return noopTurn;
    try {
      const turnWork = workId(
        key,
        namespace,
        "turn",
        value.sessionId,
        value.turnId,
      );
      if (completed.has(turnWork)) return noopTurn;
      if (active.has(turnWork)) return active.get(turnWork)!;
      nextTrace = traceId(turnWork);
      nextSpan = spanId(turnWork);
      const attributes: Record<string, string> = {
        [SC.FI_SPAN_KIND]: "AGENT",
        "session.id": stableSessionId(key, namespace, value.sessionId),
        "fi.agent.transport": TRANSPORT_CONTRACT,
        "fi.agent.fidelity": "aggregate-turn",
        "fi.agent.timing":
          value.startTime === undefined ? "observed-event" : "source",
        "fi.agent.usage.plan": value.usagePlan ?? "request",
      };
      attributes["user.id"] =
        config.userId ?? opaqueId(key, "local-user", namespace);
      if (config.tags.length)
        attributes["fi.agent.tags"] = JSON.stringify(config.tags);
      if (config.environment)
        attributes["deployment.environment.name"] = config.environment;
      const input = !config.hideInputs
        ? content(value.input, config.credentials)
        : undefined;
      if (input !== undefined) attributes[SC.INPUT_VALUE] = input;
      const root = tracer.startSpan(
        config.agent,
        { attributes, startTime: value.startTime },
        ROOT_CONTEXT,
      );
      const children = new Map<string, { span: Span; done: boolean }>();
      let ended = false;
      const turn: TurnHandle = {
        context: root.spanContext(),
        child(child): ChildHandle {
          if (
            ended ||
            !canCollect() ||
            children.size >= 512 ||
            children.has(child.id)
          )
            return noopChild;
          try {
            const parent = child.parentId
              ? children.get(child.parentId)?.span
              : root;
            if (!parent) {
              root.setAttribute("fi.agent.gap", "MISSING_PARENT");
              return noopChild;
            }
            // Only a verified model request/attempt is an LLM span, never reconstructed prompt/answer text.
            const isModel =
              child.kind === "llm" && child.fidelity === "native-request";
            const kind = isModel
              ? "LLM"
              : child.kind === "tool"
                ? "TOOL"
                : "AGENT";
            const work = workId(
              key,
              namespace,
              child.kind === "llm"
                ? value.usagePlan === "attempt"
                  ? "attempt"
                  : "request"
                : child.kind === "tool"
                  ? "tool"
                  : child.kind === "compaction"
                    ? "compaction"
                    : "activity",
              value.sessionId,
              value.turnId,
              child.id,
            );
            nextSpan = spanId(work);
            const attrs: Record<string, string> = {
              [SC.FI_SPAN_KIND]: kind,
              "fi.agent.fidelity": child.fidelity ?? "unknown",
              "fi.agent.timing":
                child.startTime === undefined ? "observed-event" : "source",
            };
            if (child.kind === "compaction")
              attrs["fi.agent.event"] = "compaction";
            if (label(child.model)) attrs[SC.LLM_MODEL_NAME] = child.model!;
            if (label(child.provider)) attrs[SC.LLM_PROVIDER] = child.provider!;
            if (child.kind === "tool") attrs["fi.agent.source_call_id"] = work;
            const text = !config.hideInputs
              ? content(child.input, config.credentials)
              : undefined;
            if (text !== undefined) attrs[SC.INPUT_VALUE] = text;
            const span = tracer.startSpan(
              label(child.name) ?? child.kind,
              { attributes: attrs, startTime: child.startTime },
              trace.setSpan(ROOT_CONTEXT, parent),
            );
            const state = { span, done: false };
            children.set(child.id, state);
            return {
              context: span.spanContext(),
              end(end = {}) {
                if (state.done) return;
                state.done = true;
                try {
                  finish(
                    span,
                    end,
                    isModel &&
                      (value.usagePlan ?? "request") !== "turn" &&
                      value.usagePlan !== "unknown",
                  );
                } catch {
                  /* fail open */
                }
              },
            };
          } catch {
            return noopChild;
          }
        },
        end(end = {}): void {
          if (ended) return;
          ended = true;
          try {
            let incomplete = false;
            for (const item of children.values())
              if (!item.done) {
                incomplete = true;
                item.done = true;
                finish(
                  item.span,
                  { outcome: "incomplete", endTime: end.endTime },
                  false,
                );
              }
            if (incomplete)
              root.setAttribute("fi.agent.gap", "MISSING_CHILD_STOP");
            finish(
              root,
              {
                ...end,
                outcome:
                  incomplete && (!end.outcome || end.outcome === "success")
                    ? "incomplete"
                    : end.outcome,
              },
              false,
            );
          } catch {
            /* fail open */
          }
          active.delete(turnWork);
          completed.add(turnWork);
          if (completed.size > 4096)
            completed.delete(completed.values().next().value!);
        },
      };
      active.set(turnWork, turn);
      return turn;
    } catch {
      return noopTurn;
    }
  }
  return {
    startTurn,
    state: processor.state,
    async flush() {
      try {
        await provider.forceFlush();
      } catch {
        /* fail open */
      }
    },
    async shutdown() {
      for (const turn of active.values()) turn.end({ outcome: "incomplete" });
      closed = true;
      try {
        await provider.shutdown();
      } catch {
        /* fail open */
      }
    },
  };
}
