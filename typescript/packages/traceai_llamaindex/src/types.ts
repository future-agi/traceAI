import { InstrumentationConfig } from "@opentelemetry/instrumentation";

export interface LlamaIndexInstrumentationConfig extends InstrumentationConfig {
  /**
   * A custom logger to log any exceptions that happen during span creation.
   */
  exceptionLogger?: (e: Error) => void;
}
