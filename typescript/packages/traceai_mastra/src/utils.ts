import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import {
  SemanticConventions,
  ATTR_PROJECT_NAME,
} from "@traceai/fi-semantic-conventions";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

/**
 * Augments a span with OpenInference project resource attribute.
 *
 * This function will add additional attributes to the span, based on the span's resource attributes.
 *
 * @param span - The span to augment.
 */
export const addFIProjectResourceAttributeSpan = (
  span: ReadableSpan,
) => {
  const attributes = span.resource.attributes;
  if (ATTR_SERVICE_NAME in attributes) {
    attributes[ATTR_PROJECT_NAME] = attributes[ATTR_SERVICE_NAME];
  }
};

/**
 * Determines whether a span is an OpenInference span.
 *
 * @param span - The span to check.
 * @returns `true` if the span is an OpenInference span, `false` otherwise.
 */
export const isFISpan = (span: ReadableSpan) => {
  const maybeFISpanKind =
    span.attributes[SemanticConventions.FI_SPAN_KIND];
  return typeof maybeFISpanKind === "string";
};

