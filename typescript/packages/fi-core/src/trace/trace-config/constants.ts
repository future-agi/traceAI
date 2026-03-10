import { TraceConfigKey, TraceConfig, TraceConfigFlag } from "./types";

/** Hides input value & messages */
export const FI_HIDE_INPUTS = "FI_HIDE_INPUTS";
/** Hides output value & messages */
export const FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS";
/** Hides all input messages */
export const FI_HIDE_INPUT_MESSAGES =
  "FI_HIDE_INPUT_MESSAGES";
/** Hides all output messages */
export const FI_HIDE_OUTPUT_MESSAGES =
  "FI_HIDE_OUTPUT_MESSAGES";
/** Hides images from input messages */
export const FI_HIDE_INPUT_IMAGES =
  "FI_HIDE_INPUT_IMAGES";
/** Hides text from input messages */
export const FI_HIDE_INPUT_TEXT = "FI_HIDE_INPUT_TEXT";
/** Hides text from output messages */
export const FI_HIDE_OUTPUT_TEXT = "FI_HIDE_OUTPUT_TEXT";
/** Hides embedding vectors */
export const FI_HIDE_EMBEDDING_VECTORS =
  "FI_HIDE_EMBEDDING_VECTORS";
/** Limits characters of a base64 encoding of an image */
export const FI_BASE64_IMAGE_MAX_LENGTH =
  "FI_BASE64_IMAGE_MAX_LENGTH";
/** Enables regex-based PII redaction on attribute values */
export const FI_PII_REDACTION = "FI_PII_REDACTION";

export const DEFAULT_HIDE_INPUTS = false;
export const DEFAULT_HIDE_OUTPUTS = false;

export const DEFAULT_HIDE_INPUT_MESSAGES = false;
export const DEFAULT_HIDE_OUTPUT_MESSAGES = false;

export const DEFAULT_HIDE_INPUT_IMAGES = false;
export const DEFAULT_HIDE_INPUT_TEXT = false;
export const DEFAULT_HIDE_OUTPUT_TEXT = false;

export const DEFAULT_HIDE_EMBEDDING_VECTORS = false;
export const DEFAULT_BASE64_IMAGE_MAX_LENGTH = 32000;
export const DEFAULT_PII_REDACTION = false;

/** When a value is hidden, it will be replaced by this redacted value */
export const REDACTED_VALUE = "__REDACTED__";

/**
 * The default, environment, and type information for each value on the TraceConfig
 * Used to generate a full TraceConfig object with the correct types and default values
 */
export const traceConfigMetadata: Readonly<
  Record<TraceConfigKey, TraceConfigFlag>
> = {
  hideInputs: {
    default: DEFAULT_HIDE_INPUTS,
    envKey: FI_HIDE_INPUTS,
    type: "boolean",
  },
  hideOutputs: {
    default: DEFAULT_HIDE_OUTPUTS,
    envKey: FI_HIDE_OUTPUTS,
    type: "boolean",
  },
  hideInputMessages: {
    default: DEFAULT_HIDE_INPUT_MESSAGES,
    envKey: FI_HIDE_INPUT_MESSAGES,
    type: "boolean",
  },
  hideOutputMessages: {
    default: DEFAULT_HIDE_OUTPUT_MESSAGES,
    envKey: FI_HIDE_OUTPUT_MESSAGES,
    type: "boolean",
  },
  hideInputImages: {
    default: DEFAULT_HIDE_INPUT_IMAGES,
    envKey: FI_HIDE_INPUT_IMAGES,
    type: "boolean",
  },
  hideInputText: {
    default: DEFAULT_HIDE_INPUT_TEXT,
    envKey: FI_HIDE_INPUT_TEXT,
    type: "boolean",
  },
  hideOutputText: {
    default: DEFAULT_HIDE_OUTPUT_TEXT,
    envKey: FI_HIDE_OUTPUT_TEXT,
    type: "boolean",
  },
  hideEmbeddingVectors: {
    default: DEFAULT_HIDE_EMBEDDING_VECTORS,
    envKey: FI_HIDE_EMBEDDING_VECTORS,
    type: "boolean",
  },
  base64ImageMaxLength: {
    default: DEFAULT_BASE64_IMAGE_MAX_LENGTH,
    envKey: FI_BASE64_IMAGE_MAX_LENGTH,
    type: "number",
  },
  piiRedaction: {
    default: DEFAULT_PII_REDACTION,
    envKey: FI_PII_REDACTION,
    type: "boolean",
  },
};

export const DefaultTraceConfig: TraceConfig = {
  hideInputs: DEFAULT_HIDE_INPUTS,
  hideOutputs: DEFAULT_HIDE_OUTPUTS,
  hideInputMessages: DEFAULT_HIDE_INPUT_MESSAGES,
  hideOutputMessages: DEFAULT_HIDE_OUTPUT_MESSAGES,
  hideInputImages: DEFAULT_HIDE_INPUT_IMAGES,
  hideInputText: DEFAULT_HIDE_INPUT_TEXT,
  hideOutputText: DEFAULT_HIDE_OUTPUT_TEXT,
  hideEmbeddingVectors: DEFAULT_HIDE_EMBEDDING_VECTORS,
  base64ImageMaxLength: DEFAULT_BASE64_IMAGE_MAX_LENGTH,
  piiRedaction: DEFAULT_PII_REDACTION,
};