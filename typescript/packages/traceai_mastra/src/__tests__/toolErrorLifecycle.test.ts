import type { AnySpan, IModelSpanTracker } from "@mastra/core/observability";
import { toolErrorLifecycle } from "../toolErrorLifecycle";

function native() {
  const wrapStream = vi.fn(stream => stream);
  const tracker = { wrapStream } as unknown as IModelSpanTracker;
  const createTracker = vi.fn(() => tracker);
  const span = { type: "model_generation", createTracker } as unknown as AnySpan & {
    createTracker(): IModelSpanTracker;
  };
  return { span, tracker, wrapStream, createTracker };
}

it("observes exact native failure IDs once without copying, consuming twice or editing chunks", async () => {
  const { span, wrapStream, createTracker } = native();
  const capture = vi.fn();
  const other = vi.fn();
  const a = toolErrorLifecycle(() => true, capture);
  const b = toolErrorLifecycle(() => true, other);
  a.process(span); a.process(span); b.process(span);
  const tracker = span.createTracker();
  expect(span.createTracker()).toBe(tracker);
  const chunks = [{ type: "tool-error", payload: { toolCallId: "failed", error: new Error("private") } },
    { type: "tool-result", payload: { toolCallId: "safe", result: "private" } }];
  const before = chunks.map(chunk => ({ ...chunk, payload: { ...chunk.payload } }));
  const stream = new ReadableStream({ start(controller) { for (const chunk of chunks) controller.enqueue(chunk); controller.close(); } });
  const reader = tracker.wrapStream(stream).getReader();
  for (const chunk of chunks) expect((await reader.read()).value).toBe(chunk);
  expect((await reader.read()).done).toBe(true);
  expect(capture.mock.calls).toEqual([[span, chunks[0].payload]]);
  expect(other.mock.calls).toEqual([[span, chunks[0].payload]]);
  expect(wrapStream).toHaveBeenCalledTimes(1);
  expect(createTracker).toHaveBeenCalledTimes(2);
  expect(chunks).toEqual(before);
});

it("leaves unowned instances, absent spans and SDKs without a tracker unchanged", () => {
  const { span } = native();
  const original = span.createTracker;
  const processor = toolErrorLifecycle(() => false, vi.fn());
  expect(processor.process(span)).toBe(span);
  expect(span.createTracker).toBe(original);
  expect(processor.process(undefined)).toBeUndefined();
  const current = { type: "model_generation" } as AnySpan;
  expect(toolErrorLifecycle(() => true, vi.fn()).process(current)).toBe(current);
});

it("propagates immediate cancellation to the original native stream", async () => {
  const { span } = native();
  const cancel = vi.fn();
  toolErrorLifecycle(() => true, vi.fn()).process(span);
  const stream = span.createTracker().wrapStream(new ReadableStream({ cancel }));
  await stream.cancel("native-cancel");
  // pipeThrough forwards cancellation through its native pipe promise.
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(cancel).toHaveBeenCalledWith("native-cancel");
});
