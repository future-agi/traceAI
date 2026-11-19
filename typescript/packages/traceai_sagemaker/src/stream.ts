/** Assemble a SageMaker response event stream while handing every event to the caller untouched. */
import { diag } from "@opentelemetry/api";
import * as payload from "./payload";

// A stream that is dropped without being read or closed still ends its span.
const registry = new FinalizationRegistry<() => void>((finalize) => finalize());

// One unterminated line may not grow without bound.
const LINE_LIMIT = payload.PARSE_LIMIT;

/** Reassemble PayloadPart bytes into lines and fold the recognised chunk shapes. */
export class StreamAccumulator {
  readonly usage = new payload.Usage();
  size = 0;
  private readonly kind: payload.Kind;
  private readonly rawText: boolean;
  private readonly decoder = new TextDecoder("utf-8");
  private buffer = "";
  private overflow = false;
  private pieces: string[] = [];
  private length = 0;
  private final?: string;

  constructor(private readonly contentType: unknown, private readonly captureOutput: boolean) {
    this.kind = payload.classify(contentType);
    // Plain text carries no line structure worth parsing; everything else textual is line-delimited.
    this.rawText = this.kind === "text" && payload.baseType(contentType) !== "text/event-stream";
  }

  feed(data: Uint8Array): void {
    this.size += data.byteLength;
    if (this.kind === "binary") return;
    const text = this.decoder.decode(data, { stream: true });
    if (this.rawText) {
      this.add(text);
      return;
    }
    this.buffer += text;
    for (;;) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) break;
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (this.overflow) {
        this.overflow = false;
        continue;
      }
      this.line(line);
    }
    if (this.buffer.length > LINE_LIMIT) {
      this.buffer = "";
      this.overflow = true;
    }
  }

  private add(text: string): void {
    if (!this.captureOutput || !text || this.length >= payload.TEXT_LIMIT) return;
    const part = text.slice(0, payload.TEXT_LIMIT - this.length);
    this.pieces.push(part);
    this.length += part.length;
  }

  private line(raw: string): void {
    let line = raw.trim();
    if (!line || /^(:|event:|id:|retry:)/.test(line)) return;
    if (line.startsWith("data:")) line = line.slice(5).trim();
    if (!line || line === "[DONE]") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return;
    }
    for (const chunk of Array.isArray(parsed) ? parsed : [parsed]) {
      const obj = payload.dict(chunk);
      if (obj) this.chunk(obj);
    }
  }

  private chunk(chunk: Record<string, unknown>): void {
    this.usage.merge(payload.usageOf(chunk));
    const token = payload.dict(chunk.token);
    if (token) {
      // Hugging Face TGI: one token per chunk, the whole text on the last one.
      if (typeof token.text === "string") this.add(token.text);
      if (typeof chunk.generated_text === "string" && chunk.generated_text) this.final = payload.clip(chunk.generated_text);
      return;
    }
    const text = payload.textOf(chunk);
    if (text) this.add(text);
  }

  /** Flush a trailing unterminated line and fold derived totals. */
  finish(): void {
    if (this.kind !== "binary") {
      const rest = this.decoder.decode();
      if (this.rawText) this.add(rest);
      else {
        this.buffer += rest;
        if (this.buffer && !this.overflow) {
          const line = this.buffer;
          this.buffer = "";
          this.line(line);
        }
      }
    }
    this.usage.complete();
  }

  output(): string | undefined {
    if (!this.captureOutput) return undefined;
    if (this.kind === "binary") return payload.binaryNote(this.contentType, this.size);
    if (this.final !== undefined) return this.final;
    return this.pieces.join("") || undefined;
  }
}

export interface StreamHooks {
  /** Called once, on the first PayloadPart with data. */
  onFirstChunk(): void;
  /** Called exactly once when the stream ends; `error` is set when it failed, `complete` false when abandoned. */
  onEnd(result: { acc: StreamAccumulator; complete: boolean; error?: unknown }): void;
}

/**
 * Wrap the SDK's event stream. Events are forwarded unchanged, in order. The end hook runs once:
 * when the stream is exhausted, fails, or is closed early by the consumer.
 */
export function wrapEventStream(
  source: AsyncIterable<unknown>,
  acc: StreamAccumulator,
  hooks: StreamHooks,
): AsyncIterable<unknown> {
  let ended = false;
  const end = (complete: boolean, error?: unknown) => {
    if (ended) return;
    ended = true;
    try {
      acc.finish();
      hooks.onEnd({ acc, complete, error });
    } catch (e) {
      diag.debug("sagemaker stream finalisation failed", e);
    }
  };
  let first = true;
  const observe = (event: unknown) => {
    try {
      const data = payload.dict(payload.dict(event)?.PayloadPart)?.Bytes;
      if (data instanceof Uint8Array) {
        if (first) {
          first = false;
          hooks.onFirstChunk();
        }
        acc.feed(data);
      }
    } catch (e) {
      diag.debug("sagemaker stream capture failed", e); // never let capture break the caller's stream
    }
  };
  const iterable: AsyncIterable<unknown> = {
    [Symbol.asyncIterator]() {
      const iterator = source[Symbol.asyncIterator]();
      return {
        async next(...args: [] | [undefined]) {
          try {
            const step = await iterator.next(...args);
            if (step.done) end(true);
            else observe(step.value);
            return step;
          } catch (error) {
            end(false, error);
            throw error;
          }
        },
        async return(value?: unknown) {
          try {
            return iterator.return ? await iterator.return(value) : { done: true as const, value };
          } finally {
            end(false);
          }
        },
        async throw(error?: unknown) {
          try {
            if (iterator.throw) return await iterator.throw(error);
            throw error;
          } catch (thrown) {
            end(false, thrown);
            throw thrown;
          }
        },
        [Symbol.asyncIterator]() {
          return this;
        },
      } as AsyncIterator<unknown> & AsyncIterable<unknown>;
    },
  };
  registry.register(iterable, () => end(false));
  return iterable;
}
