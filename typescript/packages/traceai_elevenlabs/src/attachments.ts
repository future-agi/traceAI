import type { Span } from "@opentelemetry/api";
import type { TraceConfig } from "@traceai/fi-core";
import type { AttachmentManager, AttachmentStream } from "@traceai/fi-core/attachments";
import { record, safely } from "./operation.js";

export type ElevenLabsInputAudioMimeType =
  | "audio/wav"
  | "audio/mpeg"
  | "audio/ogg"
  | "audio/mp4"
  | "audio/webm"
  | "audio/flac";

// A bounded complete-response path for convertWithTimestamps (one JSON value).
const MAX_DECODED_BYTES = 256 * 1024;
/**
 * Consumed-stream cap. The manager reserves the whole cap when a stream is admitted, so it
 * also bounds per-call memory. About 4.4 minutes of the SDK's default 128 kbit/s MP3. A longer
 * response uploads nothing (`too_large`); a truncated object is never uploaded as complete.
 */
export const ELEVENLABS_STREAM_MAX_BYTES = 4 * 1024 * 1024;
const INPUT_TYPES: readonly string[] = [
  "audio/wav", "audio/mpeg", "audio/ogg", "audio/mp4", "audio/webm", "audio/flac",
];
const MP3_FORMAT = /^mp3_(22050_32|24000_48|44100_(32|64|96|128|192))$/;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

/** Canonical, bounded base64 only. Buffer.from tolerates malformed input, so check first. */
function decode(encoded: unknown, maxBytes: number): Uint8Array | undefined {
  if (typeof encoded !== "string" || encoded.length === 0 ||
      encoded.length > Math.ceil(maxBytes / 3) * 4) return undefined;
  if (encoded.length % 4 !== 0 || !BASE64.test(encoded)) return undefined;
  const decoded = Buffer.from(encoded, "base64");
  if (decoded.length === 0 || decoded.length > maxBytes) return undefined;
  if (decoded.toString("base64") !== encoded) return undefined;
  return decoded;
}

/**
 * Adapters never own an uploader or change the provider manager's lifecycle. Every capture
 * goes through the shared manager's admission before any read, decode or copy.
 */
export class SpeechAttachments {
  private stream?: AttachmentStream;
  private streamState: "idle" | "open" | "closed" = "idle";

  constructor(
    private readonly manager: AttachmentManager,
    private readonly span: Span,
    private readonly policy: TraceConfig,
    private readonly outputFormat: string | undefined,
  ) {}

  private allowed(direction: "input" | "output"): boolean {
    // Adapter-local restrictions can only tighten the shared manager's policy.
    // No qualified speech sanitizer is available here.
    return !this.policy.piiRedaction && (direction === "input"
      ? !(this.policy.hideInputs || this.policy.hideInputMessages || this.policy.hideInputText)
      : !(this.policy.hideOutputs || this.policy.hideOutputMessages || this.policy.hideOutputText));
  }

  /** Raw PCM/ulaw lack sample-format metadata in v1; never label those bytes MP3. */
  private mp3(): boolean {
    return this.outputFormat === undefined || MP3_FORMAT.test(this.outputFormat);
  }

  input(request: unknown, mimeType: ElevenLabsInputAudioMimeType): void {
    safely(() => {
      if (!INPUT_TYPES.includes(mimeType)) return;
      const result = this.manager.attach(this.span, {
        kind: "Blob",
        // Core checks default-off, server policy, PII, scope and sampling BEFORE
        // this getter. No file/stream property is inspected on a denied capture.
        get data(): Blob {
          const file = record(request).file;
          if (!(file instanceof Blob) || file.type !== mimeType) {
            throw new Error("unsupported_audio_source");
          }
          return file;
        },
      }, {
        mimeType,
        direction: "input",
        purpose: "model_input",
        capture: { captureContent: this.allowed("input") },
      });
      this.span.setAttribute("elevenlabs.attachment.input", result.admission);
    });
  }

  timestamped(value: unknown): void {
    safely(() => {
      if (!this.mp3()) {
        this.span.setAttribute("elevenlabs.attachment.output", "source_unavailable");
        return;
      }
      const result = this.manager.attach(this.span, {
        kind: "Bytes",
        get data(): Uint8Array {
          const decoded = decode(record(value).audioBase64, MAX_DECODED_BYTES);
          if (!decoded) throw new Error("unsupported_audio_source");
          return decoded;
        },
      }, {
        mimeType: "audio/mpeg",
        direction: "output",
        purpose: "model_output",
        capture: { captureContent: this.allowed("output") },
      });
      this.span.setAttribute("elevenlabs.attachment.output", result.admission);
    });
  }

  /**
   * Admit the consumed stream once per call, before any byte is copied or decoded. A denied
   * admission retains nothing and is never retried for this call.
   */
  private open(): AttachmentStream | undefined {
    if (this.streamState === "idle") {
      this.streamState = "closed";
      if (!this.mp3()) {
        this.span.setAttribute("elevenlabs.attachment.output", "source_unavailable");
        return undefined;
      }
      const stream = this.manager.startAttachmentStream(this.span, {
        mimeType: "audio/mpeg",
        direction: "output",
        purpose: "model_output",
        maxBytes: ELEVENLABS_STREAM_MAX_BYTES,
        capture: { captureContent: this.allowed("output") },
      });
      if (stream.reason !== "queued") {
        this.span.setAttribute("elevenlabs.attachment.output", stream.reason);
        return undefined;
      }
      this.stream = stream;
      this.streamState = "open";
    }
    return this.streamState === "open" ? this.stream : undefined;
  }

  /**
   * Observe one chunk the APPLICATION already pulled through the instrumentation-owned bridge
   * (design 6.4). tryWrite copies synchronously, so a BYOB view may be handed back right after.
   */
  chunk(bytes: Uint8Array): void {
    safely(() => {
      if (this.streamState === "closed" || !(bytes instanceof Uint8Array) || bytes.byteLength === 0) return;
      const stream = this.open();
      if (stream && !stream.tryWrite(bytes)) this.close("incomplete");
    });
  }

  /** One base64 chunk of a consumed timestamp stream; decoded only after admission. */
  timestampChunk(value: unknown): void {
    safely(() => {
      if (this.streamState === "closed") return;
      const encoded = record(value).audioBase64;
      if (typeof encoded !== "string" || encoded.length === 0) return;
      const stream = this.open();
      if (!stream) return;
      const decoded = decode(encoded, ELEVENLABS_STREAM_MAX_BYTES);
      // A malformed or oversized chunk means the object would be incomplete: upload nothing.
      if (!decoded || !stream.tryWrite(decoded)) this.close("incomplete");
    });
  }

  /**
   * Called by the operation BEFORE its span ends, so a finished stream's reference is
   * projected on a recording span. Anything but a complete read aborts: nothing uploads.
   */
  close(completion: "complete" | "incomplete"): void {
    safely(() => {
      const stream = this.stream;
      const wasOpen = this.streamState === "open";
      this.stream = undefined;
      this.streamState = "closed";
      if (!stream || !wasOpen) return;
      if (completion === "complete") {
        const handle = stream.finish();
        this.span.setAttribute("elevenlabs.attachment.output", handle.admission);
      } else {
        stream.abort("incomplete");
        this.span.setAttribute("elevenlabs.attachment.output", stream.reason);
      }
    });
  }
}
