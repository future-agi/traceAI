import {
  context,
  createContextKey,
  diag,
  Span,
  SpanKind,
  SpanStatusCode,
  trace,
} from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  InstrumentationNodeModuleFile,
  isWrapped,
} from "@opentelemetry/instrumentation";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import {
  FISpanKind,
  SemanticConventions,
} from "@traceai/fi-semantic-conventions";
import type { WorkflowExecute } from "n8n-core";
import type { IRun, Workflow } from "n8n-workflow";

const VERSION = "0.1.0";
const SUPPORTED_VERSIONS = [">=2.16.1 <3"];
const EXECUTION = createContextKey("traceai.n8n.execution");

type CoreModule = { WorkflowExecute: typeof WorkflowExecute };
type Methods = Pick<WorkflowExecute, "processRunExecutionData" | "runNode">;
type FinishNode = (status: string, error?: unknown) => void;
type Execution = { span: Span; nodes: Set<FinishNode>; closed: boolean };
type Patch = {
  workflow: Methods["processRunExecutionData"];
  node: Methods["runNode"];
};

export interface N8nInstrumentationConfig {
  instrumentationConfig?: InstrumentationConfig;
  traceConfig?: TraceConfigOptions;
}

/** Workflow and node-attempt spans for the self-hosted n8n execution engine. */
export class N8nInstrumentation extends InstrumentationBase {
  // Module hooks can run during the base constructor. Lazy state must survive it.
  declare private patches: Map<Methods, Patch> | undefined;
  private readonly traceConfig?: TraceConfigOptions;

  constructor(config: N8nInstrumentationConfig = {}) {
    super("@traceai/n8n", VERSION, config.instrumentationConfig ?? {});
    this.traceConfig = config.traceConfig;
  }

  protected init(): InstrumentationNodeModuleDefinition {
    return new InstrumentationNodeModuleDefinition(
      "n8n-core",
      SUPPORTED_VERSIONS,
      (module: CoreModule) => this.patch(module),
      (module: CoreModule) => this.unpatch(module),
      [
        new InstrumentationNodeModuleFile(
          "n8n-core/dist/execution-engine/workflow-execute.js",
          SUPPORTED_VERSIONS,
          (module: CoreModule) => this.patch(module),
          (module: CoreModule) => this.unpatch(module),
        ),
      ],
    );
  }

  /** Instrument an already-loaded n8n-core module. */
  manuallyInstrument(module: CoreModule): void {
    if (this.isEnabled()) this.patch(module);
  }

  override disable(): void {
    super.disable();
    // Also restore modules supplied through manuallyInstrument().
    for (const prototype of this.patches?.keys() ?? []) {
      this.restore(prototype);
    }
  }

  private patch(module: CoreModule): CoreModule {
    const prototype = module?.WorkflowExecute?.prototype;
    if (
      !prototype ||
      this.patches?.has(prototype) ||
      typeof prototype.processRunExecutionData !== "function" ||
      typeof prototype.runNode !== "function"
    ) {
      return module;
    }
    if (
      isWrapped(prototype.processRunExecutionData) ||
      isWrapped(prototype.runNode)
    ) {
      diag.warn(
        "n8n execution methods are already instrumented; leaving them unchanged",
      );
      return module;
    }

    const instrumentation = this;
    this._wrap(prototype, "processRunExecutionData", (original) => {
      return function (this: WorkflowExecute, workflow: Workflow) {
        return instrumentation.traceWorkflow(original, this, workflow);
      };
    });
    this._wrap(prototype, "runNode", (original) => {
      return function (
        this: WorkflowExecute,
        ...args: Parameters<Methods["runNode"]>
      ) {
        return instrumentation.traceNode(original, this, args);
      };
    });
    (this.patches ??= new Map()).set(prototype, {
      workflow: prototype.processRunExecutionData,
      node: prototype.runNode,
    });
    return module;
  }

  private unpatch(module: CoreModule): void {
    if (module?.WorkflowExecute) this.restore(module.WorkflowExecute.prototype);
  }

  private restore(prototype: Methods): void {
    const patch = this.patches?.get(prototype);
    if (!patch) return;
    // Do not remove a wrapper subsequently installed by another instrumentor.
    if (prototype.processRunExecutionData === patch.workflow) {
      this._unwrap(prototype, "processRunExecutionData");
    }
    if (prototype.runNode === patch.node) this._unwrap(prototype, "runNode");
    this.patches?.delete(prototype);
  }

  private getFITracer(): FITracer {
    // setTracerProvider() may replace this.tracer after construction.
    return new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig });
  }

  private traceWorkflow(
    original: Methods["processRunExecutionData"],
    instance: WorkflowExecute,
    workflow: Workflow,
  ): ReturnType<Methods["processRunExecutionData"]> {
    const parent = context.active();
    if (!this.isEnabled() || isTracingSuppressed(parent)) {
      return original.call(instance, workflow);
    }
    const span = this.getFITracer().startSpan(
      `n8n workflow: ${workflow.name ?? workflow.id ?? "workflow"}`,
      {
        kind: SpanKind.INTERNAL,
        attributes: {
          [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
          "n8n.workflow.id": workflow.id,
          "n8n.workflow.name": workflow.name,
        },
      },
      parent,
    );
    const execution: Execution = { span, nodes: new Set(), closed: false };
    const active = trace.setSpan(parent, span).setValue(EXECUTION, execution);
    let result: ReturnType<Methods["processRunExecutionData"]>;
    try {
      result = context.with(active, () => original.call(instance, workflow));
    } catch (error) {
      this.finishWorkflow(execution, "error", error);
      throw error;
    }

    const restoreCancel = this.observeCancellation(result, execution);
    // Observe settlement without replacing n8n's PCancelable object.
    void result.then(
      (run: IRun) => {
        restoreCancel();
        if (!execution.closed)
          span.setAttribute("n8n.execution.mode", run.mode);
        this.finishWorkflow(execution, run.status, run.data.resultData.error);
      },
      (error: unknown) => {
        restoreCancel();
        this.finishWorkflow(execution, "error", error);
      },
    );
    return result;
  }

  private observeCancellation(
    result: ReturnType<Methods["processRunExecutionData"]>,
    execution: Execution,
  ): () => void {
    const ownDescriptor = Object.getOwnPropertyDescriptor(result, "cancel");
    const original = result.cancel;
    const instrumentation = this;
    const cancel = function (
      this: typeof result,
      ...args: Parameters<typeof original>
    ) {
      try {
        return original.apply(this, args);
      } finally {
        if (this === result && result.isCanceled) {
          instrumentation.finishWorkflow(execution, "canceled");
          restore();
        }
      }
    };
    try {
      Object.defineProperty(result, "cancel", {
        configurable: true,
        writable: true,
        value: cancel,
      });
    } catch (error) {
      diag.debug("Unable to observe n8n cancellation", error);
    }
    const restore = () => {
      if (result.cancel !== cancel) return;
      try {
        if (ownDescriptor)
          Object.defineProperty(result, "cancel", ownDescriptor);
        else delete (result as { cancel?: typeof cancel }).cancel;
      } catch (error) {
        diag.debug("Unable to restore n8n cancellation observer", error);
      }
    };
    return restore;
  }

  private finishWorkflow(
    execution: Execution,
    status: string,
    error?: unknown,
  ): void {
    if (execution.closed) return;
    execution.closed = true;
    for (const finish of execution.nodes) finish(status, error);
    execution.nodes.clear();
    execution.span.setAttribute("n8n.execution.status", status);
    if (
      error ||
      status === "error" ||
      status === "crashed" ||
      status === "canceled"
    ) {
      this.markError(execution.span, error);
    } else if (status === "success") {
      execution.span.setStatus({ code: SpanStatusCode.OK });
    }
    execution.span.end();
  }

  private traceNode(
    original: Methods["runNode"],
    instance: WorkflowExecute,
    args: Parameters<Methods["runNode"]>,
  ): ReturnType<Methods["runNode"]> {
    const parent = context.active();
    const execution = parent.getValue(EXECUTION) as Execution | undefined;
    if (!this.isEnabled() || isTracingSuppressed(parent) || execution?.closed) {
      return original.apply(instance, args);
    }
    const [, data, , runIndex, additionalData, mode] = args;
    const node = data.node;
    execution?.span.setAttribute("n8n.execution.mode", mode);
    if (additionalData.executionId) {
      execution?.span.setAttribute(
        "n8n.execution.id",
        additionalData.executionId,
      );
    }
    const span = this.getFITracer().startSpan(
      `n8n node: ${node.name}`,
      {
        kind: SpanKind.INTERNAL,
        attributes: {
          [SemanticConventions.FI_SPAN_KIND]: FISpanKind.TOOL,
          "n8n.node.id": node.id,
          "n8n.node.name": node.name,
          "n8n.node.type": node.type,
          "n8n.node.type_version": node.typeVersion,
          "n8n.node.run_index": runIndex,
          "n8n.execution.mode": mode,
          "n8n.execution.id": additionalData.executionId,
        },
      },
      parent,
    );
    let ended = false;
    const finish: FinishNode = (status, error) => {
      if (ended) return;
      ended = true;
      execution?.nodes.delete(finish);
      span.setAttribute("n8n.node.status", status);
      if (
        error ||
        status === "canceled" ||
        status === "error" ||
        status === "crashed"
      )
        this.markError(span, error);
      else span.setStatus({ code: SpanStatusCode.OK });
      span.end();
    };
    execution?.nodes.add(finish);
    let result: ReturnType<Methods["runNode"]>;
    try {
      result = context.with(trace.setSpan(parent, span), () =>
        original.apply(instance, args),
      );
    } catch (error) {
      finish("error", error);
      throw error;
    }
    void result.then(
      () => finish("success"),
      (error: unknown) => finish("error", error),
    );
    return result;
  }

  private markError(span: Span, error: unknown): void {
    span.setStatus({ code: SpanStatusCode.ERROR });
    // Exception messages can include item data or credentials. Keep metadata only.
    if (error)
      span.recordException({
        name: error instanceof Error ? error.name : "Error",
      });
  }
}
