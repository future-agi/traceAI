import { TraceConfigKey, TraceConfig, TraceConfigFlag } from "./types";
/** Hides input value & messages */
export declare const FI_HIDE_INPUTS = "FI_HIDE_INPUTS";
/** Hides output value & messages */
export declare const FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS";
/** Hides all input messages */
export declare const FI_HIDE_INPUT_MESSAGES = "FI_HIDE_INPUT_MESSAGES";
/** Hides all output messages */
export declare const FI_HIDE_OUTPUT_MESSAGES = "FI_HIDE_OUTPUT_MESSAGES";
/** Hides images from input messages */
export declare const FI_HIDE_INPUT_IMAGES = "FI_HIDE_INPUT_IMAGES";
/** Hides text from input messages */
export declare const FI_HIDE_INPUT_TEXT = "FI_HIDE_INPUT_TEXT";
/** Hides text from output messages */
export declare const FI_HIDE_OUTPUT_TEXT = "FI_HIDE_OUTPUT_TEXT";
/** Hides embedding vectors */
export declare const FI_HIDE_EMBEDDING_VECTORS = "FI_HIDE_EMBEDDING_VECTORS";
/** Limits characters of a base64 encoding of an image */
export declare const FI_BASE64_IMAGE_MAX_LENGTH = "FI_BASE64_IMAGE_MAX_LENGTH";
export declare const DEFAULT_HIDE_INPUTS = false;
export declare const DEFAULT_HIDE_OUTPUTS = false;
export declare const DEFAULT_HIDE_INPUT_MESSAGES = false;
export declare const DEFAULT_HIDE_OUTPUT_MESSAGES = false;
export declare const DEFAULT_HIDE_INPUT_IMAGES = false;
export declare const DEFAULT_HIDE_INPUT_TEXT = false;
export declare const DEFAULT_HIDE_OUTPUT_TEXT = false;
export declare const DEFAULT_HIDE_EMBEDDING_VECTORS = false;
export declare const DEFAULT_BASE64_IMAGE_MAX_LENGTH = 32000;
/** When a value is hidden, it will be replaced by this redacted value */
export declare const REDACTED_VALUE = "__REDACTED__";
/**
 * The default, environment, and type information for each value on the TraceConfig
 * Used to generate a full TraceConfig object with the correct types and default values
 */
export declare const traceConfigMetadata: Readonly<Record<TraceConfigKey, TraceConfigFlag>>;
export declare const DefaultTraceConfig: TraceConfig;
