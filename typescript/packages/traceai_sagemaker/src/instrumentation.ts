import { InstrumentationBase, InstrumentationConfig } from "@opentelemetry/instrumentation";
import { TraceConfigOptions } from "@traceai/fi-core";
import { VERSION } from "./version";

export function isPatched(): boolean {
  return false;
}

/** Placeholder: the implementation lands in the next commit. */
export class SageMakerInstrumentation extends InstrumentationBase {
  public readonly instrumentationName = "@traceai/sagemaker";

  constructor({
    instrumentationConfig,
  }: { instrumentationConfig?: InstrumentationConfig; traceConfig?: TraceConfigOptions } = {}) {
    super("@traceai/sagemaker", VERSION, instrumentationConfig ?? {});
  }

  protected init() {
    return [];
  }

  manuallyInstrument(_module: Record<string, unknown>): void {
    /* not implemented yet */
  }
}
