import { Context, ContextManager, ROOT_CONTEXT } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";

export function memoryProvider(): { provider: BasicTracerProvider; exporter: InMemorySpanExporter } {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  return { provider, exporter };
}

export function byName(spans: ReadableSpan[], name: string): ReadableSpan[] {
  return spans.filter((span) => span.name === name);
}

export function one(spans: ReadableSpan[], name: string): ReadableSpan {
  const matches = byName(spans, name);
  if (matches.length !== 1) {
    throw new Error(`expected one span named ${name}, got ${matches.length}: ${spans.map((s) => s.name).join(", ")}`);
  }
  return matches[0];
}

export function parentId(span: ReadableSpan): string | undefined {
  // sdk-trace-base 2.x exposes parentSpanContext; 1.x exposed parentSpanId.
  const anySpan = span as unknown as { parentSpanContext?: { spanId: string }; parentSpanId?: string };
  return anySpan.parentSpanContext?.spanId ?? anySpan.parentSpanId;
}

/** Every attribute value, event attribute and status message, as one string. */
export function exportedText(spans: ReadableSpan[]): string {
  return JSON.stringify(
    spans.map((span) => [span.name, span.attributes, span.events.map((e) => e.attributes), span.status]),
  );
}

export function hrToMs(time: [number, number]): number {
  return time[0] * 1000 + Math.round(time[1] / 1e6);
}

/** Minimal synchronous context manager, so `context.with()` works in tests without extra deps. */
export class StackContextManager implements ContextManager {
  private stack: Context[] = [ROOT_CONTEXT];

  active(): Context {
    return this.stack[this.stack.length - 1];
  }

  with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(
    ctx: Context,
    fn: F,
    thisArg?: ThisParameterType<F>,
    ...args: A
  ): ReturnType<F> {
    this.stack.push(ctx);
    try {
      return fn.call(thisArg, ...args);
    } finally {
      this.stack.pop();
    }
  }

  bind<T>(_ctx: Context, target: T): T {
    return target;
  }

  enable(): this {
    return this;
  }

  disable(): this {
    this.stack = [ROOT_CONTEXT];
    return this;
  }
}
