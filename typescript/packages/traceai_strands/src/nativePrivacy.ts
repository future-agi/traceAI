import { diag, type Attributes } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { generateTraceConfig, maskAttributes, REDACTED_VALUE } from "@traceai/fi-core";

const INPUT = new Set([
  "gen_ai.input.messages", "gen_ai.agent.input", "gen_ai.tool.call.arguments",
  "gen_ai.system_instructions", "system_prompt", "input.value", "input.mime_type",
  "raw.input", "llm.prompt_template.variables",
]);
const OUTPUT = new Set([
  "gen_ai.output.messages", "gen_ai.tool.call.result", "output.value", "output.mime_type", "raw.output",
]);

/** One privacy policy for native attributes, events, links and their FI projections. */
export class NativePrivacy {
  readonly config = generateTraceConfig();
  readonly captureErrors: boolean;
  private readonly hideInput: boolean;
  private readonly hideOutput: boolean;
  private readonly restrictCustom: boolean;

  constructor(captureErrors: boolean) {
    this.captureErrors = captureErrors && !this.config.hideErrorDetails;
    const cap = this.config.base64ImageMaxLength;
    const valid = typeof cap === "number" && Number.isSafeInteger(cap) && cap >= 0;
    if (!valid) diag.warn("Invalid base64 image cap; using 0.");
    this.config = { ...this.config, base64ImageMaxLength: valid ? cap : 0, hideErrorDetails: !this.captureErrors };
    // Native payloads contain nested parts and tool arguments, unlike FI message
    // blobs. Hide the whole direction rather than leave an unmasked alternate copy.
    this.hideInput = this.config.hideInputs || this.config.hideInputMessages ||
      this.config.hideInputText || this.config.hideInputImages;
    this.hideOutput = this.config.hideOutputs || this.config.hideOutputMessages || this.config.hideOutputText;
    this.restrictCustom = this.hideInput || this.hideOutput;
  }

  attributes(attributes: Attributes, metadata: (key: string) => boolean,
    direction?: "input" | "output" | "error", failedTool = false, successfulTool = false): Attributes {
    const result: Attributes = {};
    for (const [key, value] of Object.entries(attributes)) {
      if (metadata(key)) { result[key] = value; continue; }
      const side = INPUT.has(key) ? "input" : OUTPUT.has(key) ? "output" :
        /(^|\.)(exception\.|error\.|error_message$)/.test(key) ? "error" : direction;
      if ((side === "input" && this.hideInput) || (side === "output" && this.hideOutput)) continue;
      // An unclassified custom payload may copy either hidden direction.
      if (this.restrictCustom && !side) continue;
      if (!this.captureErrors && side === "error") continue;
      result[key] = !this.captureErrors && failedTool && side === "output"
        ? REDACTED_VALUE : this.value(value, successfulTool) as Attributes[string];
    }
    return maskAttributes({ config: this.config, attributes: result });
  }

  events(span: ReadableSpan, captureContent: boolean): ReadableSpan["events"] {
    const failedTool = span.attributes["gen_ai.tool.status"] === "error";
    const successfulTool = span.attributes["gen_ai.tool.status"] === "success";
    return span.events.flatMap((event) => {
      if (event.name === "exception") {
        return this.captureErrors ? [{ ...event, attributes: this.attributes(event.attributes ?? {}, () => false) }] : [];
      }
      if (!captureContent) return [];
      const direction = event.name === "gen_ai.choice" ? "output" :
        /^gen_ai\.(user|assistant|system|tool|message)\.message$/.test(event.name) || event.name === "gen_ai.message" ? "input" : undefined;
      return [{ ...event, attributes: this.attributes(event.attributes ?? {}, () => false,
        direction, failedTool, successfulTool) }];
    });
  }

  private value(value: unknown, successfulTool: boolean, depth = 0): unknown {
    if (depth > 32) return REDACTED_VALUE;
    if (typeof value === "string") {
      // Reuse the public core seam at zero; do not maintain a second URI grammar.
      // PII remains a final attributes pass, after native error/content handling.
      const retained = this.config.base64ImageMaxLength === 0
        ? maskAttributes({ config: { ...this.config, piiRedaction: false }, attributes: { value } }).value as string
        : value;
      if (this.config.base64ImageMaxLength === 0 && retained === "[image-redacted]") return retained;
      if (/^data:image\/[^;]+;base64,/.test(retained) && retained.length > this.config.base64ImageMaxLength) return REDACTED_VALUE;
      if (/^\s*[\[{]/.test(retained)) {
        // Truncated/malformed native JSON cannot be safely inspected for errors.
        if (retained.length > 1_048_576) return REDACTED_VALUE;
        try { return JSON.stringify(this.value(JSON.parse(retained), successfulTool, depth + 1)); }
        catch { return REDACTED_VALUE; }
      }
      return retained;
    }
    if (Array.isArray(value)) return value.map((part) => this.value(part, successfulTool, depth + 1));
    if (value && typeof value === "object") {
      const object = value as Record<string, unknown>;
      const errorResult = !this.captureErrors && object.status === "error";
      // Latest semconv drops tool-result status in conversation history. Without
      // that evidence, a response cannot safely be treated as a successful result.
      const ambiguousResult = !this.captureErrors && object.type === "tool_call_response" && !successfulTool;
      return Object.fromEntries(Object.entries(object).map(([key, child]) => [key,
        (errorResult && ["content", "response", "message", "output"].includes(key)) || (ambiguousResult && key === "response")
          ? REDACTED_VALUE : this.value(key === "image" ? this.image(child) : child, successfulTool, depth + 1),
      ]));
    }
    return value;
  }

  private image(value: unknown): unknown {
    // ImageBlock.toJSON() uses image.source.bytes with bare base64, not a data
    // URL. Sanitize before projection so tool results and history share the cap.
    if (!value || typeof value !== "object") return value;
    const image = value as Record<string, unknown>;
    if (!image.source || typeof image.source !== "object") return value;
    const source = image.source as Record<string, unknown>;
    if (typeof source.bytes !== "string" || source.bytes.length <= this.config.base64ImageMaxLength) return value;
    return { ...image, source: { ...source, bytes: this.config.base64ImageMaxLength === 0 ? "[image-redacted]" : REDACTED_VALUE } };
  }
}
