import {
  constants,
  closeSync,
  fstatSync,
  openSync,
  readFileSync,
} from "node:fs";
import { join } from "node:path";
import type { Context, Span } from "@opentelemetry/api";
import type {
  ReadableSpan,
  SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { JsonTraceSerializer } from "@opentelemetry/otlp-transformer";
import type { ResolvedConfig } from "./config.js";
import type { DeliveryState } from "./lifecycle.js";
import { updateJson } from "./files.js";
import { postOtlp, type DeliveryResult } from "./validation.js";

/** Persistent disable wins over a stale process snapshot; any unreadable fence fails closed. */
export function collectionEnabled(config: ResolvedConfig): boolean {
  if (
    !config.enabled ||
    process.env.FI_AGENT_ENABLED === "false" ||
    process.env.FI_AGENT_ENABLED === "0"
  )
    return false;
  let fd: number | undefined;
  try {
    fd = openSync(
      config.paths.manifest,
      constants.O_RDONLY | constants.O_NOFOLLOW,
    );
    const st = fstatSync(fd);
    if (
      !st.isFile() ||
      st.nlink !== 1 ||
      st.size > 65536 ||
      st.mode & 0o077 ||
      (process.getuid && st.uid !== process.getuid())
    )
      return false;
    const value = JSON.parse(readFileSync(fd, "utf8"));
    return (
      value.schemaVersion === 1 &&
      value.disabled?.[config.agent] !== true &&
      (value.generations?.[config.agent] ?? 0) === config.generation
    );
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "ENOENT";
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}
function encode(spans: ReadableSpan[]): Uint8Array {
  return JsonTraceSerializer.serializeRequest(spans)!;
}
/** Finalized standard OTLP, memory-only queue. No retry after ambiguity/partial success; no exactly-once claim. */
export class BoundedSpanProcessor implements SpanProcessor {
  readonly state: DeliveryState = { pending: 0, exported: 0, dropped: 0 };
  private queue: Array<{ span: ReadableSpan; bytes: number }> = [];
  private bytes = 0;
  private active?: Promise<void>;
  private stopped = false;
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
  ) {
    this.config = {
      ...config,
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
      const bytes = encode([span]).byteLength;
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
    const path = join(this.config.paths.stateDir, `${this.config.agent}.json`);
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
          await updateJson<DeliveryState>(path, (old) => ({
            pending: 0,
            exported: (old?.exported ?? 0) + delta.exported,
            dropped: (old?.dropped ?? 0) + delta.dropped,
            activatedAt: old?.activatedAt ?? new Date().toISOString(),
            lastAcceptedAt: delta.acceptedAt ?? old?.lastAcceptedAt,
            lastError:
              delta.error ?? (delta.acceptedAt ? undefined : old?.lastError),
          }));
        } catch {
          /* Bounded diagnostics must fail open too. */
        }
      }
    })().finally(() => {
      this.metadataRunning = false;
    });
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
  private async send(
    spans: ReadableSpan[],
    deadline = Date.now() + 5000,
  ): Promise<void> {
    if (Date.now() >= deadline) {
      this.loss(spans.length, "EXPORT_DEADLINE");
      return;
    }
    if (!collectionEnabled(this.config) || !this.config.credentials) {
      this.loss(spans.length, "DISABLED");
      return;
    }
    this.abort = new AbortController();
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
        body: encode(spans),
        signal: this.abort.signal,
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
      this.state.lastError = result.code;
      this.record(spans.length - rejected, rejected, result.code);
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
  async forceFlush(): Promise<void> {
    if (this.active) return this.active;
    // Snapshot a bounded batch; concurrent new work remains for the next tick.
    const items = this.queue.splice(0, 64);
    for (const item of items) this.bytes -= item.bytes;
    if (!items.length) {
      await this.metadata;
      return;
    }
    this.active = this.send(items.map((i) => i.span)).finally(() => {
      this.state.pending -= items.length;
      this.active = undefined;
    });
    await this.active;
    await this.metadata;
  }
  async shutdown(): Promise<void> {
    clearInterval(this.timer);
    this.stopped = true;
    await this.forceFlush();
    // Bounded termination, never wait indefinitely for all pending requests.
    const left = this.queue.length;
    this.queue = [];
    this.bytes = 0;
    this.state.pending = 0;
    if (left) this.loss(left, "SHUTDOWN_BUDGET");
    await this.metadata;
  }
}
