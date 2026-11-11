import { InstrumentationConfig } from "@opentelemetry/instrumentation";

import type { FrameworkAttachmentOptions } from "@traceai/framework-attachments";

export interface LlamaIndexInstrumentationConfig extends InstrumentationConfig, FrameworkAttachmentOptions {
  /**
   * A custom logger to log any exceptions that happen during span creation.
   */
  exceptionLogger?: (e: Error) => void;
}
