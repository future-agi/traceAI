import { AttributeValue } from "@opentelemetry/api";
import { FISpan } from "./FISpan";

/**
 * Tracing configuration options that can be set to hide or redact sensitive information from traces
 */
export type TraceConfigOptions = {
  hideInputs?: boolean;
  hideOutputs?: boolean;
  hideInputMessages?: boolean;
  hideOutputMessages?: boolean;
  hideInputImages?: boolean;
  hideInputText?: boolean;
  hideOutputText?: boolean;
  hideEmbeddingVectors?: boolean;
  base64ImageMaxLength?: number;
  piiRedaction?: boolean;
  /**
   * Hides error details on spans: the ERROR status keeps no description, exception
   * events keep only `exception.type` (and `exception.escaped`), and
   * `exception.message`, `exception.stacktrace` and `error.message` attributes are dropped.
   * `error.type` is always kept.
   *
   * Tri-state. When left unset (and `FI_HIDE_ERROR_DETAILS` is not set), it turns on
   * whenever any of `hideInputs`, `hideOutputs`, `hideInputMessages`,
   * `hideOutputMessages`, `hideInputText` or `hideOutputText` is on, because error text
   * can quote the hidden content. An explicit `true` / `false` (option or env) wins.
   */
  hideErrorDetails?: boolean;
};

/**
 * A full tracing configuration object that includes all possible options with their final values
 */
export type TraceConfig = Readonly<Required<TraceConfigOptions>>;

export type TraceConfigKey = keyof TraceConfig;

type BooleanTraceConfigFlag = {
  /**
   * The default value for the flag
   */
  default: boolean;
  /**
   * The environment variable key to use to override the default value
   */
  envKey: string;
  /**
   *  The type of the flag
   */
  type: "boolean";
};

type NumericTraceConfigFlag = {
  /**
   * The default value for the flag
   */
  default: number;
  /**
   * The environment variable key to use to override the default value
   */
  envKey: string;
  /**
   * The type of the flag
   */
  type: "number";
};

type OptionalBooleanTraceConfigFlag = {
  /**
   * No static default: when neither the option nor the environment variable is set,
   * the value is derived from the rest of the resolved config
   */
  default: undefined;
  /**
   * The environment variable key to use to override the derived value
   */
  envKey: string;
  /**
   * The type of the flag
   */
  type: "optionalBoolean";
};

/**
 * The default and environment information for a value on the TraceConfig
 * Used to generate a full TraceConfig object
 *
 * @example
 * ```typescript
 * {
 *   hideInputs: {
 *     default: DEFAULT_HIDE_INPUTS, // false
 *     envKey: FI_HIDE_INPUTS, // "FI_HIDE_INPUTS"
 *     type: "boolean",
 *   }
 * }
 * ```
 */
export type TraceConfigFlag =
  | BooleanTraceConfigFlag
  | NumericTraceConfigFlag
  | OptionalBooleanTraceConfigFlag;

/**
 * The arguments for a masking rule, used to determine if a value should be masked (redacted or omitted)
 */
export type MaskingRuleArgs = {
  /**
   * The trace config to use to determine if the value should be masked
   */
  config: TraceConfig;
  /**
   * The key of the attribute to mask
   */
  key: string;
  /**
   * The value of the attribute to mask
   */
  value?: AttributeValue;
};

/**
 * A condition to determine if a value should be masked
 * and an action that masks (removes or redacts) the value if the condition is met
 *   @example
 * ```typescript
 *  const config = {hideInputText: true}
 *  const key = "gen_ai.input.messages"
 *  if (maskInputTextRule.condition({ config, key })) {
 *    return maskInputTextRule.action({ config, key, value })
 *  }
 * ```
 */
export type MaskingRule = {
  /**
   *
   * @param args The {@linkcode MaskingRuleArgs} to determine if the value should be masked
   * @returns true if the value should be masked, false otherwise
   */
  condition: (args: MaskingRuleArgs) => boolean;
  /**
   * An action to be applied if the condition is met
   * @param args The {@linkcode MaskingRuleArgs} to determine how the value should be masked
   * @returns A redacted value or undefined
   */
  action: (args: MaskingRuleArgs) => AttributeValue | undefined;
};

/**
 * A callback that is called when a new active {@link FISpan} is created
 */
export type FIActiveSpanCallback = (span: FISpan) => void;