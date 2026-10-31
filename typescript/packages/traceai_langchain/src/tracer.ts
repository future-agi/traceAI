import { BaseTracer, Run } from "@langchain/core/tracers/base";
import {
  SpanKind,
  Span,
  context,
  trace,
  SpanStatusCode,
  Exception,
} from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import {
  safelyFlattenAttributes,
  safelyFormatFunctionCalls,
  safelyFormatIO,
  safelyFormatInputMessages,
  safelyFormatLLMParams,
  safelyFormatMetadata,
  safelyFormatOutputMessages,
  safelyFormatPromptTemplate,
  safelyFormatRetrievalDocuments,
  safelyFormatSessionId,
  safelyFormatTokenCounts,
  safelyFormatToolCalls,
  safelyGetFISpanKindFromRunType,
} from "./utils";
import { FITracer, getErrorMessage } from "@traceai/fi-core";

type RunWithSpan = {
  run: Run;
  span: Span;
};

/**
 * LangChain's BaseTracer stores `run.error` as `message + "\n\n" + stack`
 * (`stringifyError`). Splits that back into message and stack so the stack never
 * ends up in the span status. Only used when the original error object was not seen.
 */
function parseRunError(runError: string): {
  name?: string;
  message: string;
  stack?: string;
} {
  // The message itself can contain blank lines: split where a JS stack ("Name: ...\n    at ...") starts
  let separator = runError.indexOf("\n\n");
  while (separator !== -1) {
    const stack = runError.slice(separator + 2);
    const name = /^([A-Za-z_$][\w$.]*)(?::|\n|$)/.exec(stack)?.[1];
    if (name != null && /\n\s+at\s/.test(stack)) {
      return { name, message: runError.slice(0, separator), stack };
    }
    separator = runError.indexOf("\n\n", separator + 2);
  }
  return { message: runError };
}

/**
 * Records a failed run on its span: the exception event (the stack trace only goes to
 * `exception.stacktrace`) and an ERROR status whose description is the error message only.
 * @param span - the run's span
 * @param error - the error object LangChain passed to `handle*Error`, when it was seen
 * @param runError - `run.error`, used when the error object was not seen
 */
function recordRunError(span: Span, error: unknown, runError: string) {
  if (error != null) {
    span.recordException(
      typeof error === "object" ? (error as Exception) : String(error),
    );
    span.setStatus({
      code: SpanStatusCode.ERROR,
      message:
        getErrorMessage(error) ??
        (typeof error === "object" ? undefined : String(error)),
    });
    return;
  }
  const { name, message, stack } = parseRunError(runError);
  span.recordException({ name, message, stack });
  span.setStatus({ code: SpanStatusCode.ERROR, message });
}

export class LangChainTracer extends BaseTracer {
  private tracer: FITracer;
  private runs: Record<string, RunWithSpan | undefined> = {};
  /** The error objects passed to handle*Error, keyed by run id, until the run's trace ends */
  private runErrors: Record<string, unknown> = {};
  constructor(tracer: FITracer) {
    super();
    this.tracer = tracer;
  }
  name: string = "FILangChainTracer";
  protected persistRun(_run: Run): Promise<void> {
    return Promise.resolve();
  }

  /**
   * Keeps the error object for {@link _endTrace}: `run.error` is the message plus the full stack.
   */
  private async withRunError(
    error: unknown,
    runId: string,
    handle: () => Promise<Run>,
  ): Promise<Run> {
    this.runErrors[runId] = error;
    try {
      return await handle();
    } finally {
      delete this.runErrors[runId];
    }
  }

  async handleLLMError(
    error: unknown,
    runId: string,
    parentRunId?: string,
    tags?: string[],
    extraParams?: Record<string, unknown>,
  ): Promise<Run> {
    return this.withRunError(error, runId, () =>
      super.handleLLMError(error, runId, parentRunId, tags, extraParams),
    );
  }

  async handleChainError(
    ...args: Parameters<BaseTracer["handleChainError"]>
  ): Promise<Run> {
    const [error, runId] = args;
    return this.withRunError(error, runId, () =>
      super.handleChainError(...args),
    );
  }

  async handleToolError(error: unknown, runId: string): Promise<Run> {
    return this.withRunError(error, runId, () =>
      super.handleToolError(error, runId),
    );
  }

  async handleRetrieverError(error: unknown, runId: string): Promise<Run> {
    return this.withRunError(error, runId, () =>
      super.handleRetrieverError(error, runId),
    );
  }

  /**
   * Called when a new run is created on v0.1.0 of langchain see {@link BaseTracer}
   * @param run the langchain {@link Run} object
   *
   * This method is only available on langchain ^0.1.0 BaseTracer and has been replaced in 0.2 by onRunCreate
   * we support both 0.1 and 0.2 so we need to check if the method exists on the super class before calling it
   */
  protected async _startTrace(run: Run) {
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-ignore
    if (typeof super._startTrace === "function") {
      // eslint-disable-next-line @typescript-eslint/ban-ts-comment
      // @ts-ignore
      await super._startTrace(run);
    }
    await this.startTracing(run);
  }

  /**
   * Called when a new run is created on v0.2.0 of langchain see {@link BaseTracer}
   * @param run the langchain {@link Run} object
   *
   * This method is only available on the langchain ^0.2.0 {@link BaseTracer}
   */
  async onRunCreate(run: Run) {
    if (typeof super.onRunCreate === "function") {
      await super.onRunCreate(run);
    }
    await this.startTracing(run);
  }

  async startTracing(run: Run) {
    if (isTracingSuppressed(context.active())) {
      return;
    }

    /**
     * If the parent span context is available, use it as the active context for the new span.
     * This will allow the new span to be a child of the parent span.
     */
    let activeContext = context.active();
    const parentCtx = this.getParentSpanContext(run);
    if (parentCtx != null) {
      activeContext = trace.setSpanContext(context.active(), parentCtx);
    }

    const span = this.tracer.startSpan(
      run.name,
      {
        kind: SpanKind.INTERNAL,
        attributes: {
          [SemanticConventions.FI_SPAN_KIND]:
            safelyGetFISpanKindFromRunType(run.run_type) ??
            undefined,
        },
      },
      activeContext,
    );

    this.runs[run.id] = { run, span };
  }

  protected async _endTrace(run: Run) {
    await super._endTrace(run);
    if (isTracingSuppressed(context.active())) {
      return;
    }
    const runWithSpan = this.runs[run.id];
    if (!runWithSpan) {
      return;
    }
    const { span } = runWithSpan;
    if (run.error != null) {
      recordRunError(span, this.runErrors[run.id], run.error);
    } else {
      span.setStatus({ code: SpanStatusCode.OK });
    }

    const attributes = safelyFlattenAttributes({
      ...safelyFormatIO({ io: run.inputs, ioType: "input" }),
      ...safelyFormatIO({ io: run.outputs, ioType: "output" }),
      ...safelyFormatInputMessages(run.inputs),
      ...safelyFormatOutputMessages(run.outputs),
      ...safelyFormatRetrievalDocuments(run),
      ...safelyFormatLLMParams(run.extra),
      ...safelyFormatPromptTemplate(run),
      ...safelyFormatTokenCounts(run.outputs),
      ...safelyFormatFunctionCalls(run.outputs),
      ...safelyFormatToolCalls(run),
      ...safelyFormatMetadata(run),
      ...safelyFormatSessionId(run),
    });
    if (attributes != null) {
      span.setAttributes(attributes);
    }

    runWithSpan.span.end();
    delete this.runs[run.id];
  }

  private getParentSpanContext(run: Run) {
    if (run.parent_run_id == null) {
      return;
    }
    const maybeParent = this.runs[run.parent_run_id];
    if (maybeParent == null) {
      return;
    }

    return maybeParent.span.spanContext();
  }
}