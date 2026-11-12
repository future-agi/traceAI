import { generateTraceConfig, type TraceConfigOptions } from "@traceai/fi-core";
import { attachmentModality, type AttachmentManager, type AttachmentSpan, type AttachmentOptions, type AttachmentSource, type AttachmentCorrelation, type AttachmentAdmission, type AttachmentAdmissionTicket } from "@traceai/fi-core/attachments";

/**
 * Shared media-owner context marker. `createContextKey` in `@opentelemetry/api` is `Symbol.for`, so the
 * auto wire layer, the ElevenLabs instrumentation and this helper read and write the same key without
 * importing one another. A truthy value means one adapter already captures this call's media.
 */
export const ATTACHMENT_OWNER_KEY: symbol = Symbol.for("traceai.auto.attachment_owner.v1");

/** The two methods of an OpenTelemetry `Context` this helper uses (structural, so no runtime OTel import). */
export interface AttachmentOwnerContext {
  getValue(key: symbol): unknown;
  setValue(key: symbol, value: unknown): AttachmentOwnerContext;
}

/** True when another adapter already owns the media of the call made under `parent`. */
export function attachmentOwnedElsewhere(parent: AttachmentOwnerContext | undefined): boolean {
  try { return !!parent && typeof parent.getValue === "function" && !!parent.getValue(ATTACHMENT_OWNER_KEY); } catch { return false; }
}

export interface ProviderMediaOptions {
  /** The provider-owned manager returned by register(). Never create an uploader per integration. */
  attachments?: AttachmentManager;
  traceConfig?: TraceConfigOptions;
  /**
   * The context active when the application made the call. When it carries the shared owner marker
   * (`ATTACHMENT_OWNER_KEY`), another adapter captures this call's media and this capture is inert.
   */
  parentContext?: AttachmentOwnerContext;
}

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
// Bound synchronous base64 decoding independently of the manager queue. Raw bytes and
// Blobs are bounded by the manager alone (preflight too_large / queue_full).
const MAX_DECODED_BYTES = 1024 * 1024;
const MAX_ENCODED_CHARS = Math.ceil(MAX_DECODED_BYTES / 3) * 4;
const MAX_PARTS = 64;
// Consumed speech streams have no length up front. The core reserves min(this, the negotiated
// single-upload cap) while they are read, so servers whose max_upload_bytes is below 4 MiB still
// admit the stream; a body over the reservation is discarded whole.
const MAX_SPEECH_STREAM_BYTES = 4 * 1024 * 1024;
const DATA_URI_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;
// Steps 11-12 of the core's preflight order run only after every privacy, identity and
// sampling check passed, so these two denials still allow sizing the source exactly.
const SIZE_DENIALS: ReadonlySet<AttachmentAdmission> = new Set(["too_large", "queue_full"]);
const seen = new WeakMap<AttachmentManager, WeakMap<object, Set<string>>>();

type MediaFields = Omit<AttachmentOptions, "mimeType">;
type Owned = Extract<AttachmentSource, { kind: "Bytes" | "Blob" }>;
/** A source sized from its shape only. `take` decodes or hands over the bytes, and runs only after admission. */
type Described = { mimeType: string; size: number; take(maxBytes: number): Owned | undefined };

/** Exact decoded length of padded base64 from its shape only: no decode or allocation. 0 = unusable. */
function base64Length(value: unknown): number {
  if (typeof value !== "string" || !value.length || value.length > MAX_ENCODED_CHARS || value.length % 4 !== 0) return 0;
  const length = (value.length / 4) * 3 - (value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0);
  return length > 0 && length <= MAX_DECODED_BYTES ? length : 0;
}
function base64Source(value: unknown, mimeType: string): Described | undefined {
  const size = base64Length(value);
  if (!size) return undefined;
  return { mimeType, size, take(maxBytes) {
    // Strict alphabet check and decode happen only after the ticket was admitted.
    if (size > maxBytes || !/^[A-Za-z0-9+/]*={0,2}$/.test(value as string)) return undefined;
    const decoded = Buffer.from(value as string, "base64");
    return decoded.byteLength === size ? { kind: "Bytes", data: decoded } : undefined;
  } };
}

/** Typed extraction only. No recursion, URL fetch, provider file lookup, or filesystem access. */
export class ProviderMediaCapture {
  private readonly manager?: AttachmentManager;
  private readonly correlation?: AttachmentCorrelation;
  private readonly local: TraceConfigOptions;
  private readonly seen: Set<string>;
  private attempted = 0;

  constructor(span: AttachmentSpan, options: ProviderMediaOptions = {}) {
    // Honour an existing owner: exactly one adapter uploads a call's media.
    this.manager = attachmentOwnedElsewhere(options.parentContext) ? undefined : options.attachments;
    this.local = { ...options.traceConfig };
    this.seen = new Set();
    if (this.manager) {
      // Late application-owned speech reads use this exact identity, never ambient context.
      this.correlation = this.manager.correlation(span);
      let spans = seen.get(this.manager);
      if (!spans) { spans = new WeakMap(); seen.set(this.manager, spans); }
      let positions = spans.get(span);
      if (!positions) { positions = new Set(); spans.set(span, positions); }
      this.seen = positions;
    }
  }

  /** Wrapper-local restrictions intersect the shared manager policy, never replace it. */
  private locallyDenied(mimeType: string, direction: AttachmentOptions["direction"]): boolean {
    const config = generateTraceConfig(this.local);
    const input = direction !== "output";
    return !!(config.piiRedaction || (input
      ? config.hideInputs || config.hideInputMessages || config.hideInputText || (attachmentModality(mimeType) === "image" && config.hideInputImages)
      : config.hideOutputs || config.hideOutputMessages || config.hideOutputText));
  }

  private ticket(mimeType: string, sourceKind: "Bytes" | "Blob", maxBytes: number, fields: MediaFields): AttachmentAdmissionTicket {
    return this.manager!.preflight(this.correlation, { mimeType, direction: fields.direction, purpose: fields.purpose, capture: fields.capture, sourceKind, maxBytes });
  }

  /**
   * SDK design section 1: preflight BEFORE the source is read, sized or decoded. A denied
   * part is never read. The gate is sized by the adapter's cap (or the known Blob/ArrayBuffer
   * length); after admission the exact size shrinks the reservation at commit. A gate denied
   * only for size, a different exact MIME type, or a part larger than the gate is
   * preflighted once more at its exact type and length. Never more than two preflights.
   */
  private admit(key: string, gateMime: string, sourceKind: "Bytes" | "Blob", gateBytes: number, fields: MediaFields,
    describe: () => Described | undefined): void {
    const manager = this.manager;
    // One budget slot per typed part. A manager without preflight (an older core) captures nothing.
    if (!manager || !this.correlation || typeof manager.preflight !== "function" || this.attempted++ >= MAX_PARTS || this.seen.has(key)) return;
    // Every ticket taken here is released on exit. release() is a no-op once a ticket was used.
    const taken: AttachmentAdmissionTicket[] = [];
    const preflight = (mimeType: string, maxBytes: number) => {
      const ticket = this.ticket(mimeType, sourceKind, maxBytes, fields);
      taken.push(ticket);
      return ticket;
    };
    try {
      if (this.locallyDenied(gateMime, fields.direction) || !(gateBytes > 0)) return;
      let ticket = preflight(gateMime, gateBytes);
      if (!ticket.admitted && !SIZE_DENIALS.has(ticket.reason)) return;
      const source = describe();
      if (!source || !(source.size > 0) || this.locallyDenied(source.mimeType, fields.direction)) return;
      if (!ticket.admitted || source.mimeType !== gateMime || source.size > ticket.maxBytes) {
        ticket.release();
        ticket = preflight(source.mimeType, source.size);
        if (!ticket.admitted) return;
      }
      const owned = source.take(ticket.maxBytes);
      if (!owned) return;
      // Denied observations are not retained as per-part diagnostics or references.
      if (ticket.attach(owned, { messageIndex: fields.messageIndex, partIndex: fields.partIndex, role: fields.role, channel: fields.channel,
        conversationId: fields.conversationId, turnId: fields.turnId, toolCallId: fields.toolCallId }).admission === "queued") this.seen.add(key);
    } catch { /* Never leak provider payloads or replace application behavior. */ }
    finally {
      // An admitted ticket that is not used returns its reservation at once.
      for (const ticket of taken) { try { ticket.release(); } catch { /* bounded */ } }
    }
  }

  private base64(key: string, read: () => unknown, mimeType: string, fields: MediaFields): void {
    this.admit(key, mimeType, "Bytes", MAX_DECODED_BYTES, fields, () => base64Source(read(), mimeType));
  }

  private dataUri(key: string, read: () => unknown, fields: MediaFields): void {
    // Every candidate type is an image, so one image gate decides the whole part before
    // the URL string is read. Never retain a remote/signed URL as fallback metadata.
    this.admit(key, DATA_URI_TYPES[0], "Bytes", MAX_DECODED_BYTES, fields, () => {
      const value = read();
      if (typeof value !== "string") return undefined;
      for (const mimeType of DATA_URI_TYPES) {
        const prefix = `data:${mimeType};base64,`;
        if (value.startsWith(prefix)) return base64Source(value.slice(prefix.length), mimeType);
      }
      return undefined;
    });
  }

  /** Gemini Content/Part shapes shared by all three maintained Google wrappers. */
  geminiContents(contents: unknown, direction: "input" | "output" = "input"): void {
    this.safely(() => {
      const values = Array.isArray(contents) ? contents : [contents];
      for (let m = 0; m < Math.min(values.length, MAX_PARTS); m++) {
        const content = record(values[m]);
        // Legacy SDK accepts Part[] directly. No arbitrary nested field walk.
        const parts = content.parts === undefined && content.inlineData ? [content] : list(content.parts);
        for (let p = 0; p < Math.min(parts.length, MAX_PARTS); p++) {
          const inline = record(record(parts[p]).inlineData);
          if (typeof inline.mimeType !== "string") continue;
          this.base64(`gemini:${direction}:${m}:${p}`, () => inline.data, inline.mimeType, {
            direction, purpose: direction === "input" ? "model_input" : "model_output",
            messageIndex: m, partIndex: p,
          });
        }
      }
    });
  }

  geminiResponse(response: unknown): void {
    this.safely(() => {
      const result = record(response);
      const candidates = list((result.response ? record(result.response) : result).candidates);
      this.geminiContents(candidates.slice(0, MAX_PARTS).map(c => record(c).content), "output");
    });
  }

  /**
   * Compatible chat messages: inline image URL, plus typed input_audio for OpenAI-shaped
   * routes that accept it. Groq chat is image-only, so its audio parts are not advertised or read.
   */
  compatibleMessages(messages: unknown, dialect: "openai" | "groq" | "mistral" = "openai"): void {
    this.safely(() => {
      const values = list(messages);
      for (let m = 0; m < Math.min(values.length, MAX_PARTS); m++) {
        const parts = list(record(values[m]).content);
        for (let p = 0; p < Math.min(parts.length, MAX_PARTS); p++) {
          const part = record(parts[p]);
          const options = { direction: "input" as const, purpose: "model_input" as const, messageIndex: m, partIndex: p };
          if (part.type === "image_url") {
            this.dataUri(`chat:${m}:${p}`, () => {
              const image = dialect === "mistral" ? part.imageUrl ?? part.image_url : part.image_url;
              return typeof image === "string" ? image : record(image).url;
            }, options);
          } else if (part.type === "input_audio" && dialect === "openai") {
            const audio = record(part.input_audio);
            const mimeType = audio.format === "wav" ? "audio/wav" : audio.format === "mp3" ? "audio/mpeg" : undefined;
            if (mimeType) this.base64(`chat:${m}:${p}`, () => audio.data, mimeType, options);
          }
        }
      }
    });
  }

  /** Converse only; unknown model families and InvokeModel JSON remain unsupported. */
  bedrockConverse(input: unknown): void {
    this.safely(() => {
      const body = record(input);
      if (typeof body.modelId !== "string" || !/^(?:(?:us|eu|apac|global)\.)?(?:anthropic\.claude-(?:3|sonnet-4|opus-4|haiku-4)|amazon\.nova-(?:lite|pro|premier))/i.test(body.modelId)) return;
      const messages = list(body.messages);
      for (let m = 0; m < Math.min(messages.length, MAX_PARTS); m++) {
        const parts = list(record(messages[m]).content);
        for (let p = 0; p < Math.min(parts.length, MAX_PARTS); p++) {
          const part = record(parts[p]);
          this.bedrockBlock(`bedrock:${m}:${p}`, part, { direction: "input", purpose: "model_input", messageIndex: m, partIndex: p });
          // Typed tool results (for example computer-use screenshots) carry their own
          // image/document blocks. Only these named blocks are read; no generic walk.
          const tool = record(part.toolResult);
          const toolCallId = typeof tool.toolUseId === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(tool.toolUseId) ? tool.toolUseId : undefined;
          const results = list(tool.content);
          for (let q = 0; q < Math.min(results.length, MAX_PARTS); q++) {
            this.bedrockBlock(`bedrock:${m}:${p}:${q}`, record(results[q]), {
              direction: "input", purpose: "tool_result", role: "tool", messageIndex: m, partIndex: p, ...(toolCallId ? { toolCallId } : {}),
            });
          }
        }
      }
    });
  }

  private bedrockBlock(key: string, part: RecordValue, options: MediaFields): void {
    // Bedrock serializes Uint8Array bytes itself; S3 locations and other sources stay unsupported.
    const media = record(part.image ?? part.document);
    const mimeType = part.image ? ({ png: "image/png", jpeg: "image/jpeg", webp: "image/webp" } as Record<string, string>)[String(media.format)]
      : part.document && media.format === "pdf" ? "application/pdf" : undefined;
    if (!mimeType) return;
    this.admit(key, mimeType, "Bytes", MAX_DECODED_BYTES, options, () => {
      const bytes = record(media.source).bytes;
      // The core copies mutable bytes synchronously inside ticket.attach; no adapter-side copy.
      return bytes instanceof Uint8Array && bytes.byteLength ? { mimeType, size: bytes.byteLength, take: max => bytes.byteLength <= max ? { kind: "Bytes", data: bytes } : undefined } : undefined;
    });
  }

  /** Immutable File/Blob input only; never advance fs.ReadStream or fetch Response inputs. */
  speechInput(file: unknown): void {
    this.safely(() => {
      if (typeof Blob === "undefined" || !(file instanceof Blob) || attachmentModality(file.type) !== "audio") return;
      // Blob type and size are metadata; the content is never read here.
      const blob: Blob = file; const mimeType = blob.type; const size = blob.size;
      this.admit("speech:input", mimeType, "Blob", size, { direction: "input", purpose: "model_input" },
        () => ({ mimeType, size, take: max => size <= max ? { kind: "Blob", data: blob } : undefined }));
    });
  }

  /**
   * Observe only bytes the caller consumed: whole-body arrayBuffer()/blob() reads, the chunks
   * of a reader the caller obtained with response.body.getReader() (SDK design 6.4), and the
   * chunks of an async iteration of response.body (`for await`, values()), up to the end of the
   * stream. Unread, cancelled, partially read, released and broken-out-of bodies upload nothing,
   * and so do a second reader or iterator on the same body, tee()/pipeTo()/pipeThrough()/clone()
   * consumers, and any chunk-wise read of a Node-stream body (the node-fetch shim openai 4.x uses
   * by default): only WHATWG ReadableStream bodies are observed chunk by chunk. On that transport
   * a whole-body arrayBuffer() read is observed, but blob() is not (node-fetch resolves its own Blob
   * class, not the global Blob).
   */
  speechResponse<T>(response: T): T {
    this.safely(() => {
      const target = response as unknown as Response;
      if (!target || typeof target.headers?.get !== "function" || target.bodyUsed) return;
      const mimeType = target.headers.get("content-type")?.split(";")[0].trim();
      if (!mimeType || attachmentModality(mimeType) !== "audio") return;
      const capture = this;
      for (const method of ["arrayBuffer", "blob"] as const) {
        const original: (this: Response) => Promise<ArrayBuffer | Blob> = target[method];
        if (typeof original !== "function") continue;
        Object.defineProperty(target, method, { configurable: true, writable: true, value: function (this: Response, ...args: []) {
          // Borrowed methods must not attach another Response to this call's span.
          if (this !== target) return original.apply(this, args);
          // Preserve the native receiver/brand check and the application's error.
          return original.apply(this, args).then(value => {
            // Preflight at read time (not at hook install), so a late read is judged against the
            // current policy and never holds an install-time reservation past its TTL.
            capture.safely(() => capture.consumed(mimeType, value));
            return value;
          });
        } });
      }
      // Reading the body property neither locks nor reads the stream.
      const body = target.body as ReadableStream<Uint8Array> | null;
      // WHATWG streams only. A node-fetch body is a Node PassThrough: it has [Symbol.asyncIterator]
      // but no getReader(), and its chunk-wise reads are deliberately left unobserved (the auto wire
      // layer, which captures through fetch only, does not observe them either).
      if (body && typeof (body as { getReader?: unknown }).getReader === "function") {
        // Only the first reader or iterator is observed. releaseLock() (or an iterator's
        // return() with preventCancel) does not end the stream, so a later consumer would see
        // only the remaining bytes and must never upload them as whole.
        let observed = false;
        // `for await` and values() lock the stream with an internal reader, never the public
        // getReader(), so each entry point is observed on this body instance.
        const entries: Array<[PropertyKey, "read" | "next", readonly string[]]> = [
          ["getReader", "read", ["releaseLock", "cancel"]],
          ["values", "next", ["return"]],
          [Symbol.asyncIterator, "next", ["return"]],
        ];
        for (const [name, pull, stops] of entries) {
          const method = (body as unknown as Record<PropertyKey, unknown>)[name];
          if (typeof method !== "function") continue;
          Object.defineProperty(body, name, { configurable: true, writable: true, value: function (this: ReadableStream<Uint8Array>, ...args: unknown[]) {
            // A locked body throws here, unchanged, before anything is observed.
            const consumer = (method as (...a: unknown[]) => unknown).apply(this, args);
            if (this === body && !observed) {
              observed = true;
              capture.safely(() => capture.observeChunks(mimeType, consumer, pull, stops));
            }
            return consumer;
          } });
        }
      }
    });
    return response;
  }

  /** A whole body the application itself read, sized from the value it already holds. */
  private consumed(mimeType: string, value: ArrayBuffer | Blob): void {
    const fields: MediaFields = { direction: "output", purpose: "model_output" };
    if (value instanceof ArrayBuffer) {
      const buffer: ArrayBuffer = value; const size = buffer.byteLength;
      this.admit("speech:output", mimeType, "Bytes", size, fields,
        () => ({ mimeType, size, take: max => size <= max ? { kind: "Bytes", data: new Uint8Array(buffer) } : undefined }));
    } else if (typeof Blob !== "undefined" && value instanceof Blob) {
      const blob: Blob = value; const size = blob.size;
      this.admit("speech:output", mimeType, "Blob", size, fields,
        () => ({ mimeType, size, take: max => size <= max ? { kind: "Blob", data: blob } : undefined }));
    }
  }

  /**
   * Forward each chunk the caller's own reader read() or iterator next() resolved to a consumed
   * stream (design 6). Never calls read, next, clone, tee or getReader itself. A whole stream
   * within the cap uploads once at its end; a rejected pull, a non-byte chunk, an over-cap chunk
   * or any stop method (releaseLock/cancel, or an iterator's return() on break) uploads nothing.
   */
  private observeChunks(mimeType: string, consumer: unknown, pull: "read" | "next", stops: readonly string[]): void {
    const manager = this.manager;
    const target = consumer as Record<string, unknown> | null;
    const pullMethod = target?.[pull] as ((...a: unknown[]) => Promise<{ done?: boolean; value?: unknown }>) | undefined;
    if (!manager || !this.correlation || typeof manager.startAttachmentStream !== "function" || !target || typeof pullMethod !== "function" ||
      this.seen.has("speech:output") || this.locallyDenied(mimeType, "output")) return;
    // The whole budget is reserved at admission, so a write never fails mid-stream for budget reasons.
    const stream = manager.startAttachmentStream(this.correlation, { mimeType, direction: "output", purpose: "model_output", maxBytes: MAX_SPEECH_STREAM_BYTES });
    if (stream.reason !== "queued") return;
    let open = true;
    const end = (complete: boolean) => {
      if (!open) return;
      open = false;
      try {
        if (!complete) stream.abort("incomplete");
        else if (stream.finish().admission === "queued") this.seen.add("speech:output");
      } catch { /* bounded */ }
    };
    const define = (name: string, value: (...a: unknown[]) => unknown) => Object.defineProperty(target, name, { configurable: true, writable: true, value });
    // Defined before the consumer is returned: `for await` captures next() when it starts.
    define(pull, function (this: unknown, ...args: unknown[]) {
      const pending = pullMethod.apply(this, args);
      if (this !== target || !open) return pending;
      // The caller receives the same result or rejection; chunks are copied by tryWrite.
      return pending.then(result => {
        if (open) {
          if (result?.done) end(true);
          else if (!(result?.value instanceof Uint8Array) || !stream.tryWrite(result.value)) end(false);
        }
        return result;
      }, error => { end(false); throw error; });
    });
    for (const name of stops) {
      const stop = target[name];
      if (typeof stop === "function") define(name, function (this: unknown, ...args: unknown[]) { if (this === target) end(false); return stop.apply(this, args); });
    }
  }

  /**
   * Mark the context the provider SDK runs in, so wire-level capture (the auto module) and any nested
   * adapter skip this call's media and only this capture uploads it. Called only for calls whose media
   * this capture owns. An inert capture (no manager, or owned elsewhere) returns `ctx` unchanged.
   * A local hide or PII restriction still claims the call: the restriction then holds for every layer.
   */
  claim<C extends AttachmentOwnerContext>(ctx: C): C {
    if (!this.manager) return ctx;
    try { return ctx.setValue(ATTACHMENT_OWNER_KEY, true) as C; } catch { return ctx; }
  }

  private safely(fn: () => void): void { if (this.manager) { try { fn(); } catch { /* Content-free fail-open. */ } } }
}

function routeHost(resource: unknown, hosts: readonly string[]): boolean {
  try {
    const r = record(resource);
    const client = record(r._client ?? r.client);
    const url = new URL(String(client.baseURL));
    return url.protocol === "https:" && hosts.includes(url.hostname);
  } catch { return false; }
}

/**
 * Explicit compatible chat host admission for OpenAI-SDK resources; unknown and
 * text-only routes do not acquire media capture. The OpenAI chat hook itself is
 * owned by the OpenAI adapter, which may consult this allowlist.
 */
export function compatibleMediaRoute(resource: unknown): boolean {
  return routeHost(resource, ["api.openai.com", "api.fireworks.ai", "api.together.ai", "api.x.ai", "openrouter.ai", "api.groq.com", "api.mistral.ai"]);
}

/** OpenAI-SDK audio resources: only hosts whose speech/transcription routes were qualified. */
export function speechMediaRoute(resource: unknown): boolean {
  return routeHost(resource, ["api.openai.com", "api.groq.com"]);
}

/** True only while a configured manager has not been switched off. Tightening still wins later. */
export function attachmentsActive(manager: AttachmentManager | undefined): manager is AttachmentManager {
  try { return !!manager && manager.status().state !== "disabled"; } catch { return false; }
}

export type ProviderMediaOperation = Readonly<{
  operation: string;
  sources: readonly ("inline_base64" | "data_uri" | "bytes" | "blob" | "consumed_response" | "consumed_reader")[];
  modalities: readonly ("image" | "audio" | "document")[];
  directions: readonly ("input" | "output")[];
}>;

/**
 * Typed per-package capability descriptors. Anything not listed (streams, URLs,
 * provider file IDs, async operations, InvokeModel bodies, native OCR/speech
 * routes) is unsupported and never captured.
 */
export const PROVIDER_MEDIA_CAPABILITIES: Readonly<Record<string, readonly ProviderMediaOperation[]>> = Object.freeze({
  "@traceai/google-genai-sdk": [{ operation: "models.generateContent", sources: ["inline_base64"], modalities: ["image", "audio", "document"], directions: ["input", "output"] },
    { operation: "models.generateContentStream", sources: ["inline_base64"], modalities: ["image", "audio", "document"], directions: ["input"] }],
  "@traceai/google-genai": [{ operation: "GenerativeModel.generateContent", sources: ["inline_base64"], modalities: ["image", "audio", "document"], directions: ["input", "output"] }],
  "@traceai/fi-instrumentation-vertexai": [{ operation: "GenerativeModel.generateContent", sources: ["inline_base64"], modalities: ["image", "audio", "document"], directions: ["input", "output"] }],
  "@traceai/bedrock": [{ operation: "ConverseCommand", sources: ["bytes"], modalities: ["image", "document"], directions: ["input"] }],
  "@traceai/groq": [{ operation: "chat.completions.create", sources: ["data_uri"], modalities: ["image"], directions: ["input"] },
    { operation: "audio.transcriptions.create", sources: ["blob"], modalities: ["audio"], directions: ["input"] },
    { operation: "audio.translations.create", sources: ["blob"], modalities: ["audio"], directions: ["input"] },
    { operation: "audio.speech.create", sources: ["consumed_response", "consumed_reader"], modalities: ["audio"], directions: ["output"] }],
  "@traceai/mistral": [{ operation: "chat.complete", sources: ["data_uri"], modalities: ["image"], directions: ["input"] }],
  "@traceai/portkey": [{ operation: "chat.completions.create", sources: ["data_uri", "inline_base64"], modalities: ["image", "audio"], directions: ["input"] }],
  "@traceai/vllm": [{ operation: "chat.completions.create", sources: ["data_uri", "inline_base64"], modalities: ["image", "audio"], directions: ["input"] }],
  "@traceai/openai": [{ operation: "audio.transcriptions.create", sources: ["blob"], modalities: ["audio"], directions: ["input"] },
    { operation: "audio.translations.create", sources: ["blob"], modalities: ["audio"], directions: ["input"] },
    { operation: "audio.speech.create", sources: ["consumed_response", "consumed_reader"], modalities: ["audio"], directions: ["output"] }],
});
