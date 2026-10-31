import { AttributeValue } from "@opentelemetry/api";
import { REDACTED_VALUE } from "./constants";

const MESSAGE_METADATA = ["role", "name", "id", "tool_call_id", "finish_reason"];
const PART_METADATA = ["type", "id", "name", "modality", "mime_type"];
const IMAGE_TYPES = ["image", "image_url", "input_image"];
const TEXT_TYPES = ["text", "input_text", "output_text", "reasoning", "compaction"];
const MEDIA_TYPES = ["blob", "file", "uri"];

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// Only scalar metadata is safe to retain. Unknown extension fields and structured
// payloads (including tool arguments/results) may contain text or images.
function redactFields(value: JsonObject, keep: readonly string[]): JsonObject {
  return Object.fromEntries(Object.entries(value).map(([key, field]) => [
    key,
    keep.includes(key) && (field === null || typeof field !== "object")
      ? field : REDACTED_VALUE,
  ]));
}

function isImage(part: JsonObject): boolean {
  return (typeof part.type === "string" && IMAGE_TYPES.includes(part.type)) || part.modality === "image" ||
    (typeof part.mime_type === "string" && /^image\//i.test(part.mime_type));
}

function redactParts(parts: unknown, hideText: boolean, hideImages: boolean): unknown {
  if (!Array.isArray(parts)) return REDACTED_VALUE;
  return parts
    .filter(part => !(hideImages && isObject(part) && isImage(part)))
    .map(part => {
      if (!isObject(part)) return REDACTED_VALUE;
      const type = typeof part.type === "string" ? part.type : "";
      if (!hideText && TEXT_TYPES.includes(type)) {
        return redactFields(part, [...PART_METADATA, "content", "text"]);
      }
      // Image-only hiding retains explicitly non-image media. Missing/unknown
      // modality fails closed rather than guessing what opaque data contains.
      if (!hideText && MEDIA_TYPES.includes(type) &&
          ((typeof part.modality === "string" && ["audio", "video", "document"].includes(part.modality)) ||
           (typeof part.mime_type === "string" && /^(audio|video|text)\//i.test(part.mime_type)))) {
        return redactFields(part, [...PART_METADATA, "content", "uri", "file_id"]);
      }
      return redactFields(part, PART_METADATA);
    });
}

/**
 * Mask both legacy content messages and native OTel GenAI role/parts messages.
 * Text hiding conservatively redacts all payloads, including opaque tool/media
 * data. Image-only hiding retains known text/non-image media. Unknown shapes
 * fail closed; no arbitrary payload is recursively inspected or copied through.
 */
export function redactMessages({ value, hideText, hideImages }: {
  value: AttributeValue;
  hideText: boolean;
  hideImages: boolean;
}): AttributeValue {
  if (typeof value !== "string") return REDACTED_VALUE;
  try {
    const messages: unknown = JSON.parse(value);
    if (!Array.isArray(messages) || !messages.every(isObject)) return REDACTED_VALUE;
    return JSON.stringify(messages.map(message => {
      const redacted = redactFields(message, MESSAGE_METADATA);
      if ("content" in message) {
        // Preserve the legacy contract: text hiding redacts the entire content,
        // including a multimodal array, not just string content.
        redacted.content = hideText ? REDACTED_VALUE :
          typeof message.content === "string" ? message.content :
          redactParts(message.content, hideText, hideImages);
      }
      if ("parts" in message) {
        redacted.parts = redactParts(message.parts, hideText, hideImages);
      }
      return redacted;
    }));
  } catch {
    return REDACTED_VALUE;
  }
}
