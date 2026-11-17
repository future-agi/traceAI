import { createTraceState, trace, TraceFlags, type Span, type TraceState } from "@opentelemetry/api";
import { SamplingDecision, type ReadableSpan, type Sampler } from "@opentelemetry/sdk-trace-base";

/** Metadata captured by our sampler factory, never inferred from arbitrary samplers. */
export interface SamplerSelection {
  sampler: Sampler;
  ratio?: { rate: number; parentBased: boolean };
}

const ATTRIBUTE = "fi.sampling.ratio";
const validRatio = (rate: number): boolean => Number.isFinite(rate) && rate > 0 && rate < 1;

function parentRatio(entry: string | undefined): number | undefined {
  if (!entry?.startsWith("r:")) return undefined;
  const decimal = entry.slice(2);
  // Accept decimal round-trip forms (including exponents), not Number's hex or
  // whitespace coercions. Never log untrusted tracestate values.
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(decimal)) return undefined;
  const rate = Number(decimal);
  return validRatio(rate) ? rate : undefined;
}

/** Retain vendor entries unless W3C limits require evicting rightmost entries. */
function withRatioState(state: TraceState, rate: number): TraceState {
  let updated = state.set("fi", `r:${String(rate)}`);
  let members = updated.serialize().split(",");
  // The API's set() does not enforce either limit. Keep fi first and remove only
  // the oldest entries needed, rather than emitting a state rejected on the wire.
  while (members.length > 32 || members.join(",").length > 512) {
    const last = members.pop()!;
    updated = updated.unset(last.slice(0, last.indexOf("=")).trim());
  }
  return updated;
}

/** Pair only with the agent-owned SDK provider, with onStart first in its pipeline.
 * The SDK admits caller attributes before sampler attributes. If that fills a
 * positive budget, exchange its last admitted attribute for the sampler's ratio.
 * No caller input or application-owned span is mutated. Keep the SDK's existing
 * drop count: one rejected ratio becomes one displaced ordinary attribute.
 */
export function samplingRatioAdmission(selection: SamplerSelection): { sampler: Sampler; onStart(span: ReadableSpan): void } {
  const delegate = withSamplingRatio(selection);
  // SDK 2.x carries result.traceState by identity into spanContext. A unique,
  // native state snapshot pairs each sampled construction with onStart, even
  // when a diagnostic logger synchronously starts another same-trace span.
  // Weak keys avoid retaining decisions if construction never reaches onStart.
  const pending = new WeakMap<TraceState, { traceId: string; rate: number }>();
  return {
    sampler: delegate === selection.sampler ? delegate : {
      shouldSample(...args) {
        const result = delegate.shouldSample(...args);
        const rate = result.attributes?.[ATTRIBUTE];
        if (result.decision !== SamplingDecision.RECORD_AND_SAMPLED || typeof rate !== "number" || !validRatio(rate) || !result.traceState) return result;
        // A native snapshot remains inspectable by mandatory export privacy and
        // never retains ancestor states, including across different providers.
        const traceState = createTraceState(result.traceState.serialize());
        pending.set(traceState, { traceId: args[1], rate });
        return { ...result, traceState };
      },
      toString: () => delegate.toString(),
    },
    onStart(span) {
      const context = span.spanContext();
      const decided = context.traceState && pending.get(context.traceState);
      if (context.traceState) pending.delete(context.traceState);
      if (!decided || context.traceId !== decided.traceId || !(context.traceFlags & TraceFlags.SAMPLED)) return;
      if (span.attributes[ATTRIBUTE] === decided.rate) return;
      const native = span as ReadableSpan & Span & {
        _attributeCountLimit?: number; _spanLimits?: { attributeCountLimit?: number }; _attributesCount?: number;
      };
      const limit = native._attributeCountLimit ?? native._spanLimits?.attributeCountLimit;
      if (typeof limit !== "number" || limit <= 0) return;
      const keys = Object.keys(native.attributes);
      if (!(ATTRIBUTE in native.attributes) && keys.length >= limit) {
        delete native.attributes[keys[keys.length - 1]];
        // SDK 2.7+ caches cardinality; SDK 2.0 derives it from the map.
        if (typeof native._attributesCount === "number") native._attributesCount--;
      }
      native.setAttribute(ATTRIBUTE, decided.rate);
    },
  };
}

/** Applied only at agent-owned provider creation; decisions remain the delegate's. */
export function withSamplingRatio({ sampler, ratio }: SamplerSelection): Sampler {
  if (!ratio || !Number.isFinite(ratio.rate) || ratio.rate < 0 || ratio.rate > 1) return sampler;
  const { rate, parentBased } = ratio;
  // Parent inheritance is independent of the root probability, including 0/1.
  // Non-parent-based full-rate/disabled samplers need no metadata wrapper.
  if (!parentBased && !validRatio(rate)) return sampler;
  return {
    shouldSample(ctx, traceId, spanName, spanKind, attributes, links) {
      const result = sampler.shouldSample(ctx, traceId, spanName, spanKind, attributes, links);
      if (result.decision !== SamplingDecision.RECORD_AND_SAMPLED) return result;
      const parent = trace.getSpanContext(ctx);
      const followsParent = parentBased && parent !== undefined && trace.isSpanContextValid(parent);
      const effectiveRate = followsParent
        ? ((parent.traceFlags & TraceFlags.SAMPLED) !== 0 ? parentRatio(parent.traceState?.get("fi")) : undefined)
        : rate;
      if (effectiveRate === undefined || !validRatio(effectiveRate)) return result;
      const traceState = result.traceState ?? (parent && trace.isSpanContextValid(parent) ? parent.traceState : undefined) ?? createTraceState();
      return {
        ...result,
        attributes: { ...result.attributes, [ATTRIBUTE]: effectiveRate },
        // Following a parent preserves its entry verbatim; local ratio decisions
        // replace only fi, retaining the delegate's other vendor entries.
        traceState: followsParent ? traceState : withRatioState(traceState, effectiveRate),
      };
    },
    toString: () => sampler.toString(),
  };
}
