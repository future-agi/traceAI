/**
 * Minimal type declarations for @openai/agents.
 * This module is an optional peer dependency and may not be installed.
 */
declare module "@openai/agents" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  export interface TracingProcessor {
    onTraceStart(trace: any): void;
    onTraceEnd(trace: any): void;
    onSpanStart(span: any): void;
    onSpanEnd(span: any): void;
    forceFlush(): void;
    shutdown(): void;
  }

  export function setTraceProcessors(processors: TracingProcessor[]): void;
  export function addTraceProcessor(processor: TracingProcessor): void;
}
