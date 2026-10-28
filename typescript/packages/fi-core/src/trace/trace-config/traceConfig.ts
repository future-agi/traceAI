import { diag } from "@opentelemetry/api";
import {
  HIDE_ERROR_DETAILS_IMPLIED_BY,
  traceConfigMetadata,
} from "./constants";
import { TraceConfigKey, TraceConfig, TraceConfigOptions } from "./types";
import { assertUnreachable, withSafety } from "../../utils";

const safelyParseInt = withSafety({ fn: parseInt });

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
    return optionValue;
  }
  const envValue = process.env[optionMetadata.envKey];
  if (envValue !== undefined) {
    switch (optionMetadata.type) {
      case "number": {
        const maybeEnvNumber = safelyParseInt(envValue);
        return maybeEnvNumber != null && !isNaN(maybeEnvNumber)
          ? maybeEnvNumber
          : optionMetadata.default;
      }
      case "boolean":
        return envValue.toLowerCase() === "true";
      case "optionalBoolean": {
        const normalized = envValue.trim().toLowerCase();
        if (normalized === "true") return true;
        if (normalized === "false") return false;
        if (normalized !== "") {
          diag.warn(
            `Could not parse '${envValue}' as a boolean for the environment variable '${optionMetadata.envKey}'. Deriving the value instead.`,
          );
        }
        return optionMetadata.default;
      }
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
