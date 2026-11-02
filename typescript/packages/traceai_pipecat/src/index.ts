/**
 * @traceai/fi-instrumentation-pipecat
 *
 * OpenTelemetry instrumentation for Pipecat JavaScript client telemetry.
 */

export { PipecatInstrumentation, PipecatInstrumentationConfig } from "./instrumentation";
export { isPatched } from "./instrumentation";
export { VERSION } from "./version";
export { observePipecatClient, PipecatEventSource, PipecatClientTracingOptions } from "./client";
