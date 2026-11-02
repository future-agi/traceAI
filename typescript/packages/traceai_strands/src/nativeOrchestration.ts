import { context, createContextKey, SpanStatusCode, type Context } from "@opentelemetry/api";
import type { ReadableSpan, Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";

type Completion = "COMPLETED" | "FAILED" | "CANCELLED" | "INTERRUPTED" | "UNKNOWN";
interface Invocation {
  owner: StrandsSpanProcessor;
  claimed: boolean;
  finished: boolean;
  completion: Completion;
  span?: ReadableSpan;
}
const INVOCATION = createContextKey("traceai.strands.orchestration");

function completion(result: unknown): Completion {
  const status = result && typeof result === "object" && "status" in result ? result.status : undefined;
  return status === "COMPLETED" || status === "FAILED" || status === "CANCELLED" || status === "INTERRUPTED"
    ? status : "UNKNOWN";
}

/**
 * Forward existing native spans, holding a tracked orchestration root until its
 * public invoke/stream result is known. Strands ends that span BEFORE emitting
 * MultiAgentResultEvent, so an exporter alone cannot recover the result.
 *
 * Install around the processor feeding StrandsSpanExporter, then call invoke or
 * stream on this instance. No SDK methods are patched and no spans are created.
 */
export class StrandsSpanProcessor implements SpanProcessor {
  private readonly scope: string;
  private readonly invocations = new WeakMap<ReadableSpan, Invocation>();
  private readonly pending = new Set<Invocation>();
  private closed = false;

  constructor(private readonly processor: SpanProcessor, options: { instrumentationScope?: string } = {}) {
    this.scope = options.instrumentationScope || process.env.OTEL_SERVICE_NAME || "strands-agents";
  }

  onStart(span: Span, parentContext: Context): void {
    const invocation = parentContext.getValue(INVOCATION) as Invocation | undefined;
    const operation = span.attributes["gen_ai.operation.name"];
    if (!this.closed && invocation?.owner === this && !invocation.claimed &&
        span.instrumentationScope.name === this.scope && (operation === "invoke_graph" || operation === "invoke_swarm")) {
      // Bind by invocation context + exact span, never by reusable agent IDs.
      // Nested orchestrators need their own tracked call; their results must not
      // be replaced by the outer orchestration's result.
      invocation.claimed = true;
      this.invocations.set(span, invocation);
    }
    this.processor.onStart(span, parentContext);
  }

  onEnd(span: ReadableSpan): void {
    if (this.closed) return;
    const invocation = this.invocations.get(span);
    if (!invocation) { this.processor.onEnd(span); return; }
    this.invocations.delete(span);
    invocation.span = span;
    if (invocation.finished) this.forward(invocation);
    else {
      this.pending.add(invocation);
      // A blocked application hook must not retain an unbounded number of
      // ended roots. Evicted roots are forwarded once as unknown.
      if (this.pending.size > 4096) this.forward(this.pending.values().next().value!);
    }
  }

  /** Run one graph/swarm invocation using its public API and preserve its result. */
  async invoke<T>(callback: () => Promise<T>): Promise<T> {
    const invocation = this.createInvocation();
    try {
      const result = await context.with(context.active().setValue(INVOCATION, invocation), callback);
      invocation.completion = completion(result);
      return result;
    } catch (error) {
      invocation.completion = "FAILED";
      throw error;
    } finally {
      this.finish(invocation);
    }
  }

  /** Consume one public graph/swarm stream; early return without a result is unknown. */
  async *stream<T, R>(callback: () => AsyncGenerator<T, R, unknown>): AsyncGenerator<T, R, unknown> {
    const invocation = this.createInvocation();
    const active = context.active().setValue(INVOCATION, invocation);
    let iterator: AsyncGenerator<T, R, unknown> | undefined;
    let done = false;
    try {
      iterator = context.with(active, callback);
      while (true) {
        const next = await context.with(active, () => iterator!.next());
        if (next.done) {
          done = true;
          const final = completion(next.value);
          if (final !== "UNKNOWN") invocation.completion = final;
          return next.value;
        }
        const event = next.value;
        if (event && typeof event === "object" && "type" in event &&
            event.type === "multiAgentResultEvent" && "result" in event) {
          invocation.completion = completion(event.result);
        }
        yield event;
      }
    } catch (error) {
      invocation.completion = "FAILED";
      throw error;
    } finally {
      try {
        // Match for-await's IteratorClose: do not drain downstream work after a
        // consumer break. Cleanup events are not orchestration result evidence.
        if (iterator && !done) await context.with(active, () => iterator!.return(undefined as R));
      } catch (error) {
        invocation.completion = "FAILED";
        throw error;
      } finally {
        this.finish(invocation);
      }
    }
  }

  forceFlush(): Promise<void> {
    // An explicit mid-invocation flush must not hang waiting for a future result.
    // Forward already-ended roots as unknown; never export them a second time.
    for (const invocation of this.pending) this.forward(invocation);
    return this.processor.forceFlush();
  }

  shutdown(): Promise<void> {
    for (const invocation of this.pending) this.forward(invocation);
    this.closed = true;
    return this.processor.shutdown();
  }

  private createInvocation(): Invocation {
    return { owner: this, claimed: false, finished: false, completion: "UNKNOWN" };
  }

  private finish(invocation: Invocation): void {
    invocation.finished = true;
    if (invocation.span) this.forward(invocation);
  }

  private forward(invocation: Invocation): void {
    this.pending.delete(invocation);
    const span = invocation.span;
    delete invocation.span;
    if (!span || this.closed) return;
    const status = invocation.finished ? invocation.completion : "UNKNOWN";
    const code = span.status.code === SpanStatusCode.ERROR || (status !== "COMPLETED" && status !== "UNKNOWN")
      ? SpanStatusCode.ERROR : status === "COMPLETED" ? SpanStatusCode.OK : SpanStatusCode.UNSET;
    // Export-only view: do not mutate ended SDK spans, copy result payloads or
    // introduce synthetic exception messages. Other processors keep their view.
    const view: ReadableSpan = Object.create(span, {
      attributes: { value: { ...span.attributes, "strands.orchestration.status": status } },
      status: { value: { ...span.status, code } },
    });
    this.processor.onEnd(view);
  }
}
