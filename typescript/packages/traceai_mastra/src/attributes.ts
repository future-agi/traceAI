import {
  FISpanKind,
  SemanticConventions,
} from "@traceai/fi-semantic-conventions";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

const MASTRA_AGENT_SPAN_NAME_PREFIXES = [
  "agent",
  "mastra.getAgent",
  "post /api/agents",
];

/**
 * Add the FI span kind to the given Mastra span.
 *
 * This function will add the FI span kind to the given Mastra span.
 */
const addFISpanKind = (
  span: ReadableSpan,
  kind: FISpanKind,
) => {
  span.attributes[SemanticConventions.FI_SPAN_KIND] = kind;
};

/**
 * Get the FI span kind for the given Mastra span.
 *
 * This function will return the FI span kind for the given Mastra span, if it has already been set.
 */
const getFISpanKind = (span: ReadableSpan) => {
  return span.attributes[SemanticConventions.FI_SPAN_KIND] as
    | FISpanKind
    | undefined;
};

/**
 * Get the closest FI span kind for the given Mastra span.
 *
 * This function will attempt to detect the closest FI span kind for the given Mastra span,
 * based on the span's name and parent span ID.
 */
const getFISpanKindFromMastraSpan = (
  span: ReadableSpan,
): FISpanKind | null => {
  const oiKind = getFISpanKind(span);
  if (oiKind) {
    return oiKind;
  }
  const spanName = span.name.toLowerCase();
  if (
    MASTRA_AGENT_SPAN_NAME_PREFIXES.some((prefix) =>
      spanName.startsWith(prefix),
    )
  ) {
    return FISpanKind.AGENT;
  }
  return null;
};

/**
 * Enrich a Mastra span with FI attributes.
 *
 * This function will add additional attributes to the span, based on the Mastra span's attributes.
 *
 * It will attempt to detect the closest FI span kind for the given Mastra span, and then
 * enrich the span with the appropriate attributes based on the span kind and current attributes.
 *
 * @param span - The Mastra span to enrich.
 */
export const addFIAttributesToMastraSpan = (span: ReadableSpan) => {
  const kind = getFISpanKindFromMastraSpan(span);
  if (kind) {
    addFISpanKind(span, kind);
  }
  // TODO: Further enrich the span with additional attributes based on the span kind
};