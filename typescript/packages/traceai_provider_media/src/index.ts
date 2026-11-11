import { generateTraceConfig, type TraceConfigOptions } from "@traceai/fi-core";
import { attachmentModality, type AttachmentManager, type AttachmentSpan, type AttachmentOptions, type AttachmentSource, type AttachmentCorrelation } from "@traceai/fi-core/attachments";

export interface ProviderMediaOptions {
  /** The provider-owned manager returned by register(). Never create an uploader per integration. */
  attachments?: AttachmentManager;
  traceConfig?: TraceConfigOptions;
}

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
// Bound synchronous adapter scratch independently of the manager queue. Larger media
// needs the core's future lazy-source/stream API, not an unbounded decoding callback.
const MAX_DECODED_BYTES = 1024 * 1024;
const MAX_PARTS = 64;
const seen = new WeakMap<AttachmentManager, WeakMap<object, Set<string>>>();

/** Typed extraction only. No recursion, URL fetch, provider file lookup, or filesystem access. */
export class ProviderMediaCapture {
  private readonly manager?: AttachmentManager;
  private readonly correlation?: AttachmentCorrelation;
  private readonly local: TraceConfigOptions;
  private readonly seen: Set<string>;
  private attempted = 0;

  constructor(span: AttachmentSpan, options: ProviderMediaOptions = {}) {
    this.manager = options.attachments;
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

  private submit(key: string, source: AttachmentSource, options: AttachmentOptions): void {
    if (!this.manager || !this.correlation || this.attempted++ >= MAX_PARTS || this.seen.has(key)) return;
    try {
      const config = generateTraceConfig(this.local);
      const input = options.direction !== "output";
      // Wrapper-local restrictions intersect the shared manager policy, never replace it.
      if (config.piiRedaction || (input
        ? config.hideInputs || config.hideInputMessages || config.hideInputText || (attachmentModality(options.mimeType) === "image" && config.hideInputImages)
        : config.hideOutputs || config.hideOutputMessages || config.hideOutputText)) return;
      const result = this.manager.attach(this.correlation, source, options);
      // Denied observations are not retained as per-part diagnostics or references.
      if (result.admission === "queued") this.seen.add(key);
    } catch { /* Never leak provider payloads or replace application behavior. */ }
  }

  private base64(key: string, read: () => unknown, options: AttachmentOptions): void {
    this.submit(key, { kind: "Bytes", get data() {
      // AttachmentManager does not access this getter until all policy, identity,
      // sampling and descriptor gates have passed. Never decode during extraction.
      const value = read();
      if (typeof value !== "string" || !value.length || value.length > Math.ceil(MAX_DECODED_BYTES / 3) * 4 ||
        value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error("source_unavailable");
      const decoded = Buffer.from(value, "base64");
      if (decoded.byteLength > MAX_DECODED_BYTES) throw new Error("source_unavailable");
      return decoded;
    } }, options);
  }

  private dataUri(key: string, read: () => unknown, options: Omit<AttachmentOptions, "mimeType">): void {
    // MIME is a typed descriptor; payload slicing/decoding occurs only inside the
    // admitted getter. Do not retain a remote/signed URL as fallback metadata.
    for (const mimeType of ["image/png", "image/jpeg", "image/webp"]) {
      this.base64(`${key}:${mimeType}`, () => {
        const value = read();
        const prefix = `data:${mimeType};base64,`;
        return typeof value === "string" && value.startsWith(prefix) ? value.slice(prefix.length) : undefined;
      }, { ...options, mimeType });
    }
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
          this.base64(`gemini:${direction}:${m}:${p}`, () => inline.data, {
            mimeType: inline.mimeType, direction, purpose: direction === "input" ? "model_input" : "model_output",
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

  /** Compatible chat messages: inline image URL and typed input_audio only. */
  compatibleMessages(messages: unknown, dialect: "openai" | "mistral" = "openai"): void {
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
          } else if (part.type === "input_audio") {
            const audio = record(part.input_audio);
            const mimeType = audio.format === "wav" ? "audio/wav" : audio.format === "mp3" ? "audio/mpeg" : undefined;
            if (mimeType) this.base64(`chat:${m}:${p}`, () => audio.data, { ...options, mimeType });
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
          const media = record(part.image ?? part.document);
          const mimeType = part.image ? ({ png: "image/png", jpeg: "image/jpeg", webp: "image/webp" } as Record<string, string>)[String(media.format)]
            : media.format === "pdf" ? "application/pdf" : undefined;
          if (!mimeType) continue;
          this.submit(`bedrock:${m}:${p}`, { kind: "Bytes", get data() { return record(media.source).bytes as Uint8Array; } }, {
            mimeType, direction: "input", purpose: "model_input", messageIndex: m, partIndex: p,
          });
        }
      }
    });
  }

  /** Immutable File/Blob input only; never advance fs.ReadStream or fetch Response inputs. */
  speechInput(file: unknown): void {
    this.safely(() => {
      if (typeof Blob === "undefined" || !(file instanceof Blob) || attachmentModality(file.type) !== "audio") return;
      this.submit("speech:input", { kind: "Blob", data: file }, { direction: "input", mimeType: file.type, purpose: "model_input" });
    });
  }

  /** Observe only caller-requested whole-body reads. Unread/cancelled/stream reads upload nothing. */
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
            capture.safely(() => {
              const source: AttachmentSource = value instanceof ArrayBuffer
                ? { kind: "Bytes", data: new Uint8Array(value) }
                : { kind: "Blob", data: value as Blob };
              capture.submit("speech:output", source, { direction: "output", mimeType, purpose: "model_output" });
            });
            return value;
          });
        } });
      }
    });
    return response;
  }

  private safely(fn: () => void): void { if (this.manager) { try { fn(); } catch { /* Content-free fail-open. */ } } }
}

/** Explicit compatible host admission; unknown/text-only routes do not acquire media capture. */
export function compatibleMediaRoute(resource: unknown): boolean {
  try {
    const r = record(resource);
    const client = record(r._client ?? r.client);
    const url = new URL(String(client.baseURL));
    return url.protocol === "https:" && ["api.openai.com", "api.fireworks.ai", "api.together.ai", "api.x.ai", "openrouter.ai", "api.groq.com", "api.mistral.ai"].includes(url.hostname);
  } catch { return false; }
}
