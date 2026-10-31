/**
 * Minimal type declarations for @openai/agents.
 * This module is an optional peer dependency and may not be installed.
 */
declare module "@openai/agents" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  export interface TracingProcessor {
    onTraceStart(trace: any): Promise<void>;
    onTraceEnd(trace: any): Promise<void>;
    onSpanStart(span: any): Promise<void>;
    onSpanEnd(span: any): Promise<void>;
    forceFlush(): Promise<void>;
    shutdown(): Promise<void>;
  }

  export function setTraceProcessors(processors: TracingProcessor[]): void;
  export function addTraceProcessor(processor: TracingProcessor): void;
}
