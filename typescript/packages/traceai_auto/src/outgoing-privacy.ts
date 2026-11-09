import { propagation, trace, SpanKind, isSpanContextValid, baggageEntryMetadataFromString, type Context, type Span, type BaggageEntry } from "@opentelemetry/api";
import { isTracingSuppressed, W3CTraceContextPropagator, W3CBaggagePropagator } from "@opentelemetry/core";
import { privacyFilter, type PrivacyOptions } from "./export-privacy";
import { isCredentialName, scrubAttribute, scrubText } from "./wire/privacy-runtime";
import { observePropagationHead } from "./wire/observe-node";
import { allowsOutgoingDestination, type OutgoingPolicy } from "./outgoing-policy";
import { outgoingRedirectClosed, noteOutgoingRedirectSpan } from "./outgoing-redirect";
import { validTraceparent, validTracestate, validBaggage, validBaggageKey, validBaggageValue, validBaggageMetadata } from "./outgoing-w3c";

export type OutgoingHeaderName = "traceparent" | "tracestate" | "baggage";
export type HeaderPairs = readonly (readonly [string, string])[];
const kinds = new WeakMap<object, SpanKind>();
/** No exception from this observer may interrupt application span creation. */
export function noteOutgoingSpanStart(span: Span, parent?: Context): void {
  try {
    noteOutgoingRedirectSpan(span, parent);
    const kind = Object.getOwnPropertyDescriptor(span, "kind");
    if (kind && "value" in kind && Number.isInteger(kind.value) && kind.value >= SpanKind.INTERNAL && kind.value <= SpanKind.CONSUMER) kinds.set(span, kind.value);
  } catch { /* Opaque spans cannot authorize an addition. */ }
}
const traceContext = new W3CTraceContextPropagator();
const baggageContext = new W3CBaggagePropagator();
const setter = { set(carrier: Record<string, string>, key: string, value: string) { carrier[key] = value; } };
export interface OutgoingPrivacy {
  prepareOutgoingHeaders(ctx: Context, destination: URL, existing: HeaderPairs, policy: OutgoingPolicy): ReadonlyMap<OutgoingHeaderName, string> | undefined;
}
/** Scratch-only FI admission: neither the caller's Context nor its carrier is changed. */
export function createOutgoingPrivacy(options: PrivacyOptions = {}, configuredSecrets: readonly unknown[] = []): OutgoingPrivacy {
  const secrets = configuredSecrets.filter((v): v is string => typeof v === "string");
  return {
    prepareOutgoingHeaders(ctx, destination, existing, policy) {
      try {
        // Preserve duplicate credentials. Discovery failure vetoes this entire batch.
        if (!observePropagationHead(destination.href, existing)) return;
        if (!allowsOutgoingDestination(policy, destination) || existing.some(([key]) => key.toLowerCase() === "traceparent") || isTracingSuppressed(ctx) || outgoingRedirectClosed(ctx)) return;
        const span = trace.getSpan(ctx), sc = trace.getSpanContext(ctx);
        const kind = span ? kinds.get(span) : undefined;
        if (kind === SpanKind.CLIENT) return;
        // No structural guess from an arbitrary span.kind. Pre-existing local SDK
        // spans and opaque unsampled/FI wrappers are conservative no-addition paths.
        if (span && kind === undefined && sc?.isRemote !== true) return;
        const hasContext = !!sc && isSpanContextValid(sc);
        const privacy = privacyFilter(options, secrets);
        const unchanged = (key: string, value: string) => {
          if (scrubText(value, secrets) !== value || scrubAttribute(value, secrets) !== value) return false;
          const attrs = privacy.attributes({ [key]: value });
          return Object.prototype.hasOwnProperty.call(attrs, key) && attrs[key] === value;
        };
        const cleanMember = (key: string, value: string, attribute: string) => {
          if (isCredentialName(key) || !unchanged(attribute, key) || !unchanged(attribute, value)) return false;
          const structured = JSON.stringify({ [key]: value });
          if (!unchanged(attribute, structured)) return false;
          // Percent encodings must not conceal credential keys/values from FI.
          let decodedKey: string, decodedValue: string;
          try { decodedKey = decodeURIComponent(key); decodedValue = decodeURIComponent(value); }
          catch { return false; } // Omit this member, not the otherwise-safe parent.
          return !isCredentialName(decodedKey) && unchanged(attribute, JSON.stringify({ [decodedKey]: decodedValue }));
        };
        const exists = (name: string) => existing.some(([key]) => key.toLowerCase() === name);
        const staged = new Map<OutgoingHeaderName, string>();
        if (hasContext) {
          const scratch: Record<string, string> = Object.create(null);
          traceContext.inject(ctx, scratch, setter);
          const parent = scratch.traceparent;
          if (typeof parent !== "string" || !validTraceparent(parent) || !unchanged("http.request.header.traceparent", parent)) return;
          staged.set("traceparent", parent);
          const state = scratch.tracestate;
          if (!exists("tracestate") && typeof state === "string" && validTracestate(state) && unchanged("http.request.header.tracestate", state) && state.split(",").every(member => {
            const at = member.indexOf("="); return cleanMember(member.slice(0, at).trim(), member.slice(at + 1).trim(), "http.request.header.tracestate.member");
          })) staged.set("tracestate", state);
        }
        const source = propagation.getBaggage(ctx);
        if (!exists("baggage") && source) {
          const entries = source.getAllEntries();
          if (entries.length <= 64) {
            const clean: Record<string, BaggageEntry> = Object.create(null);
            let budget = 0, overflow = false;
            for (const [key, entry] of entries) {
              if (typeof key !== "string" || typeof entry.value !== "string") continue;
              if (key.length > 4096 || entry.value.length > 4096) { overflow = true; break; }
              // Snapshot metadata once: a custom object's next toString() must not
              // change a member between its privacy check and final serialization.
              const metadata = entry.metadata === undefined ? undefined : entry.metadata.toString();
              if (metadata !== undefined && typeof metadata !== "string") continue;
              let encodedKey: string, encodedValue: string;
              try { encodedKey = encodeURIComponent(key); encodedValue = encodeURIComponent(entry.value); }
              catch { continue; } // A malformed Unicode member must not discard its safe siblings.
              if (!validBaggageKey(encodedKey) || !validBaggageValue(encodedValue) || metadata !== undefined && !validBaggageMetadata(metadata)) continue;
              budget += encodedKey.length + encodedValue.length + 2 + (metadata === undefined ? 0 : metadata.length + 1);
              if (budget > 4096) { overflow = true; break; }
              if (!key || /[\x00-\x20\x7f]/.test(key) || /[\x00-\x1f\x7f]/.test(entry.value) || !cleanMember(key, entry.value, "http.request.header.baggage.member")) continue;
              if (metadata !== undefined) {
                if (!metadata || !unchanged("http.request.header.baggage.metadata", metadata)) continue;
                const valid = metadata.split(";").every(part => {
                  const at = part.indexOf("="), name = (at < 0 ? part : part.slice(0, at)).trim(), value = at < 0 ? "" : part.slice(at + 1).trim();
                  return validBaggageKey(name) && validBaggageValue(value) && cleanMember(name, value, "http.request.header.baggage.metadata");
                });
                if (!valid) continue;
              }
              clean[key] = { value: entry.value, ...(metadata === undefined ? {} : { metadata: baggageEntryMetadataFromString(metadata) }) };
            }
            // Enforce the budget BEFORE the OTel serializer, which may itself
            // truncate entries. Truncation is not admission of the original batch.
            const serialized = Object.entries(clean).map(([key, entry]) => `${encodeURIComponent(key)}=${encodeURIComponent(entry.value)}${entry.metadata ? `;${entry.metadata.toString()}` : ""}`).join(",");
            if (!overflow && serialized && Buffer.byteLength(serialized, "utf8") <= 4096) {
              const scratch: Record<string, string> = Object.create(null);
              baggageContext.inject(propagation.setBaggage(ctx, propagation.createBaggage(clean)), scratch, setter);
              const value = scratch.baggage;
              if (value === serialized && validBaggage(value) && unchanged("http.request.header.baggage", value)) staged.set("baggage", value);
            }
          }
        }
        return staged.size ? staged : undefined;
      } catch { return; } // Never expose exception text or a partially prepared batch.
    },
  };
}
