import { describe, it, expect } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { stripStackTrace } from "../trace/trace-config/errorDetails";

const MB = 1024 * 1024;
/** Generous: the linear code needs a few ms per MB, the quadratic one seconds to minutes */
const LIMIT_MS = 200;
// The tests run on shared CI machines: five runs, best one counts (see time())
const FRAME = "    at f (/a.js:1:1)";

function repeatTo(unit: string, size: number): string {
  return unit.repeat(Math.ceil(size / unit.length));
}

/**
 * The best of five runs, in CPU time (not wall-clock time), so neither a busy machine nor other
 * test workers fail a linear algorithm. GC time in this process is included.
 */
function time(fn: () => void): number {
  let best = Infinity;
  for (let run = 0; run < 5; run++) {
    const before = process.cpuUsage();
    fn();
    const used = process.cpuUsage(before);
    best = Math.min(best, (used.user + used.system) / 1000);
  }
  return best;
}

const SHAPES: Array<[string, () => string]> = [
  ["a repeated header paragraph then a frame", () => repeatTo("Note: m\n\n", MB) + FRAME],
  ["repeated paragraphs of different text then a frame", () => {
    let text = "";
    for (let i = 0; text.length < MB; i++) text += `Note: m${i}\n\n`;
    return text + FRAME;
  }],
  ["blank lines then a frame", () => `msg\n${"\n".repeat(MB)}${FRAME}`],
  ["whitespace-only blank lines then a frame", () => `msg\n${repeatTo(" \n\n", MB)}${FRAME}`],
  ["a long single line", () => repeatTo("x", MB)],
  ["a long single line then a frame", () => `${repeatTo("x", MB)}\n${FRAME}`],
  ["many colons", () => repeatTo(":", MB) + "\n" + FRAME],
  ["many colons in an at-line", () => `  at ${repeatTo(":1", MB)}`],
  ["many tracebacks", () => repeatTo("Traceback (most recent call last):\n  File \"a.py\", line 1\nValueError: boom\n", MB)],
  ["many tracebacks on one line", () => repeatTo("Traceback (most recent call last): ", MB)],
  ["long indented traceback bodies", () =>
    `Traceback (most recent call last):\n${repeatTo("    indented source line\n", MB)}ValueError: boom`],
  ["a long whitespace run before a traceback header", () => `${" ".repeat(MB)}Traceback (most recent call last):\nValueError: boom`],
  ["many exception-group prefixes", () => repeatTo("  + | Exception Group Traceback (most recent call last):\n", MB)],
];

describe("stripStackTrace is linear in the message size", () => {
  it.each(SHAPES)("%s, 1 MB", (_name, build) => {
    const input = build();
    let result = "";
    const elapsed = time(() => {
      result = stripStackTrace(input);
    });
    expect(typeof result).toBe("string");
    expect(elapsed).toBeLessThan(LIMIT_MS);
  });

  it("still strips the frame and keeps the whole message on big inputs", () => {
    // Only the frame goes; the repeated paragraphs are not the message followed by its header
    expect(stripStackTrace(`${"Note: m\n\n".repeat(51)}${FRAME}`)).toBe("Note: m\n\n".repeat(50) + "Note: m");
    expect(stripStackTrace(`m\n\nError: m\n${FRAME}`)).toBe("m");
    expect(stripStackTrace(`line one\nline two\n\nError: line one\nline two\n${FRAME}`)).toBe(
      "line one\nline two",
    );
  });
});

describe("a 1 MB message through the span API", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const tracer = new FITracer({ tracer: provider.getTracer("perf"), traceConfig: {} });

  it("setStatus in default mode", () => {
    const message = repeatTo("Note: m\n\n", MB) + FRAME;
    const span = tracer.startSpan("s");
    expect(time(() => span.setStatus({ code: SpanStatusCode.ERROR, message }))).toBeLessThan(LIMIT_MS);
    span.end();
  });

  it("setStatus in default mode, plain message", () => {
    const span = tracer.startSpan("s");
    const message = repeatTo("x", MB);
    expect(time(() => span.setStatus({ code: SpanStatusCode.ERROR, message }))).toBeLessThan(LIMIT_MS);
    span.end();
  });

  it("recordException", () => {
    const error = new Error(repeatTo("Note: m\n\n", MB));
    const span = tracer.startSpan("s");
    expect(time(() => span.recordException(error))).toBeLessThan(LIMIT_MS);
    span.end();
  });
});
