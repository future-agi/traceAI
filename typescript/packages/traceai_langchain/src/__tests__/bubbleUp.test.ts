import { describe, it, expect, beforeEach } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { BaseTracer } from "@langchain/core/tracers/base";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { RunnableLambda } from "@langchain/core/runnables";
import { FITracer } from "@traceai/fi-core";
import { LangChainTracer } from "../tracer";

const PAYLOAD = "synthetic-private-approval-question";

// Same shape as @langchain/langgraph's errors: control-flow signals expose a
// prototype `is_bubble_up` getter and set `name` in the constructor.
class GraphBubbleUp extends Error {
  get is_bubble_up() {
    return true;
  }
}
class GraphInterrupt extends GraphBubbleUp {
  constructor(public interrupts: unknown[]) {
    super(JSON.stringify(interrupts, null, 2));
    this.name = "GraphInterrupt";
  }
}
class ParentCommand extends GraphBubbleUp {
  constructor() {
    super("Command(graph=PARENT)");
    this.name = "ParentCommand";
  }
}

describe("LangGraph control-flow bubble-ups", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  beforeEach(() => exporter.reset());

  const langChainTracer = () =>
    new LangChainTracer(new FITracer({ tracer: provider.getTracer("test") }));

  async function spanFor(error: Error, runName: string): Promise<ReadableSpan> {
    const node = RunnableLambda.from(async (_input: string) => {
      throw error;
    }).withConfig({ runName });
    await expect(node.invoke("x", { callbacks: [langChainTracer()] })).rejects.toBe(error);
    await awaitAllCallbacks();
    const spans = exporter.getFinishedSpans().filter((span) => span.name === runName);
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  const exceptionEvents = (span: ReadableSpan) =>
    span.events.filter((event) => event.name === "exception");

  it("an interrupt() pause is OK, marked as an interrupt, and does not leak the payload", async () => {
    const span = await spanFor(
      new GraphInterrupt([{ id: "synthetic-id", value: PAYLOAD }]),
      "ask_human",
    );
    expect(span.status).toEqual({ code: SpanStatusCode.OK });
    expect(exceptionEvents(span)).toEqual([]);
    expect(span.attributes["langgraph.interrupt"]).toBe(true);
    expect(span.events.map((event) => event.name)).toContain("interrupt");
    expect(span.attributes["error.type"]).toBeUndefined();
    expect(
      JSON.stringify({ status: span.status, attributes: span.attributes, events: span.events }),
    ).not.toContain(PAYLOAD);
  });

  it("a Command.PARENT handoff is OK and not marked as an interrupt", async () => {
    const span = await spanFor(new ParentCommand(), "route");
    expect(span.status).toEqual({ code: SpanStatusCode.OK });
    expect(exceptionEvents(span)).toEqual([]);
    expect(span.attributes["langgraph.interrupt"]).toBeUndefined();
    expect(span.events).toEqual([]);
  });

  it("recognises a bubble-up from run.error when the error object was not seen", async () => {
    const tracer = langChainTracer();
    const runId = "00000000-0000-4000-8000-000000000101";
    await tracer.handleChainStart(
      { lc: 1, type: "not_implemented", id: ["ask_human"] },
      { input: "x" },
      runId,
    );
    const error = new GraphInterrupt([{ id: "synthetic-id", value: PAYLOAD }]);
    // Bypass the override, as an older BaseTracer path would: only run.error is available
    await BaseTracer.prototype.handleChainError.call(tracer, error, runId);
    const [span] = exporter.getFinishedSpans();
    expect(span.status).toEqual({ code: SpanStatusCode.OK });
    expect(exceptionEvents(span)).toEqual([]);
    expect(span.attributes["langgraph.interrupt"]).toBe(true);
  });

  it("a real error is still an ERROR span with an exception", async () => {
    const span = await spanFor(new TypeError("synthetic node failure"), "broken");
    expect(span.status).toEqual({
      code: SpanStatusCode.ERROR,
      message: "synthetic node failure",
    });
    expect(exceptionEvents(span)).toHaveLength(1);
    expect(span.attributes["langgraph.interrupt"]).toBeUndefined();
  });

  it("an error that only borrows a falsy is_bubble_up is still an error", async () => {
    const error = Object.assign(new Error("synthetic failure"), { is_bubble_up: false });
    const span = await spanFor(error, "not_a_bubble_up");
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(exceptionEvents(span)).toHaveLength(1);
  });
});
