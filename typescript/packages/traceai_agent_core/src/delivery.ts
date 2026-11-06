import type { Context, Span } from "@opentelemetry/api";
import type {
  ReadableSpan,
  SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import {
  JsonTraceSerializer,
  ProtobufTraceSerializer,
} from "@opentelemetry/otlp-transformer";
import { deliveryStatePath, type ResolvedConfig } from "./config.js";
import type { DeliveryState } from "./lifecycle.js";
import { updateJson } from "./files.js";
import { postOtlp, type DeliveryResult } from "./validation.js";

export { collectionEnabled } from "./collection.js";
import { collectionEnabled } from "./collection.js";
type Protocol = "http/json" | "http/protobuf";
function encode(
  spans: ReadableSpan[],
  protocol: Protocol = "http/json",
): Uint8Array {
  return (
    protocol === "http/protobuf" ? ProtobufTraceSerializer : JsonTraceSerializer
  ).serializeRequest(spans)!;
}
async function within<T>(
  operation: Promise<T>,
  deadline: number,
  fallback: T,
  signal?: AbortSignal,
): Promise<T> {
  if (signal?.aborted) return fallback;
  let timer: NodeJS.Timeout | undefined, abort: (() => void) | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<T>((resolve) => {
        abort = () => resolve(fallback);
        signal?.addEventListener("abort", abort, { once: true });
        timer = setTimeout(abort, Math.max(0, deadline - Date.now()));
      }),
    ]);
  } finally {
    clearTimeout(timer);
    if (abort) signal?.removeEventListener("abort", abort);
  }
}
/** Finalized standard OTLP, memory-only queue. No retry after ambiguity/partial success; no exactly-once claim. */
export class BoundedSpanProcessor implements SpanProcessor {
  readonly state: DeliveryState = { pending: 0, exported: 0, dropped: 0 };
  private queue: Array<{ span: ReadableSpan; bytes: number }> = [];
  private bytes = 0;
  private active?: Promise<void>;
  private closing?: Promise<void>;
  private draining?: Promise<void>;
  private shutdownDeadline?: number;
  private readonly beforeExport?: () => Promise<boolean>;
  private readonly protocol: Protocol;
  private stopped = false;
  private revoked = false;
  private abort?: AbortController;
  private readonly timer: NodeJS.Timeout;
  private metadata: Promise<void> = Promise.resolve();
  private metadataRunning = false;
  private delta = {
    exported: 0,
    dropped: 0,
    error: undefined as string | undefined,
    acceptedAt: undefined as string | undefined,
  };
  private readonly config: ResolvedConfig;
  constructor(
    config: ResolvedConfig,
    private readonly onDelivery?: (result: DeliveryResult) => void,
    beforeExportOrProtocol?: (() => Promise<boolean>) | Protocol,
    protocol: Protocol = "http/json",
  ) {
    this.beforeExport =
      typeof beforeExportOrProtocol === "function"
        ? beforeExportOrProtocol
        : undefined;
    this.protocol =
      typeof beforeExportOrProtocol === "string"
        ? beforeExportOrProtocol
        : protocol;
    this.config = {
      ...config,
      revalidate: config.revalidate,
      paths: { ...config.paths },
      credentials: config.credentials ? { ...config.credentials } : undefined,
    };
    this.timer = setInterval(() => {
      if (!collectionEnabled(this.config)) this.fence();
      else void this.forceFlush();
    }, 1000);
    this.timer.unref();
  }
  onStart(_span: Span, _parent: Context): void {}
  onEnd(span: ReadableSpan): void {
    try {
      if (this.stopped || !collectionEnabled(this.config)) {
        this.loss(1, "DISABLED");
        return;
      }
      const bytes = encode([span], this.protocol).byteLength;
      if (
        bytes > 256 * 1024 ||
        this.queue.length >= 256 ||
        this.bytes + bytes > 4 * 1024 * 1024
      ) {
        this.loss(1, "QUEUE_LIMIT");
        return;
      }
      this.queue.push({ span, bytes });
      this.bytes += bytes;
      this.state.pending++;
    } catch {
      this.loss(1, "INVALID_SPAN");
    }
  }
  private record(
    exported: number,
    dropped: number,
    error?: string,
    acceptedAt?: string,
  ): void {
    this.delta.exported += exported;
    this.delta.dropped += dropped;
    this.delta.error = error ?? this.delta.error;
    this.delta.acceptedAt = acceptedAt ?? this.delta.acceptedAt;
    if (this.metadataRunning) return;
    this.metadataRunning = true;
    const path = deliveryStatePath(this.config);
    this.metadata = (async () => {
      while (this.delta.exported || this.delta.dropped) {
        const delta = this.delta;
        this.delta = {
          exported: 0,
          dropped: 0,
          error: undefined,
          acceptedAt: undefined,
        };
        try {
          await updateJson<DeliveryState>(
            path,
            (old) => ({
              pending: 0,
              ...(this.config.deliveryBinding
                ? { binding: this.config.deliveryBinding }
                : {}),
              exported: (old?.exported ?? 0) + delta.exported,
              dropped: (old?.dropped ?? 0) + delta.dropped,
              activatedAt: old?.activatedAt ?? new Date().toISOString(),
              lastAcceptedAt: delta.acceptedAt ?? old?.lastAcceptedAt,
              lastError:
                delta.error ?? (delta.acceptedAt ? undefined : old?.lastError),
            }),
            { lockWaitMs: 200 },
          );
        } catch {
          /* Bounded diagnostics must fail open too. */
        }
      }
    })().finally(() => {
      this.metadataRunning = false;
    });
  }
  recordDropped(count: number, reason: string): void {
    if (
      Number.isSafeInteger(count) &&
      count > 0 &&
      /^[A-Z_]{1,80}$/.test(reason)
    )
      this.loss(count, reason);
  }
  private loss(count: number, reason: string): void {
    this.state.dropped += count;
    this.state.lastError = reason;
    this.record(0, count, reason);
  }
  private fence(): void {
    this.abort?.abort();
    const count = this.queue.length;
    this.queue = [];
    this.bytes = 0;
    this.state.pending -= count;
    if (count) this.loss(count, "DISABLED");
  }
  discard(): void {
    this.revoked = true;
    this.stopped = true;
    clearInterval(this.timer);
    this.fence();
  }
  private async send(
    spans: ReadableSpan[],
    deadline = Date.now() + 5000,
  ): Promise<void> {
    deadline = Math.min(deadline, this.shutdownDeadline ?? Infinity);
    const controller = new AbortController();
    this.abort = controller;
    // Re-read trusted policy and the adapter's durable snapshot at every batch,
    // including 413 splits. A hung or unreadable policy can never bypass the budget.
    for (const check of [this.beforeExport, this.config.revalidate]) {
      if (!check) continue;
      let allowed = false;
      try {
        allowed = await within(
          Promise.resolve().then(check),
          deadline,
          false,
          controller.signal,
        );
      } catch {
        /* fail closed */
      }
      if (!allowed) {
        if (controller.signal.aborted) {
          this.loss(spans.length, "SHUTDOWN_BUDGET");
          return;
        }
        this.discard();
        this.loss(spans.length, "EXPORT_POLICY_REVOKED");
        return;
      }
    }
    deadline = Math.min(deadline, this.shutdownDeadline ?? Infinity);
    // The policy check can await I/O: recheck the deadline and disable fence afterwards.
    if (Date.now() >= deadline) {
      this.loss(
        spans.length,
        this.shutdownDeadline === undefined
          ? "EXPORT_DEADLINE"
          : "SHUTDOWN_BUDGET",
      );
      return;
    }
    if (
      this.revoked ||
      controller.signal.aborted ||
      !collectionEnabled(this.config) ||
      !this.config.credentials
    ) {
      this.loss(spans.length, "DISABLED");
      return;
    }
    // Cross-process disable interrupts a currently outstanding request as well as queued work.
    const fence = setInterval(() => {
      if (!collectionEnabled(this.config)) this.abort?.abort();
    }, 25);
    fence.unref();
    let result: DeliveryResult;
    try {
      result = await postOtlp({
        endpoint: this.config.endpoint,
        projectName: this.config.projectName,
        credentials: this.config.credentials,
        body: encode(spans, this.protocol),
        protocol: this.protocol,
        signal: controller.signal,
        timeoutMs: deadline - Date.now(),
      });
    } catch {
      result = { code: "TRANSPORT_AMBIGUOUS" };
    } finally {
      clearInterval(fence);
    }
    if (result.code === "TOO_LARGE" && spans.length > 1) {
      const middle = Math.ceil(spans.length / 2);
      await this.send(spans.slice(0, middle), deadline);
      await this.send(spans.slice(middle), deadline);
      return;
    }
    if (result.code === "ACCEPTED") {
      this.state.exported += spans.length;
      this.state.lastAcceptedAt = result.acceptedAt;
      this.record(spans.length, 0, undefined, result.acceptedAt);
    } else {
      // Partial success identifies counts, not members: never resend any part of that batch.
      const rejected =
        result.code === "PARTIAL_REJECTION" &&
        result.rejectedSpans !== undefined
          ? Math.min(spans.length, result.rejectedSpans)
          : spans.length;
      this.state.exported += spans.length - rejected;
      this.state.dropped += rejected;
      const reason =
        this.shutdownDeadline !== undefined &&
        Date.now() >= this.shutdownDeadline
          ? "SHUTDOWN_BUDGET"
          : result.code;
      this.state.lastError = reason;
      this.record(spans.length - rejected, rejected, reason);
      if (result.code === "AUTH_REJECTED") {
        this.stopped = true;
        this.fence();
      }
    }
    try {
      this.onDelivery?.(result);
    } catch {
      /* Host failures must never become instrumentation failures. */
    }
  }
  private exportBatch(deadline = Date.now() + 5000): Promise<void> {
    if (this.active) return this.active;
    const items = this.queue.splice(0, 64);
    for (const item of items) this.bytes -= item.bytes;
    if (!items.length) return Promise.resolve();
    this.active = this.send(
      items.map((i) => i.span),
      deadline,
    ).finally(() => {
      this.state.pending -= items.length;
      this.active = undefined;
    });
    return this.active;
  }
  async forceFlush(deadline = Date.now() + 5000): Promise<void> {
    if (this.closing) return this.closing;
    if (this.draining) return this.draining;
    await this.exportBatch(deadline);
    // Joining another export must also join its persisted receipt.
    await within(this.metadata, deadline, undefined);
  }
  /** Drain the bounded queue under one deadline without closing a reusable host provider. */
  drain(timeoutMs = 5000): Promise<void> {
    if (this.draining) return this.draining;
    const budget = Number.isFinite(timeoutMs)
      ? Math.max(1, Math.min(timeoutMs, 5000))
      : 5000;
    const deadline = Date.now() + budget;
    this.shutdownDeadline = deadline - Math.min(250, Math.floor(budget / 5));
    this.draining = (async () => {
      const abort = setTimeout(
        () => this.abort?.abort(),
        Math.max(0, this.shutdownDeadline! - Date.now()),
      );
      try {
        const work = async () => {
          await this.active;
          while (this.queue.length && Date.now() < this.shutdownDeadline!)
            await this.exportBatch(this.shutdownDeadline);
        };
        await within(work(), this.shutdownDeadline!, undefined);
        const left = this.queue.length;
        this.queue = [];
        this.bytes = 0;
        this.state.pending -= left;
        if (left) this.loss(left, "SHUTDOWN_BUDGET");
        await within(this.active ?? Promise.resolve(), deadline, undefined);
        await within(this.metadata, deadline, undefined);
      } finally {
        clearTimeout(abort);
        this.shutdownDeadline = undefined;
        this.draining = undefined;
      }
    })();
    return this.draining;
  }
  shutdown(timeoutMs = 5000): Promise<void> {
    if (this.closing) return this.closing;
    clearInterval(this.timer);
    // Native observers can finalize their root while a previous child export is
    // in flight. Include those spans, but reject all work after drain completion.
    this.closing = this.drain(timeoutMs).finally(() => {
      this.stopped = true;
    });
    return this.closing;
  }
}
