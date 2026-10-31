import { Attributes, createContextKey, createTraceState, INVALID_SPAN_CONTEXT, SpanContext } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { generateTraceConfig } from "./trace/trace-config/traceConfig";
import { REDACTED_VALUE } from "./trace/trace-config/constants";
import { maskAttributes } from "./trace/trace-config/maskingRules";
import type { TraceConfigOptions } from "./trace/trace-config/types";
import { PROJECTION_METADATA } from "./attribute-projection";
import { getRegisteredSecrets, registerSecret } from "./secrets";
import { types } from "node:util";
import { discoverSecrets, indexPrivacyHints, isCredentialName, mediaReplacement, scrubAttribute, scrubValue } from "./privacy-runtime";

// Compatibility name for integration tests/packages migrating this round; the agent never reads or propagates it.
export const LIBRARY_SECRETS = createContextKey("traceai.auto.library.credentials");
/** Read data, not accessors, constructors, toJSON/toString or proxy traps. */
function data(value: unknown, key: PropertyKey): any {
  if (!value || (typeof value !== "object" && typeof value !== "function") || types.isProxy(value)) return undefined;
  for (let level = 0; value && level < 16; level++, value = Object.getPrototypeOf(value)) {
    if (types.isProxy(value)) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor) return "value" in descriptor ? descriptor.value : undefined;
  }
  return undefined;
}
function entries(value: unknown): [string, any][] {
  if (!value || typeof value !== "object" || types.isProxy(value)) return [];
  const result: [string, any][] = [];
  for (const key of Object.keys(value)) {
    if (result.length >= 4096) throw new Error("Privacy attribute budget");
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor && "value" in descriptor) result.push([key, descriptor.value]);
  }
  return result;
}
export type PrivacyOptions = TraceConfigOptions & { hideLLMInvocationParameters?: boolean };

/** The sole FI export boundary. No original span, resource or context is reachable from its snapshot. */
export function privacyFilter(options: PrivacyOptions = {}, configured: readonly string[] = []) {
  for (const secret of configured) registerSecret(secret);
  // FI configuration is independently authoritative even after transport churn
  // evicts its initial seeds. Cache the merged view, not another secret registry.
  const fixed = configured.filter(value => value.length >= 8);
  let lastRegistry: readonly string[] | undefined, lastSnapshot: readonly string[] = [];
  const snapshot = () => {
    const current = getRegisteredSecrets();
    if (current !== lastRegistry) {
      lastSnapshot = fixed.length ? [...new Set([...current, ...fixed])].sort((a, b) => b.length - a.length) : current;
      lastRegistry = current;
    }
    return lastSnapshot;
  };
  const invocation = options.hideLLMInvocationParameters ?? process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS?.trim().toLowerCase() === "true";
  const resolved = generateTraceConfig(options);
  // Derive this from resolved policy, not only the explicit options. Environment
  // image hiding must protect status/exception details identically.
  const tc = generateTraceConfig({ ...resolved, ...((invocation || resolved.hideInputImages) && options.hideErrorDetails === undefined && process.env.FI_HIDE_ERROR_DETAILS === undefined ? { hideErrorDetails: true } : {}) });
  const hideInput = tc.hideInputs || tc.hideInputMessages || tc.hideInputText;
  const hideOutput = tc.hideOutputs || tc.hideOutputMessages || tc.hideOutputText;
  let spanBudget: { remaining: number; nodes: number } | undefined;
  const attributes = (attrs: unknown, requestSecrets?: readonly string[], operation?: unknown, capsuleDepth = 0, admission = false): Attributes => {
    if (capsuleDepth > 16) throw new Error("Privacy capsule budget");
    const secrets = requestSecrets ?? snapshot();
    const budget = spanBudget ?? { remaining: 8 * 1024 * 1024, nodes: 20000 };
    const string = (value: string): string => {
      if ((budget.remaining -= value.length) < 0) throw new Error("Privacy text budget");
      // FI's hide sentinel happens to decode to MP3 frame-sync bytes. It already
      // contains no content; preserve the masking contract before S6 recognition.
      let clean = value === REDACTED_VALUE ? value : scrubAttribute(value, secrets);
      // Generic Bedrock invoke and custom attributes need the same vector policy,
      // irrespective of the catalog's model-kind classification.
      if (tc.hideEmbeddingVectors && /\\*"(?:embedding|embeddings|vector|vectors)\\*"\s*:/i.test(clean)) clean = "[REDACTED]";
      return clean;
    };
    // Materialize hint arrays through data descriptors, never getters/proxy traps.
    let hintText = 0;
    const hintItems = entries(attrs).map(([key, item]): [string, unknown] => {
      hintText += key.length;
      if (hintText > budget.remaining) throw new Error("Privacy text budget");
      if (Array.isArray(item) && !types.isProxy(item)) {
        const length = data(item, "length");
        if (length > budget.nodes) throw new Error("Privacy array budget");
        return [key, Array.from({ length }, (_, i) => data(item, String(i))).filter(v => typeof v === "string")];
      }
      return [key, typeof item === "string" ? item : undefined];
    });
    const hints = indexPrivacyHints(hintItems);
    const value = (item: unknown, key: string, depth = 0): any => {
      if (--budget.nodes < 0 || depth > 32) throw new Error("Privacy value budget");
      if (!admission && isCredentialName(key)) return "<redacted>";
      if (tc.hideEmbeddingVectors && /(?:^|\.)(?:embedding|embeddings|vector|vectors)(?:\.|$)/i.test(key) && !/(?:count|dimension|text)/i.test(key)) return "[REDACTED]";
      if (typeof item === "number" || typeof item === "boolean") {
        const media = mediaReplacement(item, key, hints); if (media !== undefined) return media;
        const raw = String(item), clean = string(raw);
        if (clean !== raw) return clean;
        return typeof item === "number" && !Number.isFinite(item) ? raw : item;
      }
      if (typeof item === "string") {
        if (item.length > budget.remaining) throw new Error("Privacy text budget");
        // Admission protects exact values/media against SDK truncation, without
        // rewriting credential-named host metadata (e.g. session.id). Its name
        // still reaches the detached export boundary, where S6 always masks it.
        const media = item === REDACTED_VALUE ? undefined : mediaReplacement(item, key, hints);
        return string(item === REDACTED_VALUE ? item : media ?? scrubValue(item, secrets, admission && isCredentialName(key) ? "" : key, 0, false, undefined, hints));
      }
      if (Array.isArray(item) && !types.isProxy(item)) {
        const length = data(item, "length");
        if (length > budget.nodes) throw new Error("Privacy array budget");
        const items = Array.from({ length }, (_, i) => data(item, String(i)));
        const media = mediaReplacement(items, key, hints); if (media !== undefined) return media;
        return items.map((item, i) => value(item, `${key}.${i}`, depth + 1));
      }
      return string(item === null ? "null" : typeof item);
    };
    const clean: Attributes = Object.create(null);
    for (const [key, item] of entries(attrs)) {
      if (key === PROJECTION_METADATA) {
        if (typeof item !== "string" || (budget.remaining -= item.length) < 0) throw new Error("Privacy capsule budget");
        const packed: unknown = JSON.parse(item);
        if (!packed || typeof packed !== "object" || Array.isArray(packed)) throw new Error("Privacy capsule shape");
        // A capsule is an attribute map, not an unrestricted JSON blob. Apply
        // every original key's hide family, including error/prompt/image rules.
        clean[key] = JSON.stringify(attributes(packed, secrets, operation, capsuleDepth + 1));
        continue;
      }
      // Apply hiding by semantic family, not a list of particular leaf spellings.
      if ((hideInput && /^(?:input\.|raw\.input$|gen_ai\.input\.|gen_ai\.system_instructions(?:\.|$))/.test(key)) ||
          (hideOutput && /^(?:output\.|raw\.output$|gen_ai\.output\.)/.test(key))) {
        if (["input.value", "output.value", "raw.input", "raw.output"].includes(key)) clean[key] = "[REDACTED]";
        continue;
      }
      if ((hideInput || hideOutput || invocation) && key.startsWith("gen_ai.tool.")) continue;
      if ((hideInput || invocation) && key === "gen_ai.request.parameters") continue;
      if (invocation && /^gen_ai\.request\.(?:temperature|top_p|max_tokens|seed|frequency_penalty|presence_penalty)$/.test(key)) continue;
      if (hideInput && /embedding.*text/.test(key)) continue;
      if (tc.hideInputImages && (/^(?:input\.|raw\.input$|gen_ai\.input\.)/.test(key) || /(?:image|file).*(?:url|uri)|(?:url|uri).*(?:image|file)/i.test(key))) { clean[key] = "[REDACTED]"; continue; }
      if (key === "gen_ai.audio.transcript" && (operation === "text_to_speech" ? hideInput : hideOutput)) { clean[key] = "[REDACTED]"; continue; }
      const name = string(key);
      clean[name] = value(item, key);
    }
    return maskAttributes({ config: tc, attributes: clean });
  };
  const text = (value: unknown, secrets: readonly string[]) => attributes({ value }, secrets).value as string;
  const times = (value: unknown): [number, number] => [0, 1].map(i => { const n = data(value, String(i)); return typeof n === "number" && Number.isFinite(n) ? n : 0; }) as [number, number];
  const spanContext = (value: unknown, secrets: readonly string[]): SpanContext => {
    const raw = data(value, "traceState"), state: string[] = [];
    const add = (key: unknown, val: unknown) => {
      if (typeof key !== "string" || typeof val !== "string" || state.length >= 32) return;
      const cleanKey = text(key, secrets), clean = attributes({ [key]: val }, secrets)[cleanKey];
      if (typeof clean === "string") state.push(`${cleanKey}=${clean.replace(/[=,\r\n]/g, " ")}`);
    };
    const internal = data(raw, "_internalState"), serialized = data(raw, "_rawTraceState");
    if (internal && !types.isProxy(internal)) {
      try {
        const iterator = Map.prototype.entries.call(internal);
        for (let i = 0; i < 32; i++) { const item = iterator.next(); if (item.done) break; add(item.value[0], item.value[1]); }
      } catch { /* Unsupported tracestate representation: omit it. */ }
    }
    else if (typeof serialized === "string") for (const part of serialized.slice(0, 4096).split(",")) { const i = part.indexOf("="); if (i > 0) add(part.slice(0, i), part.slice(i + 1)); }
    return { traceId: typeof data(value, "traceId") === "string" ? data(value, "traceId") : INVALID_SPAN_CONTEXT.traceId,
      spanId: typeof data(value, "spanId") === "string" ? data(value, "spanId") : INVALID_SPAN_CONTEXT.spanId,
      traceFlags: typeof data(value, "traceFlags") === "number" ? data(value, "traceFlags") : 0,
      ...(state.length ? { traceState: createTraceState(state.join(",")) } : {}) };
  };
  return {
    attributes,
    /** @internal Best-effort protection before SDK value/count admission. Names
     * remain available to samplers/host processors; the export boundary is final. */
    admissionAttributes: (attrs: unknown): Attributes => {
      // Harvest already-materialized JSON/URLs before scrubbing removes their
      // credential fields. Keep ordinary host metadata names out of admission
      // discovery; the final detached boundary recognizes those original keys.
      for (const secret of discoverSecrets(entries(attrs).map(([, value]) => value), entries)) registerSecret(secret);
      return attributes(attrs, undefined, data(attrs, "gen_ai.operation.name"), 0, true);
    },
    span(span: ReadableSpan, requestSecrets: readonly string[] | null = [], extraResource: Attributes = {}): ReadableSpan {
      // ReadableSpan's explicit context API is needed for library-generated snapshots;
      // attribute/resource/scope values never get method or accessor dispatch.
      let rawContext = data(span, "_spanContext");
      if (!rawContext) { const method = data(span, "spanContext"); if (typeof method === "function") { try { rawContext = Reflect.apply(method, span, []); } catch { /* Keep timing with an invalid context. */ } } }
      const rawParent = data(span, "parentSpanContext");
      // Identity/timing are independent of content sanitization, including its failure path.
      const base: any = { kind: data(span, "kind"), startTime: times(data(span, "startTime")), endTime: times(data(span, "endTime")), duration: times(data(span, "_duration") ?? data(span, "duration")), ended: true,
        droppedAttributesCount: data(span, "_droppedAttributesCount") ?? data(span, "droppedAttributesCount") ?? 0, droppedEventsCount: data(span, "_droppedEventsCount") ?? data(span, "droppedEventsCount") ?? 0, droppedLinksCount: data(span, "_droppedLinksCount") ?? data(span, "droppedLinksCount") ?? 0 };
      const identityOnly = (value: unknown): SpanContext => ({ traceId: typeof data(value, "traceId") === "string" ? data(value, "traceId") : INVALID_SPAN_CONTEXT.traceId, spanId: typeof data(value, "spanId") === "string" ? data(value, "spanId") : INVALID_SPAN_CONTEXT.spanId, traceFlags: typeof data(value, "traceFlags") === "number" ? data(value, "traceFlags") : 0 });
      const identity = identityOnly(rawContext);
      base.spanContext = () => identity;
      const parent = rawParent ?? (data(span, "parentSpanId") ? { ...identity, spanId: data(span, "parentSpanId") } : undefined);
      if (parent) base.parentSpanContext = identityOnly(parent);
      const previousBudget = spanBudget;
      spanBudget = { remaining: 8 * 1024 * 1024, nodes: 20000 };
      try {
        if (requestSecrets === null) throw new Error("Privacy request discovery failed");
        const attrs = data(span, "attributes"), operation = data(attrs, "gen_ai.operation.name"), status = data(span, "status"), scope = data(span, "instrumentationScope") ?? data(span, "instrumentationLibrary"), resource = data(span, "resource");
        const pairs = data(resource, "_rawAttributes");
        // Recognition needs the entire sibling set. Cropping can remove a MIME
        // discriminator and leak its earlier payload, so exhaustions fail closed.
        if (Array.isArray(pairs) && !types.isProxy(pairs) && data(pairs, "length") > 4096) throw new Error("Privacy resource budget");
        // SDK 2.x raw resources are ordered FIRST non-null value wins. Ordinary
        // Object.fromEntries would reverse service/resource merge precedence.
        const rawAttrs = data(resource, "attributes") ?? data(resource, "_attributes") ?? data(resource, "_memoizedAttributes") ?? Object.fromEntries(Array.isArray(pairs) && !types.isProxy(pairs) ? Array.from({ length: data(pairs, "length") }, (_, i) => {
          const pair = data(pairs, String(i)); return [data(pair, "0"), data(pair, "1")];
        }).filter(([key, value]) => typeof key === "string" && value != null).reverse() : []);
        const surfaces = (items: unknown) => Array.isArray(items) && !types.isProxy(items) ? Array.from({ length: Math.min(data(items, "length"), 4096) }, (_, i) => data(data(items, String(i)), "attributes")) : [];
        for (const secret of requestSecrets) registerSecret(secret); // Legacy direct callers, not context inheritance.
        for (const secret of discoverSecrets([attrs, rawAttrs, extraResource, data(scope, "attributes"), ...surfaces(data(span, "events")), ...surfaces(data(span, "links"))], entries)) registerSecret(secret);
        const secrets = snapshot(); // One registry lookup/snapshot per span, shared by every surface.
        const ctx = spanContext(rawContext, secrets);
        const array = (items: unknown, convert: (item: any) => any): any[] => {
          if (!Array.isArray(items) || types.isProxy(items)) return [];
          return Array.from({ length: Math.min(data(items, "length"), 4096) }, (_, i) => convert(data(items, String(i))));
        };
        return { ...base, spanContext: () => ctx, ...(parent ? { parentSpanContext: spanContext(parent, secrets) } : {}),
          name: text(data(span, "name") ?? "", secrets), attributes: attributes(attrs, secrets, operation),
          resource: resourceFromAttributes({ ...attributes(rawAttrs, secrets, operation), ...attributes(extraResource, secrets) }),
          instrumentationScope: { name: text(data(scope, "name") ?? "", secrets), version: text(data(scope, "version") ?? "", secrets), schemaUrl: text(data(scope, "schemaUrl") ?? "", secrets), attributes: attributes(data(scope, "attributes"), secrets, operation) },
          status: { code: data(status, "code") ?? 0, ...(!tc.hideErrorDetails && data(status, "message") !== undefined ? { message: text(data(status, "message"), secrets) } : {}) },
          events: array(data(span, "events"), event => ({ name: text(data(event, "name") ?? "", secrets), time: times(data(event, "time")), attributes: attributes(data(event, "attributes"), secrets, operation), droppedAttributesCount: data(event, "droppedAttributesCount") ?? 0 })),
          links: array(data(span, "links"), link => ({ context: spanContext(data(link, "context"), secrets), attributes: attributes(data(link, "attributes"), secrets, operation), droppedAttributesCount: data(link, "droppedAttributesCount") ?? 0 })),
        } as ReadableSpan;
      } catch {
        // Fail closed per span, not per batch. Never inherit unsanitized data or lose timing.
        return { ...base, name: "<redacted>", attributes: {}, status: { code: data(data(span, "status"), "code") ?? 0 }, events: [], links: [], resource: resourceFromAttributes({}), instrumentationScope: { name: "@traceai/auto" } };
      } finally { spanBudget = previousBudget; }
    },
  };
}
