import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { Mutable, ValueOf } from "./typeUtils";

/**
 * A ReadWriteSpan is a mutable {@link ReadableSpan}.
 * Within the OpenTelemetry SpanProcessors, the spans are typed as readonly
 * However, mutation of spans in processors has been added to the OTEL spec and implementation is in progress @see https://github.com/open-telemetry/opentelemetry-specification/pull/4024
 * We use this type to directly mutate the attributes of spans in the FISpanProcessor
 */
export type ReadWriteSpan = Mutable<ReadableSpan>;

export type FISemanticConventionKey = ValueOf<
  typeof SemanticConventions
>;

export type FIIOConventionKey = Extract<
  FISemanticConventionKey,
  | typeof SemanticConventions.OUTPUT_VALUE
  | typeof SemanticConventions.INPUT_VALUE
>;

/**
 * A filter to apply to a {@link ReadableSpan}.
 */
export type SpanFilter = (span: ReadableSpan) => boolean;