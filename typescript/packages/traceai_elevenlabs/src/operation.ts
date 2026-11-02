import { diag, Span, SpanStatusCode } from "@opentelemetry/api";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { TraceConfig } from "@traceai/fi-core";

let diagnosed = false;

/** Fixed, once-per-process diagnostic: exceptions may contain customer content. */
export function diagnose(): void {
  if (diagnosed) return;
  diagnosed = true;
  try {
    diag.warn(
      "@traceai/elevenlabs: telemetry failed; SDK calls continue unchanged, but traces may be incomplete.",
    );
  } catch {
    /* A throwing diagnostic logger must not recurse or break an SDK call. */
  }
}

/** Never let telemetry failures replace a provider value or exception. */
export function safely(fn: () => void): void {
  try {
    fn();
  } catch {
    diagnose();
  }
}

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

/** Only structured identifiers, never arbitrary options, URLs, or file names. */
export function identifier(value: unknown): string | undefined {
  return typeof value === "string" &&
    /^[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,127}$/.test(value)
    ? value
    : undefined;
}

export const TEXT_LIMIT = 4096;
export type Completion = "complete" | "cancelled" | "error" | "incomplete";

/** Per-call state is constant-sized; it never retains an audio chunk or request. */
export class Operation {
  private span?: Span;
  private timer?: ReturnType<typeof setTimeout>;
  private firstAudio = false;
  private bytes = 0;
  private chunks = 0;
  private readonly started = performance.now();
  private detachAbort?: () => void;

  constructor(
    span: Span,
    private readonly policy: TraceConfig,
    timeoutMs: number,
    private readonly remove: (operation: Operation) => void,
  ) {
    this.span = span;
    this.timer = setTimeout(() => this.end("incomplete"), timeoutMs);
    this.timer.unref?.();
  }

  watchAbort(signal: unknown): void {
    if (!(signal instanceof AbortSignal)) return;
    const onAbort = () => this.end("cancelled");
    if (signal.aborted) onAbort();
    else {
      signal.addEventListener("abort", onAbort, { once: true });
      this.detachAbort = () => signal.removeEventListener("abort", onAbort);
    }
  }

  audio(value: unknown, timestamps = false): void {
    safely(() => {
      if (!this.span) return;
      const size = timestamps
        ? typeof record(value).audioBase64 === "string"
          ? (record(value).audioBase64 as string).length
          : 0
        : value instanceof Uint8Array
          ? value.byteLength
          : 0;
      if (size === 0) return;
      if (!this.firstAudio) {
        this.firstAudio = true;
        this.span.setAttribute(
          "elevenlabs.first_audio_latency_ms",
          performance.now() - this.started,
        );
      }
      this.chunks++;
      // Timestamp responses contain base64, not raw bytes. Do not estimate decoded size.
      if (!timestamps) this.bytes += size;
    });
  }

  output(value: unknown): void {
    safely(() => {
      if (
        !this.span ||
        this.policy.hideOutputs ||
        this.policy.hideOutputMessages ||
        this.policy.hideOutputText
      )
        return;
      const result = record(value);
      let text =
        typeof result.text === "string" ? result.text.slice(0, TEXT_LIMIT) : "";
      if (Array.isArray(result.transcripts)) {
        // Upstream supports at most five channels. Bound both iteration and retained text.
        text = result.transcripts
          .slice(0, 5)
          .reduce((acc: string, channel: unknown) => {
            const content = record(channel).text;
            return typeof content === "string" && acc.length < TEXT_LIMIT
              ? (
                  acc +
                  (acc ? "\n" : "") +
                  content.slice(0, TEXT_LIMIT - acc.length)
                ).slice(0, TEXT_LIMIT)
              : acc;
          }, "");
      }
      if (text) this.span.setAttribute(S.OUTPUT_VALUE, text);
    });
  }

  status(value: unknown): void {
    safely(() => {
      if (
        typeof value === "number" &&
        Number.isInteger(value) &&
        value >= 100 &&
        value <= 599
      )
        this.span?.setAttribute("http.response.status_code", value);
    });
  }

  error(error: unknown): void {
    safely(() => this.status(record(error).statusCode));
    // SDK exception messages can contain the entire request, signed URL or response body.
    // Never export them, even when FI_HIDE_ERROR_DETAILS is false.
    this.end("error");
  }

  end(completion: Completion): void {
    const span = this.span;
    if (!span) return;
    this.span = undefined;
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    safely(() => this.detachAbort?.());
    this.detachAbort = undefined;
    this.remove(this);
    safely(() => span.setAttribute("elevenlabs.completion", completion));
    if (this.chunks > 0)
      safely(() => span.setAttribute("elevenlabs.audio.chunks", this.chunks));
    if (this.bytes > 0)
      safely(() => span.setAttribute("elevenlabs.audio.bytes", this.bytes));
    safely(() =>
      span.setStatus({
        code:
          completion === "complete"
            ? SpanStatusCode.OK
            : completion === "error"
              ? SpanStatusCode.ERROR
              : SpanStatusCode.UNSET,
      }),
    );
    if (completion === "error")
      safely(() =>
        span.setAttribute("error.type", "elevenlabs.request_failed"),
      );
    safely(() => span.end());
  }
}
