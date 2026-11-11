import type { Span } from "@opentelemetry/api";
import type { TraceConfig } from "@traceai/fi-core";
import type { AttachmentManager } from "@traceai/fi-core/attachments";
import { record, safely } from "./operation.js";

export type ElevenLabsInputAudioMimeType =
  | "audio/wav"
  | "audio/mpeg"
  | "audio/ogg"
  | "audio/mp4"
  | "audio/webm"
  | "audio/flac";

// This is a bounded complete-response path, NOT a recording/stream accumulator.
const MAX_DECODED_BYTES = 256 * 1024;
const MAX_ENCODED_CHARACTERS = Math.ceil(MAX_DECODED_BYTES / 3) * 4;
const INPUT_TYPES: readonly string[] = [
  "audio/wav", "audio/mpeg", "audio/ogg", "audio/mp4", "audio/webm", "audio/flac",
];

/** Adapters never own an uploader or change the provider manager's lifecycle. */
export class SpeechAttachments {
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
      // Raw PCM/ulaw require sample-format metadata that the core does not yet
      // carry. Do not label those bytes MP3. The SDK's default is mp3_44100_128.
      if (this.outputFormat !== undefined &&
          !/^mp3_(22050_32|24000_48|44100_(32|64|96|128|192))$/.test(this.outputFormat)) {
        this.span.setAttribute("elevenlabs.attachment.output", "source_unavailable");
        return;
      }
      const result = this.manager.attach(this.span, {
        kind: "Bytes",
        get data(): Uint8Array {
          const encoded = record(value).audioBase64;
          if (typeof encoded !== "string" || encoded.length === 0 ||
              encoded.length > MAX_ENCODED_CHARACTERS) return new Uint8Array(0);
          // Buffer.from tolerates malformed base64; require canonical padding and
          // alphabet first, then a round-trip before retaining decoded bytes.
          if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) {
            throw new Error("unsupported_audio_source");
          }
          const decoded = Buffer.from(encoded, "base64");
          if (decoded.length > MAX_DECODED_BYTES) return new Uint8Array(0);
          if (decoded.toString("base64") !== encoded) throw new Error("unsupported_audio_source");
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
}
