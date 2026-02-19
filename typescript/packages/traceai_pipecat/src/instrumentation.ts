/**
 * OpenTelemetry instrumentation for Pipecat.
 *
 * Pipecat is a voice AI pipeline framework for building conversational agents.
 * This instrumentation captures pipeline execution, frame processing,
 * and transport events.
 */

import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  Span,
  SpanKind,
  SpanStatusCode,
  context,
  trace,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface PipecatInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * Pipecat Instrumentation class.
 *
 * Provides automatic instrumentation for the Pipecat voice AI SDK.
 */
export class PipecatInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private pipelineSpans: Map<string, Span> = new Map();

  constructor(config: PipecatInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-pipecat",
      VERSION,
      config.instrumentationConfig || {}
    );
    this._traceConfig = config.traceConfig;
  }

  override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@pipecat-ai/client-js",
      ["^0.2.0", "^0.3.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
    return module;
  }

  /**
   * Manually instrument the @pipecat-ai/client-js module.
   */
  manuallyInstrument(pipecatModule: any): void {
    this.patch(pipecatModule);
  }

  private patch(pipecatModule: any & { _fiPatched?: boolean }): any {
    if (pipecatModule?._fiPatched || _isFIPatched) {
      return pipecatModule;
    }

    const instrumentation = this;

    // Wrap RTVIClient.connect
    if (pipecatModule.RTVIClient?.prototype?.connect) {
      this._wrap(
        pipecatModule.RTVIClient.prototype,
        "connect",
        (original: Function) => {
          return function patchedConnect(this: any, ...args: any[]) {
            return instrumentation.traceClientConnect(original, this, args);
          };
        }
      );
    }

    // Wrap RTVIClient.disconnect
    if (pipecatModule.RTVIClient?.prototype?.disconnect) {
      this._wrap(
        pipecatModule.RTVIClient.prototype,
        "disconnect",
        (original: Function) => {
          return function patchedDisconnect(this: any, ...args: any[]) {
            return instrumentation.traceClientDisconnect(original, this, args);
          };
        }
      );
    }

    // Wrap RTVIClient.action
    if (pipecatModule.RTVIClient?.prototype?.action) {
      this._wrap(
        pipecatModule.RTVIClient.prototype,
        "action",
        (original: Function) => {
          return function patchedAction(this: any, ...args: any[]) {
            return instrumentation.traceAction(original, this, args);
          };
        }
      );
    }

    // Wrap RTVIClient.sendMessage (for chat/text interactions)
    if (pipecatModule.RTVIClient?.prototype?.sendMessage) {
      this._wrap(
        pipecatModule.RTVIClient.prototype,
        "sendMessage",
        (original: Function) => {
          return function patchedSendMessage(this: any, ...args: any[]) {
            return instrumentation.traceSendMessage(original, this, args);
          };
        }
      );
    }

    // Wrap Pipeline class if available
    if (pipecatModule.Pipeline?.prototype?.run) {
      this._wrap(
        pipecatModule.Pipeline.prototype,
        "run",
        (original: Function) => {
          return function patchedRun(this: any, ...args: any[]) {
            return instrumentation.tracePipelineRun(original, this, args);
          };
        }
      );
    }

    // Wrap FrameProcessor if available
    if (pipecatModule.FrameProcessor?.prototype?.processFrame) {
      this._wrap(
        pipecatModule.FrameProcessor.prototype,
        "processFrame",
        (original: Function) => {
          return function patchedProcessFrame(this: any, ...args: any[]) {
            return instrumentation.traceFrameProcess(original, this, args);
          };
        }
      );
    }

    pipecatModule._fiPatched = true;
    _isFIPatched = true;
    return pipecatModule;
  }

  private unpatch(pipecatModule: any & { _fiPatched?: boolean }): void {
    if (pipecatModule?.RTVIClient?.prototype?.connect) {
      this._unwrap(pipecatModule.RTVIClient.prototype, "connect");
    }
    if (pipecatModule?.RTVIClient?.prototype?.disconnect) {
      this._unwrap(pipecatModule.RTVIClient.prototype, "disconnect");
    }
    if (pipecatModule?.RTVIClient?.prototype?.action) {
      this._unwrap(pipecatModule.RTVIClient.prototype, "action");
    }
    if (pipecatModule?.RTVIClient?.prototype?.sendMessage) {
      this._unwrap(pipecatModule.RTVIClient.prototype, "sendMessage");
    }
    if (pipecatModule?.Pipeline?.prototype?.run) {
      this._unwrap(pipecatModule.Pipeline.prototype, "run");
    }
    if (pipecatModule?.FrameProcessor?.prototype?.processFrame) {
      this._unwrap(pipecatModule.FrameProcessor.prototype, "processFrame");
    }
    if (pipecatModule) {
      pipecatModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private async traceClientConnect(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const config = instance._config || instance.config || {};
    const baseUrl = config.baseUrl || config.url || "unknown";

    const span = this.fiTracer.startSpan("Pipecat Client Connect", {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "pipecat.operation": "client.connect",
        "pipecat.base_url": this.sanitizeUrl(baseUrl),
        "pipecat.transport": config.transport || "unknown",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify({
          baseUrl: this.sanitizeUrl(baseUrl),
          transport: config.transport,
        }),
      },
    });

    // Store for lifecycle tracking
    const clientId = instance._id || generateId();
    this.pipelineSpans.set(`client:${clientId}`, span);
    instance._fiClientId = clientId;

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        "pipecat.connected": true,
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ connected: true }),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      // Don't end - will be ended on disconnect

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      this.pipelineSpans.delete(`client:${clientId}`);
      throw error;
    }
  }

  private async traceClientDisconnect(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const clientId = instance._fiClientId || "unknown";

    const span = this.fiTracer.startSpan("Pipecat Client Disconnect", {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "pipecat.operation": "client.disconnect",
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        "pipecat.disconnected": true,
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ disconnected: true }),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      // End the connection span
      const connectionSpan = this.pipelineSpans.get(`client:${clientId}`);
      if (connectionSpan) {
        connectionSpan.setStatus({ code: SpanStatusCode.OK });
        connectionSpan.end();
        this.pipelineSpans.delete(`client:${clientId}`);
      }

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceAction(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const action = args[0] || {};
    const actionName = action.action || action.name || "unknown";
    const service = action.service || "unknown";

    const span = this.fiTracer.startSpan(`Pipecat Action: ${actionName}`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.TOOL,
        "pipecat.operation": "action",
        "pipecat.action_name": actionName,
        "pipecat.service": service,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(action),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceSendMessage(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const message = args[0];
    const messageType = typeof message === "object" ? message.type || "message" : "text";

    const span = this.fiTracer.startSpan(`Pipecat Send Message: ${messageType}`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        "pipecat.operation": "send_message",
        "pipecat.message_type": messageType,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify(message),
        [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
      },
    });

    // Add LLM input message attributes
    if (typeof message === "string") {
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.role`,
        "user"
      );
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.content`,
        message
      );
    } else if (message?.content) {
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.role`,
        message.role || "user"
      );
      span.setAttribute(
        `${SemanticConventions.LLM_INPUT_MESSAGES}.0.message.content`,
        message.content
      );
    }

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
        [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async tracePipelineRun(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const pipelineName = instance.name || instance.constructor?.name || "Pipeline";

    const span = this.fiTracer.startSpan(`Pipecat Pipeline: ${pipelineName}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "pipecat.operation": "pipeline.run",
        "pipecat.pipeline_name": pipelineName,
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify({
          pipeline: pipelineName,
          args: args.length > 0 ? args[0] : null,
        }),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify(result),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceFrameProcess(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const frame = args[0];
    const frameType = frame?.type || frame?.constructor?.name || "unknown";
    const processorName = instance.name || instance.constructor?.name || "FrameProcessor";

    const span = this.fiTracer.startSpan(`Pipecat Process Frame: ${frameType}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "pipecat.operation": "frame.process",
        "pipecat.frame_type": frameType,
        "pipecat.processor": processorName,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private sanitizeUrl(url: string): string {
    try {
      const parsed = new URL(url);
      return `${parsed.protocol}//${parsed.host}`;
    } catch {
      return url.split("?")[0]; // Remove query params at minimum
    }
  }
}

// Global patched flag
let _isFIPatched = false;

/**
 * Check if the module has been patched.
 */
export function isPatched(): boolean {
  return _isFIPatched;
}

// Helper functions

function safeJsonStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}

function generateId(): string {
  return Math.random().toString(36).substring(2, 15);
}
