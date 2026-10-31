import { createFIMastraExporter } from "../FIObservability";
import type { SpanProcessor } from "@opentelemetry/sdk-trace-base";

function create(forceFlush: () => Promise<void>) {
  const shutdown = jest.fn(async () => {});
  const processor: SpanProcessor = { onStart() {}, onEnd() {}, shutdown, forceFlush };
  return { exporter: createFIMastraExporter({ apiKey: "test-key", secretKey: "test-secret", spanProcessor: processor }), shutdown };
}

describe("borrowed auto-agent processor lifecycle", () => {
  it("does not let a rejected collector flush change application shutdown", async () => {
    const { exporter, shutdown } = create(async () => { throw new Error("collector failure"); });
    await expect(exporter.flush()).resolves.toBeUndefined();
    await expect(exporter.shutdown()).resolves.toBeUndefined();
    expect(shutdown).not.toHaveBeenCalled();
  });

  it("does not let a never-settling flush hang Mastra shutdown beyond five seconds", async () => {
    jest.useFakeTimers();
    try {
      const { exporter, shutdown } = create(() => new Promise(() => {}));
      const pending = exporter.shutdown();
      await jest.advanceTimersByTimeAsync(5000);
      await expect(pending).resolves.toBeUndefined();
      expect(shutdown).not.toHaveBeenCalled();
      expect(jest.getTimerCount()).toBe(0);
    } finally { jest.useRealTimers(); }
  });
});
