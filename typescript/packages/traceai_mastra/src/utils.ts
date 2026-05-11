import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

// FI ingestion (fi-core) reads these as raw, un-namespaced resource attribute
// keys. They must be present for traces to be bucketed into a project on the
// Future AGI dashboard.
const FI_RESOURCE_PROJECT_NAME = "project_name";
const FI_RESOURCE_PROJECT_TYPE = "project_type";
const FI_PROJECT_TYPE_OBSERVE = "observe";

/**
 * Ensure the resource carries the FI-required project_name + project_type
 * attributes. Without project_type set, the FI dashboard accepts the trace
 * but won't surface it under any project.
 */
export const addFIProjectResourceAttributeSpan = (span: ReadableSpan) => {
  const attributes = span.resource.attributes as Record<string, unknown>;
  if (
    ATTR_SERVICE_NAME in attributes &&
    !(FI_RESOURCE_PROJECT_NAME in attributes)
  ) {
    attributes[FI_RESOURCE_PROJECT_NAME] = attributes[ATTR_SERVICE_NAME];
  }
  if (!(FI_RESOURCE_PROJECT_TYPE in attributes)) {
    attributes[FI_RESOURCE_PROJECT_TYPE] = FI_PROJECT_TYPE_OBSERVE;
  }
};

/**
 * Determines whether a span carries an FI span kind attribute.
 *
 * @param span - The span to check.
 * @returns `true` if the span has `fi.span.kind` set, `false` otherwise.
 */
export const isFISpan = (span: ReadableSpan) => {
  const maybeFISpanKind = span.attributes[SemanticConventions.FI_SPAN_KIND];
  return typeof maybeFISpanKind === "string";
};
