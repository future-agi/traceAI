import Anthropic from "@anthropic-ai/sdk";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationModuleDefinition,
  InstrumentationNodeModuleDefinition,
  safeExecuteInTheMiddle,
} from "@opentelemetry/instrumentation";
import {
  diag,
  context,
  trace,
  SpanKind,
  Attributes,
  SpanStatusCode,
  Span,
} from "@opentelemetry/api";
import { SemanticConventions, FISpanKind, LLMSystem, LLMProvider, MimeType } from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, safelyJSONStringify, TraceConfigOptions } from "@traceai/fi-core";
import { VERSION } from "./version";
import {
    getAnthropicInputMessagesAttributes,
    getAnthropicOutputMessagesAttributes,
    getAnthropicUsageAttributes,
    getAnthropicToolsAttributes,
    aggregateAnthropicStreamEvents,
} from "./responseAttributes";
import { type MessageStreamEvent } from "@anthropic-ai/sdk/resources/messages";
import { Stream } from "@anthropic-ai/sdk/streaming";

const MODULE_NAME = "@anthropic-ai/sdk";
let _isFIPatched = false;

export function isPatched() {
  return _isFIPatched;
}

function getExecContext(span: Span) {
  const activeContext = context.active();
  const suppressTracing = isTracingSuppressed(activeContext);
  // If tracing is suppressed, don't set the span in context, effectively dropping it for nested operations
  const execContext = suppressTracing ? activeContext : trace.setSpan(context.active(), span);
  // TODO: check if this diag log is needed or should be removed/conditional
  // if (suppressTracing) {
  //   diag.debug("Tracing is suppressed. Span will not be exported or set in context for children.");
  // }
  return execContext;
}

export class AnthropicInstrumentation extends InstrumentationBase{
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/instrumentation-anthropic", VERSION,Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  protected init(){
    const module = new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=0.20.0 <1"], // Looser version range based on recent activity
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  manuallyInstrument(module: typeof Anthropic) {
    this.patch(module);
  }

  private patch(moduleExports: typeof Anthropic, moduleVersion?: string) {
    if ((moduleExports as any).fiPatched || _isFIPatched) {
      return moduleExports;
    }
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const instrumentation: AnthropicInstrumentation = this;

    if (!(instrumentation.fiTracer as any).tracer || (instrumentation.fiTracer as any).tracer !== instrumentation.tracer) {
        instrumentation.fiTracer = new FITracer({ tracer: instrumentation.tracer, traceConfig: instrumentation._traceConfig });
    }

    const messagesPrototype = moduleExports.Messages?.prototype;
    if (!messagesPrototype) {
      diag.error("Anthropic.Messages.prototype not found, cannot patch messages.create.");
      return moduleExports;
    }

    // Patch messages.create
    this._wrap(
      messagesPrototype,
      "create",
      (originalCreate: Anthropic.Messages['create']) => {
        return async function patchedMessagesCreate(this: Anthropic.Messages, ...args: Parameters<Anthropic.Messages['create']>) {
          const params = args[0] as Anthropic.MessageCreateParams;
          const { messages: _messages, system: _system, tools: _tools, ...invocationParameters } = params;

          const span = instrumentation.fiTracer.startSpan("Anthropic Messages Create", {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: params.model,
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.ANTHROPIC,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.ANTHROPIC,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]: safelyJSONStringify(invocationParameters) ?? "",
              ...getAnthropicInputMessagesAttributes(params),
              ...getAnthropicToolsAttributes(params),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(params) ?? "",
            },
          });

          const execContext = getExecContext(span);

          try {
            const result = await context.with(execContext, () => {
              return originalCreate.apply(this, args);
            });

            if (params.stream === true) {
              const receivedStream = result as Stream<Anthropic.MessageStreamEvent>; 
              const [streamForInstrumentation, streamForUser] = receivedStream.tee();

              const processInstrumentationStream = async () => {
                try {
                  const chunks: MessageStreamEvent[] = [];
                  for await (const chunk of streamForInstrumentation) {
                    chunks.push(chunk); 
                  }
                  const { reconstructedMessage } = aggregateAnthropicStreamEvents(chunks);
                  
                  const streamSpanAttributes: Attributes = {};
                  let concatenatedStreamText = "";
                  let toolCallOutputIndex = 0;

                  if (reconstructedMessage.role) {
                      streamSpanAttributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`] = reconstructedMessage.role;
                  }

                  reconstructedMessage.content.forEach(block => {
                      if (block.type === 'text') {
                          concatenatedStreamText += (block as Anthropic.TextBlock).text;
                      } else if (block.type === 'tool_use') {
                          const toolUseBlock = block as Anthropic.ToolUseBlock;
                          const toolCallPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_TOOL_CALLS}.${toolCallOutputIndex}.`;
                          streamSpanAttributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] = toolUseBlock.name;
                          streamSpanAttributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] = safelyJSONStringify(toolUseBlock.input) ?? "{}";
                          toolCallOutputIndex++;
                      }
                  });

                  if (concatenatedStreamText) {
                      streamSpanAttributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`] = concatenatedStreamText;
                      streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = concatenatedStreamText;
                      streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.TEXT;
                  } else if (toolCallOutputIndex > 0) { 
                      streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = safelyJSONStringify(reconstructedMessage) ?? "";
                      streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.JSON;
                  } else {
                      streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = safelyJSONStringify(reconstructedMessage) ?? "";
                      streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.JSON;
                  }

                  if (reconstructedMessage.usage) {
                      streamSpanAttributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = reconstructedMessage.usage.input_tokens;
                      streamSpanAttributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = reconstructedMessage.usage.output_tokens;
                      streamSpanAttributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] = reconstructedMessage.usage.input_tokens + reconstructedMessage.usage.output_tokens;
                  }

                  span.setAttributes({
                      ...streamSpanAttributes,
                      [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(reconstructedMessage) ?? "",
                  });
                  span.setStatus({ code: SpanStatusCode.OK });
                } catch (streamProcessingError: unknown) {
                  diag.error("Error processing Anthropic stream for attributes", streamProcessingError);
                  span.recordException(streamProcessingError as Error);
                  span.setStatus({ code: SpanStatusCode.ERROR, message: (streamProcessingError as Error).message || "Error processing stream" });
                } finally {
                  span.end();
                }
              };
              
              processInstrumentationStream(); 

              return streamForUser; 
            } else {
              const messageResponse = result as Anthropic.Message;
              span.setAttributes({
                ...getAnthropicOutputMessagesAttributes(messageResponse),
                ...getAnthropicUsageAttributes(messageResponse),
                [SemanticConventions.LLM_MODEL_NAME]: messageResponse.model, 
                [SemanticConventions.OUTPUT_VALUE]: safelyJSONStringify(messageResponse) ?? "",
                [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(messageResponse) ?? "",
              });
              span.setStatus({ code: SpanStatusCode.OK });
              span.end();
              return messageResponse;
            }
          } catch (error: any) {
            span.recordException(error);
            span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
            span.end();
            throw error; 
          }
        } as any; // Added 'as any' to reconcile Promise vs APIPromise type mismatch for the wrapper
      }
    );

    _isFIPatched = true;
    try {
      (moduleExports as any).fiPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
    return moduleExports;
  }

  private unpatch(moduleExports: typeof Anthropic & { fiPatched?: boolean }) {
    const messagesPrototype = moduleExports.Messages?.prototype;
    if (messagesPrototype) {
      this._unwrap(messagesPrototype, "create");
    } else {
      diag.warn("Anthropic.Messages.prototype not found during unpatch, could not unwrap messages.create.");
    }
    _isFIPatched = false;
    try {
      (moduleExports as any).fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
  }
}
