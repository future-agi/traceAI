import { diag } from "@opentelemetry/api";
import {
  HIDE_ERROR_DETAILS_IMPLIED_BY,
  traceConfigMetadata,
} from "./constants";
import { TraceConfigKey, TraceConfig, TraceConfigOptions } from "./types";
import { assertUnreachable, withSafety } from "../../utils";

const safelyParseInt = withSafety({ fn: parseInt });

const SWITCH_ON = ["true", "1", "yes", "on"];
const SWITCH_OFF = ["false", "0", "no", "off"];
const warnedSwitches = new Set<string>();

/**
 * Reads a privacy switch from the environment. A switch someone set must never silently do
 * nothing: true, 1, yes and on turn it on, false, 0, no and off turn it off (any case,
 * surrounding spaces ignored), and an empty value counts as unset. Any other value turns it
 * on, with one warning per variable that names the accepted values and never the value.
 * The export boundary reads its own two switches with the same function.
 */
export function parseSwitch(envValue: string, envKey: string): boolean | undefined {
  const normalized = envValue.trim().toLowerCase();
  if (normalized === "") return undefined;
  if (SWITCH_ON.includes(normalized)) return true;
  if (SWITCH_OFF.includes(normalized)) return false;
  if (!warnedSwitches.has(envKey)) {
    warnedSwitches.add(envKey);
    diag.warn(
      `${envKey} is not one of true, 1, yes, on, false, 0, no or off; treating it as on, so the content it controls is hidden.`,
    );
  }
  return true;
}

type TraceConfigOptionMetadata = (typeof traceConfigMetadata)[TraceConfigKey];

/**
 * Parses an option based on its type
 * The order of precedence is: optionValue > envValue > defaultValue
 * @param key - The key of the option.
 * @param optionMetadata - The {@link TraceConfigOptionMetadata} for the option which includes its type, default value, and environment variable key.
 *
 * Returns undefined only for an "optionalBoolean" flag that is set neither by option nor by env.
 */
function parseOption({
  optionValue,
  optionMetadata,
}: {
  optionValue?: number | boolean;
  optionMetadata: TraceConfigOptionMetadata;
}): number | boolean | undefined {
  if (optionValue !== undefined) {
    if (optionMetadata.type === "number") {
      if (typeof optionValue === "number" && Number.isSafeInteger(optionValue) && optionValue >= 0) {
        return optionValue;
      }
      // The numeric option is the image cap. Invalid input never restores a
      // positive default or environment cap, and caller data never enters logs.
      diag.warn("Invalid base64 image cap; using 0.");
      return 0;
    }
    return optionValue;
  }
  const envValue = process.env[optionMetadata.envKey];
  if (envValue !== undefined) {
    switch (optionMetadata.type) {
      case "number": {
        const maybeEnvNumber = safelyParseInt(envValue);
        if (/^\s*\d+\s*$/.test(envValue) && maybeEnvNumber != null && Number.isSafeInteger(maybeEnvNumber) && maybeEnvNumber >= 0) {
          return maybeEnvNumber;
        }
        // Keep the existing complete-integer grammar, including valid "0";
        // never truncate a fractional/partial value or throw for invalid text.
        diag.warn("Invalid base64 image cap; using 0.");
        return 0;
      }
      case "boolean":
        return parseSwitch(envValue, optionMetadata.envKey) ?? optionMetadata.default;
      case "optionalBoolean":
        return parseSwitch(envValue, optionMetadata.envKey) ?? optionMetadata.default;
      default:
        assertUnreachable(optionMetadata);
    }
  }

  return optionMetadata.default;
}

/**
 * Generates a full trace config object based on passed in options, environment variables, and default values.
 * The order of precedence is: optionValue > envValue > defaultValue
 *
 * `hideErrorDetails` has no static default: when neither the option nor `FI_HIDE_ERROR_DETAILS` is set,
 * it is on whenever any of the content-hiding flags in {@link HIDE_ERROR_DETAILS_IMPLIED_BY} is on.
 *
 * Environment variables are read even when no options are passed.
 * @param options - The user provided TraceConfigOptions.
 * @returns A full TraceConfig object with all options set to their final values.
 */
export function generateTraceConfig(options?: TraceConfigOptions): TraceConfig {
  const resolvedOptions: TraceConfigOptions = options ?? {};
  const config = Object.entries(traceConfigMetadata).reduce(
    (config, [key, optionMetadata]) => {
      const TraceConfigKey = key as TraceConfigKey;
      return {
        ...config,
        [TraceConfigKey]: parseOption({
          optionValue: resolvedOptions[TraceConfigKey],
          optionMetadata,
        }),
      };
    },
    {} as Record<TraceConfigKey, number | boolean | undefined>,
  );
  if (config.hideErrorDetails === undefined) {
    config.hideErrorDetails = HIDE_ERROR_DETAILS_IMPLIED_BY.some(
      (flag) => config[flag] === true,
    );
  }
  return config as TraceConfig;
}
