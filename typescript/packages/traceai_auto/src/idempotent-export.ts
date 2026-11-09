import { performance } from "node:perf_hooks";
import type { SpanContext } from "@opentelemetry/api";
import { ExportResultCode, type ExportResult } from "@opentelemetry/core";
import type { ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-base";

export const MAX_IDEMPOTENT_IDENTITIES = 16384;
export const IDEMPOTENT_IDENTITY_TTL_MS = 60000;

/** Only the FI-owned processor's exporter handoff admits identities. */
export function idempotentExporter(exporter: SpanExporter, clock: () => number = () => performance.now()): SpanExporter {
  const window = new Map<string, { admittedAt: number }>();
  const evictExpired = (now: number) => {
    for (const [key, entry] of window) {
      // Test/custom clocks can regress: FIFO order need not be timestamp order.
      // Scan the bounded window so an earlier future-dated entry cannot hide expiry.
      if (now - entry.admittedAt >= IDEMPOTENT_IDENTITY_TTL_MS) window.delete(key);
    }
  };
  return {
    export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
      let completed = false;
      const admitted = new Map<string, { admittedAt: number }>();
      const done = (result: ExportResult) => {
        if (completed) return;
        completed = true;
        if (result.code !== ExportResultCode.SUCCESS) {
          for (const [key, token] of admitted) {
            // A late failure cannot remove a newer post-eviction admission.
            if (window.get(key) === token) window.delete(key);
          }
        }
        callback(result);
      };
      try {
        const now = clock();
        evictExpired(now);
        const seen = new Set<string>();
        const selected: ReadableSpan[] = [];
        for (const span of spans) {
          const identity: SpanContext = span.spanContext();
          const key = `${identity.traceId}:${identity.spanId}`;
          if (seen.has(key)) continue;
          seen.add(key);
          if (window.has(key)) continue;
          admitted.set(key, { admittedAt: now });
          selected.push(span);
        }
        if (selected.length === 0) { done({ code: ExportResultCode.SUCCESS }); return; }
        // No await separates admission from the owned exporter handoff.
        for (const [key, token] of admitted) {
          window.set(key, token);
          while (window.size > MAX_IDEMPOTENT_IDENTITIES) {
            const oldest = window.keys().next();
            if (oldest.done) break;
            window.delete(oldest.value);
          }
        }
        exporter.export(selected, done);
      } catch { done({ code: ExportResultCode.FAILED }); }
    },
    shutdown() { return exporter.shutdown(); },
    forceFlush() { return exporter.forceFlush?.() ?? Promise.resolve(); },
  };
}
