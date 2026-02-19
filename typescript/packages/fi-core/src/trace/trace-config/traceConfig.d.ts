import { TraceConfig, TraceConfigOptions } from "./types";
/**
 * Generates a full trace config object based on passed in options, environment variables, and default values.
 * The order of precedence is: optionValue > envValue > defaultValue
 * @param options - The user provided TraceConfigOptions.
 * @returns A full TraceConfig object with all options set to their final values.
 */
export declare function generateTraceConfig(options?: TraceConfigOptions): TraceConfig;
