import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { RunnableLambda } from "@langchain/core/runnables";
import type { Run } from "@langchain/core/tracers/base";
import { BasicTracerProvider, ReadableSpan, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "@traceai/fi-core";
import { LangChainTracer } from "../tracer";

// Hold a background root callback open without timing assumptions. SDK shutdown
// alone cannot drain work that LangChain has not finished yet.
async function runWithPendingRoot(drainBeforeShutdown: boolean) {
  let releaseRoot!: () => void;
  const rootGate = new Promise<void>((resolve) => { releaseRoot = resolve; });
  class PendingRootTracer extends LangChainTracer {
    protected async persistRun(run: Run): Promise<void> {
      await rootGate;
      await super.persistRun(run);
    }
  }
  const spans: ReadableSpan[] = [];
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor({
      export(batch, done) { spans.push(...batch); done({ code: 0 }); },
      async shutdown() {},
    })],
  });
  const tracer = new PendingRootTracer(new FITracer({ tracer: provider.getTracer("lifecycle-test") }));
  const chain = RunnableLambda.from((input: number) => input + 1)
    .withConfig({ runName: "first" })
    .pipe(RunnableLambda.from((input: number) => input * 2).withConfig({ runName: "second" }))
    .withConfig({ runName: "root" });
  try {
    expect(await chain.invoke(3, { callbacks: [tracer] })).toBe(8);
    expect(spans.map((span) => span.name)).toEqual(["first", "second"]);
    if (!drainBeforeShutdown) await provider.shutdown();
    releaseRoot();
    await awaitAllCallbacks();
    if (drainBeforeShutdown) await provider.shutdown();
    return spans;
  } finally {
    releaseRoot();
    await awaitAllCallbacks();
    await provider.shutdown();
  }
}

describe("LangChain background callback shutdown", () => {
  it("loses a pending root when the provider is shut down first (negative control)", async () => {
    const spans = await runWithPendingRoot(false);
    expect(spans.map((span) => span.name)).toEqual(["first", "second"]);
  });

  it("exports the complete tree when callbacks are drained before shutdown", async () => {
    const spans = await runWithPendingRoot(true);
    expect(spans.map((span) => span.name)).toEqual(["first", "second", "root"]);
    const root = spans[2];
    for (const child of spans.slice(0, 2)) {
      expect(child.parentSpanContext?.spanId).toBe(root.spanContext().spanId);
      expect(child.spanContext().traceId).toBe(root.spanContext().traceId);
    }
  });
});
