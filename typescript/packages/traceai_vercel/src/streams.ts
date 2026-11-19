import { ReadableStream, ReadableStreamDefaultReader } from "node:stream/web";

/**
 * v5/v6 native telemetry and v7 object callbacks can miss raw ReadableStream
 * errors instead of SDK error parts. Observe the SDK's private object-mode base
 * stream on demand, keeping its public result and every public helper intact.
 * Zero high-water mark means installing this adapter never reads or locks it.
 */
export function observeStreamResult(
  result: unknown,
  onFailure: (error: unknown) => void,
  onIncomplete: () => void,
): void {
  if (!result || typeof result !== "object") return;
  const descriptor = Object.getOwnPropertyDescriptor(result, "baseStream");
  if (!descriptor?.writable || !(descriptor.value instanceof ReadableStream)) return;
  const source: ReadableStream = descriptor.value;
  let reader: ReadableStreamDefaultReader | undefined;
  const stream = new ReadableStream({
    async pull(controller) {
      try {
        reader ??= source.getReader();
        const item = await reader.read();
        if (item.done) {
          controller.close();
          reader.releaseLock();
          onIncomplete(); // Usually a no-op: SDK callbacks have already ended every span.
        } else controller.enqueue(item.value);
      } catch (error) {
        onFailure(error);
        controller.error(error);
        try { reader?.releaseLock(); } catch { /* A cancelled pending read can hold the lock. */ }
      }
    },
    cancel(reason) {
      onIncomplete();
      return reader ? reader.cancel(reason) : source.cancel(reason);
    },
  }, { highWaterMark: 0 });
  Object.defineProperty(result, "baseStream", { ...descriptor, value: stream });
}
