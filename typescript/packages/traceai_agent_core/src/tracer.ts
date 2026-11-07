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
import { contentHash } from "./files.js";
import { VERSION } from "./version.js";
import type { DeliveryResult } from "./validation.js";

export type TurnOutcome = "success" | "error" | "interrupted" | "incomplete";
export type EventTiming =
  | "source"
  | "observed-event"
  | "estimated"
  | "reconstructed"
  | "unknown";
export type Fidelity =
  | "native-request"
  | "recorded-request"
  | "reconstructed-message"
  | "aggregate-turn"
  | "unknown";
export interface UsageInput extends Counters {
  cacheInInput: boolean;
  reasoningInOutput: boolean;
  costUsd?: number;
  costSource?: "reported" | "estimated" | "host-price-table" | "host-estimate";
  costEstimator?: string;
}
export interface EndInput {
  model?: string;
  provider?: string;
  /** Native measured TTFT in milliseconds, never a reconstructed duration. */
  timeToFirstTokenMs?: number;
  output?: string;
  outcome?: TurnOutcome;
  usage?: UsageInput;
  endTime?: TimeInput;
  /** Bounded diagnostic code, never source content. */
  gap?: string;
}
export interface TurnInput {
  /** Observed host surface, not inferred from the setup command. */
  surface?: "cli" | "app";
  unit?: "turn" | "run" | "stop-summary";
  gaps?: string[];
  deferRoot?: boolean;
  checkpoint?: readonly string[];
  continuationOf?: SpanContext;
  sessionId: string;
  turnId: string;
  input?: string;
  startTime?: TimeInput;
  timing?: EventTiming;
  usagePlan?: UsagePlan;
}
export interface ChildInput {
  toolCallId?: string;
  contentSource?: "native" | "reconstructed-rollout";
  sourceTiming?: "message-interval" | "hook-interval";
  usageContribution?: "exclusive";
  gap?: string;
  id: string;
  kind: "llm" | "generation" | "tool" | "agent" | "compaction";
  parentId?: string;
  name?: string;
  input?: string;
  model?: string;
  provider?: string;
  startTime?: TimeInput;
  timing?: EventTiming;
  fidelity?: Fidelity;
}
export interface ChildHandle {
  end(value?: EndInput): void;
  context?: SpanContext;
}
export interface TurnEventInput {
  name: string;
  input?: string;
  time?: TimeInput;
}
export interface TurnHandle extends ChildHandle {
  event(value: TurnEventInput): void;
  gap(reason: string): void;
  child(value: ChildInput): ChildHandle;
}
export interface TurnTracer {
  startTurn(value: TurnInput): TurnHandle;
  flush(): Promise<void>;
  shutdown(): Promise<void>;
  /** Discard a stale/privacy-revoked route without finalizing or exporting its active content. */
  discard(): Promise<void>;
  readonly state: BoundedSpanProcessor["state"];
}
const noopChild: ChildHandle = { end() {} };
const noopTurn: TurnHandle = {
  end() {},
  event() {},
  gap() {},
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
  // redact() inspects complete structured content within its processing bound.
  // Display truncation must never remove JSON structure before that inspection.
  let clean = redact(value);
  for (const key of [credentials?.apiKey, credentials?.secretKey])
    if (key) clean = clean.split(key).join("[REDACTED]");
  return clean.slice(0, 20000);
}

/** A private FI provider: no global tracer/context registration, host/process detectors or model gateway. */
export function createTurnTracer(options: {
  config: ResolvedConfig;
  identityKey?: Uint8Array;
  onDelivery?: (result: DeliveryResult) => void;
  /** Revalidate a durable snapshot's privacy/routing policy immediately before each HTTP batch. */
  beforeExport?: () => Promise<boolean>;
  protocol?: "http/json" | "http/protobuf";
}): TurnTracer {
  const config: ResolvedConfig = {
    ...options.config,
    revalidate: options.config.revalidate,
    paths: { ...options.config.paths },
    tags: [...options.config.tags],
    credentials: options.config.credentials
      ? { ...options.config.credentials }
      : undefined,
    identityKey: options.config.identityKey,
  };
  const processor = new BoundedSpanProcessor(
    config,
    options.onDelivery,
    options.beforeExport,
    options.protocol,
  );
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
      const summaryKeys: typeof keys = {
        input: SC.FI_AGENT_SUMMARY_INPUT,
        output: SC.FI_AGENT_SUMMARY_OUTPUT,
        total: SC.FI_AGENT_SUMMARY_TOTAL,
        cacheRead: SC.FI_AGENT_SUMMARY_CACHE_READ,
        cacheWrite: SC.FI_AGENT_SUMMARY_CACHE_WRITE,
        reasoning: SC.FI_AGENT_SUMMARY_REASONING,
      };
      for (const [k, value] of Object.entries(counters))
        if (value !== undefined)
          span.setAttribute(
            (additive ? keys : summaryKeys)[k as keyof typeof keys],
            value,
          );
      span.setAttribute(SC.FI_AGENT_USAGE_ADDITIVE, additive);
      span.setAttribute(
        SC.FI_USAGE_CONTRIBUTION,
        additive ? "additive" : "none",
      );
      span.setAttribute(SC.FI_AGENT_USAGE_CACHE_IN_INPUT, input.cacheInInput);
      span.setAttribute(
        SC.FI_AGENT_USAGE_REASONING_IN_OUTPUT,
        input.reasoningInOutput,
      );
      if (
        input.costUsd !== undefined &&
        Number.isFinite(input.costUsd) &&
        input.costUsd >= 0
      ) {
        span.setAttribute(
          additive ? SC.GEN_AI_COST_TOTAL : SC.FI_AGENT_SUMMARY_COST_USD,
          input.costUsd,
        );
        span.setAttribute(
          SC.FI_AGENT_COST_SOURCE,
          input.costSource ?? "reported",
        );
        if (label(input.costEstimator))
          span.setAttribute(SC.FI_AGENT_COST_ESTIMATOR, input.costEstimator!);
        if (additive)
          span.setAttribute(
            SC.FI_COST_KIND,
            !input.costSource || input.costSource === "reported"
              ? "provider_reported"
              : "estimated",
          );
      } else if (additive) span.setAttribute(SC.FI_COST_KIND, "unknown");
    } catch {
      span.setAttribute(SC.FI_AGENT_GAP, "UNRESOLVED_USAGE");
    }
  }
  function endOutcome(value: EndInput, nativeTool = false): TurnOutcome {
    if (value.outcome && value.outcome !== "success") return value.outcome;
    // The native MCP tool-result contract uses an explicit isError flag.
    // Never interpret generated JSON status/success/type fields as lifecycle signals.
    if (nativeTool && typeof value.output === "string" && value.output.length <= 256 * 1024) {
      try {
        const result = JSON.parse(value.output);
        if (result && (result.isError === true || result.is_error === true)) return "error";
      } catch { /* ordinary tool text */ }
    }
    return value.outcome ?? "success";
  }
  function finish(
    span: Span,
    value: EndInput,
    additive: boolean,
    measuredTiming = false,
  ): void {
    const outcome = endOutcome(value);
    const out =
      !config.hideOutputs && !(config.hideErrorDetails && outcome === "error")
        ? content(value.output, config.credentials)
        : undefined;
    if (out !== undefined && canCollect())
      span.setAttribute(SC.OUTPUT_VALUE, out);
    if (canCollect()) {
      usage(span, value.usage, additive);
      if (
        measuredTiming &&
        value.timeToFirstTokenMs !== undefined &&
        Number.isFinite(value.timeToFirstTokenMs) &&
        value.timeToFirstTokenMs >= 0
      )
        span.setAttribute(
          SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN,
          value.timeToFirstTokenMs,
        );
    }
    if (value.gap && /^[A-Z_]{1,64}$/.test(value.gap))
      span.setAttribute(SC.FI_AGENT_GAP, value.gap);
    span.setAttribute(SC.FI_AGENT_OUTCOME, outcome);
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
      value = {
        ...value,
        checkpoint: value.checkpoint ? [...value.checkpoint] : undefined,
        gaps: value.gaps ? [...value.gaps] : undefined,
      };
      const turnWork = workId(
        key,
        namespace,
        value.unit === "run"
          ? "run"
          : value.unit === "stop-summary"
            ? "activity"
            : "turn",
        value.sessionId,
        value.turnId,
        value.unit === "stop-summary" ? "stop-summary" : undefined,
      );
      if (completed.has(turnWork)) return noopTurn;
      if (active.has(turnWork)) return active.get(turnWork)!;
      nextTrace = traceId(turnWork);
      nextSpan = spanId(turnWork);
      const attributes: Record<string, string> = {
        [SC.FI_SPAN_KIND]: "AGENT",
        "session.id": stableSessionId(key, namespace, value.sessionId),
        [SC.FI_AGENT_TRANSPORT]: TRANSPORT_CONTRACT,
        [SC.FI_AGENT_FIDELITY]:
          value.unit === "run" ? "aggregate-run" : "aggregate-turn",
        "fi.agent.unit": value.unit ?? "turn",
        [SC.FI_AGENT_TIMING]:
          value.timing ??
          (value.startTime === undefined ? "observed-event" : "source"),
        [SC.FI_AGENT_USAGE_PLAN]: value.usagePlan ?? "request",
      };
      if (value.surface === "cli" || value.surface === "app")
        attributes["fi.agent.surface"] = value.surface;
      if (value.gaps?.length)
        attributes["fi.agent.gaps"] = value.gaps
          .filter((v) => /^[A-Z_]{1,80}$/.test(v))
          .slice(0, 16)
          .join(",");
      if (value.continuationOf)
        attributes["fi.agent.continuation"] = "retained-agent";
      attributes["user.id"] =
        config.userId ?? opaqueId(key, "local-user", namespace);
      if (config.tags.length)
        attributes[SC.FI_AGENT_TAGS] = JSON.stringify(config.tags);
      if (config.environment)
        attributes["deployment.environment.name"] = config.environment;
      const input = !config.hideInputs
        ? content(value.input, config.credentials)
        : undefined;
      if (input !== undefined) attributes[SC.INPUT_VALUE] = input;
      const root = tracer.startSpan(
        value.unit === "run" ? `${config.agent}.run` : config.agent,
        {
          attributes,
          startTime: value.startTime,
          links: value.continuationOf
            ? [{ context: value.continuationOf }]
            : undefined,
        },
        ROOT_CONTEXT,
      );
      const checkpoint = new Set(value.checkpoint ?? []);
      const children = new Map<
        string,
        {
          span: Span;
          done: boolean;
          isModel: boolean;
          measuredTiming: boolean;
          skipped: boolean;
          outcome?: TurnOutcome;
          pending?: EndInput;
        }
      >();
      let ended = false,
        eventCount = 0;
      const turn: TurnHandle = {
        context: root.spanContext(),
        gap(reason): void {
          if (!ended && canCollect() && /^[A-Z_]{1,80}$/.test(reason))
            root.setAttribute(SC.FI_AGENT_GAP, reason);
        },
        event(event): void {
          if (
            ended ||
            !canCollect() ||
            eventCount >= 64 ||
            !label(event.name) ||
            [config.credentials?.apiKey, config.credentials?.secretKey].some(
              (key) => key && event.name.includes(key),
            )
          )
            return;
          try {
            const input = !config.hideInputs
              ? content(event.input, config.credentials)
              : undefined;
            root.addEvent(
              event.name,
              input === undefined ? {} : { [SC.INPUT_VALUE]: input },
              event.time,
            );
            eventCount++;
          } catch {
            /* fail open */
          }
        },
        child(child): ChildHandle {
          if (
            ended ||
            !canCollect() ||
            children.size >= 512 ||
            children.has(child.id)
          )
            return noopChild;
          try {
            child = { ...child };
            const parent = child.parentId
              ? children.get(child.parentId)?.span
              : root;
            if (!parent) {
              root.setAttribute(SC.FI_AGENT_GAP, "MISSING_PARENT");
              return noopChild;
            }
            // Generations require a source assistant message, not an aggregate final answer.
            const isModel =
              (child.kind === "llm" &&
                (child.fidelity === "native-request" ||
                  child.fidelity === "recorded-request")) ||
              (child.kind === "generation" &&
                child.fidelity === "reconstructed-message");
            const kind = isModel
              ? "LLM"
              : child.kind === "tool"
                ? "TOOL"
                : "AGENT";
            const work = workId(
              key,
              namespace,
              child.kind === "llm" || child.kind === "generation"
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
              "session.id": stableSessionId(key, namespace, value.sessionId),
              [SC.FI_AGENT_FIDELITY]: child.fidelity ?? "unknown",
              [SC.FI_AGENT_TIMING]:
                child.timing ??
                child.sourceTiming ??
                (child.startTime === undefined ? "observed-event" : "source"),
            };
            if (child.contentSource) {
              attrs[SC.FI_AGENT_CONTENT_SOURCE] = child.contentSource;
              if (child.contentSource === "reconstructed-rollout")
                attrs[SC.FI_AGENT_INPUT_COMPLETENESS] = "partial";
            }
            if (child.gap && /^[A-Z_]{1,80}$/.test(child.gap))
              attrs[SC.FI_AGENT_GAP] = child.gap;
            if (child.kind === "compaction")
              attrs[SC.FI_AGENT_EVENT] = "compaction";
            if (label(child.model)) attrs[SC.LLM_MODEL_NAME] = child.model!;
            if (label(child.provider)) attrs[SC.LLM_PROVIDER] = child.provider!;
            if (child.kind === "tool") {
              attrs[SC.FI_AGENT_SOURCE_CALL_ID] = work;
              attrs[SC.TOOL_NAME] = label(child.name) ?? "tool";
              if (label(child.toolCallId))
                attrs[SC.TOOL_CALL_ID] = child.toolCallId!;
            }
            const text = !config.hideInputs
              ? content(child.input, config.credentials)
              : undefined;
            if (text !== undefined) attrs[SC.INPUT_VALUE] = text;
            const span = tracer.startSpan(
              label(child.name) ?? child.kind,
              { attributes: attrs, startTime: child.startTime },
              trace.setSpan(ROOT_CONTEXT, parent),
            );
            if (isModel)
              span.setAttribute(
                SC.FI_USAGE_SCOPE,
                value.usagePlan ?? "request",
              );
            const state: {
              span: Span;
              done: boolean;
              isModel: boolean;
              pending?: EndInput;
              measuredTiming: boolean;
              skipped: boolean;
              outcome?: TurnOutcome;
            } = {
              span,
              done: false,
              isModel,
              skipped: checkpoint.has(contentHash(child.id)),
              measuredTiming:
                isModel &&
                child.fidelity === "native-request" &&
                !["reconstructed", "estimated", "unknown"].includes(
                  child.timing ?? "",
                ) &&
                !child.sourceTiming,
            };
            children.set(child.id, state);
            return {
              context: span.spanContext(),
              end(end = {}) {
                if (state.done) return;
                state.done = true;
                try {
                  state.outcome = endOutcome(end, child.kind === "tool");
                  if (state.skipped) return;
                  if (isModel && canCollect()) {
                    if (label(end.model))
                      span.setAttribute(SC.LLM_MODEL_NAME, end.model!);
                    if (label(end.provider))
                      span.setAttribute(SC.LLM_PROVIDER, end.provider!);
                  }
                  if (isModel && value.usagePlan === "turn") {
                    state.pending = {
                      ...end,
                      outcome: state.outcome,
                      output:
                        config.hideOutputs ||
                        (config.hideErrorDetails && state.outcome === "error")
                          ? undefined
                          : content(end.output, config.credentials),
                      usage: end.usage ? { ...end.usage } : undefined,
                      endTime: end.endTime ?? Date.now(),
                    };
                    return;
                  }
                  finish(
                    span,
                    { ...end, outcome: state.outcome },
                    (isModel ||
                      (child.kind === "compaction" &&
                        child.fidelity === "aggregate-turn" &&
                        child.usageContribution === "exclusive")) &&
                      (value.usagePlan ?? "request") !== "turn" &&
                      value.usagePlan !== "unknown",
                    state.measuredTiming,
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
            const lastModel = [...children.values()]
              .filter((item) => item.isModel)
              .at(-1);
            // A generic Stop is not proof of success after a fatal provider error.
            // A later successful generation recovers earlier retry failures.
            const terminalOutcome = endOutcome(end);
            const outcome =
              lastModel?.outcome === "error" &&
              terminalOutcome !== "interrupted"
                ? "error"
                : terminalOutcome;
            // Match the shared turn-usage contract: source insertion order, never finish order.
            const models =
              value.usagePlan === "turn"
                ? [...children.values()].filter((item) => item.isModel)
                : [];
            const usageOwner = models.at(-1);
            for (const item of models) {
              if (item.skipped) {
                item.done = true;
                continue;
              }
              const ownsUsage = item === usageOwner && end.usage !== undefined;
              incomplete ||= !item.done;
              const modelEnd = item.pending ?? {
                outcome: "incomplete" as const,
                endTime: end.endTime,
              };
              item.done = true;
              item.span.setAttribute(
                SC.FI_USAGE_CONTRIBUTION,
                ownsUsage ? "additive" : "none",
              );
              item.span.setAttribute(SC.FI_AGENT_USAGE_ADDITIVE, ownsUsage);
              finish(
                item.span,
                { ...modelEnd, ...(ownsUsage ? { usage: end.usage } : {}) },
                ownsUsage,
                item.measuredTiming,
              );
            }
            for (const item of children.values())
              if (!item.done && !item.skipped) {
                incomplete = true;
                item.done = true;
                finish(
                  item.span,
                  { outcome: "incomplete", endTime: end.endTime },
                  false,
                );
              }
            if (incomplete)
              root.setAttribute(SC.FI_AGENT_GAP, "MISSING_CHILD_STOP");
            root.setAttribute(SC.FI_USAGE_SCOPE, "turn");
            if (!value.deferRoot)
              finish(
                root,
                {
                  ...end,
                  outcome:
                    incomplete && outcome === "success"
                      ? "incomplete"
                      : outcome,
                },
                value.usagePlan === "turn" && !usageOwner,
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
    async discard() {
      closed = true;
      active.clear();
      processor.discard();
      try {
        await provider.shutdown();
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
