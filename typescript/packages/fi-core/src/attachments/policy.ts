import type { TraceConfigOptions } from "../trace/trace-config/types";
import type { AttachmentAdmission, AttachmentConfig, AttachmentModality, AttachmentOptions, AttachmentServerPolicy, AttachmentSourceKind } from "./types";

const MIME_MODALITIES: Readonly<Record<string, AttachmentModality>> = Object.freeze({
  "audio/wav": "audio", "audio/ogg": "audio", "audio/mpeg": "audio", "audio/mp4": "audio", "audio/webm": "audio", "audio/flac": "audio", "audio/L16": "audio",
  "image/png": "image", "image/jpeg": "image", "image/webp": "image", "video/mp4": "video", "video/webm": "video",
  "application/pdf": "document", "application/octet-stream": "file",
});
export function attachmentModality(mime: string): AttachmentModality | undefined {
  return Object.prototype.hasOwnProperty.call(MIME_MODALITIES, mime) ? MIME_MODALITIES[mime] : undefined;
}
const enabledValue = (value: string | undefined) => /^(true|1|yes)$/i.test(value?.trim() ?? "");
const restrictiveValue = (value: string | undefined) => value !== undefined && !/^(false|0|no)$/i.test(value.trim());
export function attachmentEnabled(config: AttachmentConfig, env: Readonly<Record<string, string | undefined>>): boolean {
  return config.enabled !== false && (config.enabled === true || enabledValue(env.FI_ATTACHMENTS_ENABLED)) &&
    (env.FI_ATTACHMENTS_ENABLED === undefined || enabledValue(env.FI_ATTACHMENTS_ENABLED));
}
/** Pure admission. This function never accesses, copies, decodes or hashes a source. */
export function evaluateAttachmentPolicy(config: AttachmentConfig, traceConfig: TraceConfigOptions,
  server: AttachmentServerPolicy | undefined, sourceKind: AttachmentSourceKind, options: AttachmentOptions,
  env: Readonly<Record<string, string | undefined>> = {}): AttachmentAdmission | undefined {
  if (!attachmentEnabled(config, env)) return "disabled";
  if (!server) return "bootstrap_pending";
  if (!server.enabled) return "policy_denied";
  const capture = options.capture ?? {};
  for (const key of ["captureContent", "recordInputs", "recordOutputs", "muted", "temporaryChat", "requested", "modelInput", "containsInput", "containsOutput", "errorDetail", "newRecording"] as const) {
    if (capture[key] !== undefined && typeof capture[key] !== "boolean") return "policy_denied";
  }
  if (capture.captureContent === false || capture.requested === false || capture.muted || capture.temporaryChat) return "policy_denied";
  if (!["input", "output", "mixed"].includes(options.direction)) return "policy_denied";
  const input = options.direction !== "output" || capture.containsInput || capture.modelInput || options.purpose === "model_input" || options.purpose === "computer_screenshot";
  const output = options.direction !== "input" || capture.containsOutput;
  if ((input && capture.recordInputs === false) || (output && capture.recordOutputs === false)) return "policy_denied";
  const hidden = (key: keyof TraceConfigOptions, name: string) => (traceConfig[key] !== undefined && traceConfig[key] !== false) || restrictiveValue(env[name]);
  const inputHidden = hidden("hideInputs", "FI_HIDE_INPUTS") || hidden("hideInputMessages", "FI_HIDE_INPUT_MESSAGES") || hidden("hideInputText", "FI_HIDE_INPUT_TEXT");
  const outputHidden = hidden("hideOutputs", "FI_HIDE_OUTPUTS") || hidden("hideOutputMessages", "FI_HIDE_OUTPUT_MESSAGES") || hidden("hideOutputText", "FI_HIDE_OUTPUT_TEXT");
  if ((input && inputHidden) || (output && outputHidden)) return "policy_denied";
  const modality = attachmentModality(options.mimeType);
  if (!modality) return "source_unavailable";
  if (input && modality === "image" && hidden("hideInputImages", "FI_HIDE_INPUT_IMAGES")) return "policy_denied";
  const errorHidden = hidden("hideErrorDetails", "FI_HIDE_ERROR_DETAILS") ||
    (traceConfig.hideErrorDetails === undefined && env.FI_HIDE_ERROR_DETAILS === undefined && (inputHidden || outputHidden));
  if ((capture.errorDetail || options.purpose === "diagnostic") && errorHidden) return "policy_denied";
  // No local qualified media sanitizer ships with the core. Never trust a caller's "sanitized" flag.
  if (server.require_media_redaction || server.require_metadata_removal) return "redaction_unavailable";
  const unredacted = config.allowUnredactedMediaWithTextPii === true ||
    (config.allowUnredactedMediaWithTextPii === undefined && enabledValue(env.FI_ATTACHMENTS_ALLOW_UNREDACTED_MEDIA_WITH_TEXT_PII));
  if (hidden("piiRedaction", "FI_PII_REDACTION") && !(unredacted && server.allow_unredacted_media_with_text_pii)) return "redaction_unavailable";
  if (!server.allowed_modalities.includes(modality) || !server.allowed_sources.includes(sourceKind)) return "policy_denied";
  if (capture.newRecording) {
    const key = modality === "audio" ? "recordAudio" : modality === "video" ? "recordVideo" : undefined;
    if (!key) return "recording_disabled";
    const name = key === "recordAudio" ? "FI_ATTACHMENTS_RECORD_AUDIO" : "FI_ATTACHMENTS_RECORD_VIDEO";
    const permitted = key === "recordAudio" ? server.record_audio : server.record_video;
    if (!permitted || config[key] === false || !(config[key] === true || enabledValue(env[name])) ||
      (env[name] !== undefined && !enabledValue(env[name]))) return "recording_disabled";
  }
  return undefined;
}
