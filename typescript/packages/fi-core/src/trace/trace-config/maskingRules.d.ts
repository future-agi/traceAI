import { AttributeValue } from "@opentelemetry/api";
import { MaskingRuleArgs } from "./types";
/**
 * A function that masks (redacts or removes) sensitive information from span attributes based on the trace config.
 * @param config The {@link TraceConfig} to use to determine if the value should be masked
 * @param key The key of the attribute to mask
 * @param value The value of the attribute to mask
 * @returns The redacted value or undefined if the value should be masked, otherwise the original value
 */
export declare function mask({ config, key, value, }: MaskingRuleArgs): AttributeValue | undefined;
