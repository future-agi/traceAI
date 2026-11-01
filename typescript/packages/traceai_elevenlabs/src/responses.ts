import { Operation, record, safely } from "./operation.js";

/** A zero-prefetch bridge: one upstream read only in response to a downstream pull. */
function audioStream(
  source: ReadableStream<Uint8Array>,
  operation: Operation,
): ReadableStream<Uint8Array> {
  let reader:
    | { mode: "default"; value: ReadableStreamDefaultReader<Uint8Array> }
    | { mode: "byob"; value: ReadableStreamBYOBReader }
    | undefined;
  let cancelled = false;
  let supportsBYOB = false;
  try {
    const probe = source.getReader({ mode: "byob" });
    probe.releaseLock();
    supportsBYOB = true;
  } catch {
    /* A default (non-byte) stream must remain a default stream. */
  }
  const underlying = {
    async pull(
      controller:
        | ReadableStreamDefaultController<Uint8Array>
        | ReadableByteStreamController,
    ) {
      try {
        const request =
          "byobRequest" in controller ? controller.byobRequest : null;
        let next: ReadableStreamReadResult<Uint8Array>;
        if (request?.view) {
          if (reader?.mode !== "byob") {
            reader?.value.releaseLock();
            reader = { mode: "byob", value: source.getReader({ mode: "byob" }) };
          }
          // Pass the consumer's storage through, including its offset and capacity.
          // The upstream read transfers it; respondWithNewView returns that buffer.
          const view = request.view;
          next = await reader.value.read(
            new Uint8Array(view.buffer, view.byteOffset, view.byteLength),
          );
        } else {
          if (reader?.mode !== "default") {
            reader?.value.releaseLock();
            reader = { mode: "default", value: source.getReader() };
          }
          next = await reader.value.read();
        }
        if (cancelled) return;
        if (next.done) {
          operation.end("complete");
          controller.close();
          if (request && next.value) request.respondWithNewView(next.value);
          else if ("byobRequest" in controller) controller.byobRequest?.respond(0);
          reader.value.releaseLock();
        } else {
          operation.audio(next.value);
          if (request) request.respondWithNewView(next.value);
          else controller.enqueue(next.value);
        }
      } catch (error) {
        operation.error(error);
        controller.error(error);
        safely(() => reader?.value.releaseLock());
      }
    },
    async cancel(reason: unknown) {
      cancelled = true;
      operation.end("cancelled");
      try {
        return await (reader
          ? reader.value.cancel(reason)
          : source.cancel(reason));
      } finally {
        safely(() => reader?.value.releaseLock());
      }
    },
  };
  return supportsBYOB
    ? new ReadableStream({ ...underlying, type: "bytes" }, { highWaterMark: 0 })
    : new ReadableStream<Uint8Array>(underlying, { highWaterMark: 0 });
}

function observeIterator<T>(
  iterator: AsyncIterator<T>,
  operation: Operation,
  metadata = false,
): AsyncIterableIterator<T> {
  return {
    [Symbol.asyncIterator]() {
      return this;
    },
    async next(...args: [] | [unknown]) {
      try {
        const next = await iterator.next(...args);
        if (next.done) operation.end("complete");
        else
          operation.audio(
            metadata ? record(next.value).data : next.value,
            true,
          );
        return next;
      } catch (error) {
        operation.error(error);
        throw error;
      }
    },
    async return(value) {
      try {
        return iterator.return
          ? await iterator.return(value)
          : { done: true, value };
      } catch (error) {
        operation.error(error);
        throw error;
      } finally {
        operation.end("cancelled");
      }
    },
    async throw(error) {
      try {
        if (iterator.throw) return await iterator.throw(error);
        throw error;
      } finally {
        operation.error(error);
      }
    },
  };
}

/** Keep the upstream Stream prototype and extra APIs, including current withMetadata(). */
function timestampStream(
  source: AsyncIterable<unknown>,
  operation: Operation,
): AsyncIterable<unknown> {
  return new Proxy(source, {
    get(target, key) {
      if (key === Symbol.asyncIterator)
        return () => observeIterator(target[Symbol.asyncIterator](), operation);
      const value = Reflect.get(target, key, target);
      if (key === "withMetadata" && typeof value === "function") {
        return (...args: unknown[]) => {
          const iterable = Reflect.apply(
            value,
            target,
            args,
          ) as AsyncIterable<unknown>;
          return {
            [Symbol.asyncIterator]: () =>
              observeIterator(
                iterable[Symbol.asyncIterator](),
                operation,
                true,
              ),
          };
        };
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

interface Envelope {
  data: unknown;
  rawResponse: { status?: number };
}
interface ResponsePromise extends Promise<unknown> {
  withRawResponse(): Promise<Envelope>;
}

/**
 * Observe one cached raw result, not each then/await. Retain HttpResponsePromise's
 * prototype and API instead of replacing it with a plain async-function Promise.
 */
export function observeResponse(
  response: ResponsePromise,
  operation: Operation,
  method: string,
): ResponsePromise {
  const raw = response.withRawResponse().then(
    (envelope) => {
      let data = envelope.data;
      safely(() => {
        operation.status(envelope.rawResponse.status);
        if (
          method === "textToSpeech.convert" ||
          method === "textToSpeech.stream"
        ) {
          data = audioStream(data as ReadableStream<Uint8Array>, operation);
        } else if (method === "textToSpeech.streamWithTimestamps") {
          data = timestampStream(data as AsyncIterable<unknown>, operation);
        } else {
          if (method === "speechToText.convert") operation.output(data);
          else operation.audio(data, true);
          operation.end("complete");
        }
      });
      return data === envelope.data ? envelope : { ...envelope, data };
    },
    (error) => {
      operation.error(error);
      throw error;
    },
  );
  // Observing a rejected SDK call must not introduce an unhandled rejection of our own.
  void raw.catch(() => undefined);
  let data: Promise<unknown> | undefined;
  const unwrap = () => (data ??= raw.then((envelope) => envelope.data));
  const methods = {
    then: (...args: Parameters<Promise<unknown>["then"]>) =>
      unwrap().then(...args),
    catch: (...args: Parameters<Promise<unknown>["catch"]>) =>
      unwrap().catch(...args),
    finally: (...args: Parameters<Promise<unknown>["finally"]>) =>
      unwrap().finally(...args),
    withRawResponse: () => raw,
  };
  return new Proxy(response, {
    get(target, key) {
      if (Object.prototype.hasOwnProperty.call(methods, key))
        return methods[key as keyof typeof methods];
      return Reflect.get(target, key, target);
    },
  });
}
