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

export class AnthropicInstrumentation extends InstrumentationBase<typeof Anthropic> {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/instrumentation-anthropic", VERSION, instrumentationConfig);
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  protected init(): InstrumentationModuleDefinition<typeof Anthropic> {
    const module = new InstrumentationNodeModuleDefinition<typeof Anthropic>(
      MODULE_NAME,
      [">=0.20.0 <1"], // Looser version range based on recent activity
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  manuallyInstrument(module: typeof Anthropic) {
    diag.debug(`Manually instrumenting ${MODULE_NAME}`);
    this.patch(module);
  }

  private patch(moduleExports: typeof Anthropic, moduleVersion?: string) {
    diag.debug(`Applying patch for ${MODULE_NAME}@${moduleVersion}`);
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
      ((originalCreate: Anthropic.Messages['create']) => {
        return function patchedMessagesCreate(this: Anthropic.Messages, ...args: Parameters<Anthropic.Messages['create']>) {
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

          const originalPromise = context.with(execContext, () => {
            return originalCreate.apply(this, args);
          });

          originalPromise
            .then(async (result: Awaited<ReturnType<Anthropic.Messages['create']>>) => {
              try {
                if (params.stream === true) {
                  const receivedStream = result as Stream<Anthropic.MessageStreamEvent>; 
                  
                  // Tee the stream.
                  const [streamForInstrumentation, _streamForUser] = receivedStream.tee(); 

                  if (!streamForInstrumentation || typeof streamForInstrumentation[Symbol.asyncIterator] !== 'function') {
                    diag.error("Anthropic stream (teed copy) not async iterable for attributes.");
                    span.setStatus({ code: SpanStatusCode.ERROR, message: "Teed stream copy not async iterable." });
                    span.end(); // End span on error
                    return; 
                  }
                  
                  const chunks: MessageStreamEvent[] = [];
                  try {
                    for await (const chunk of streamForInstrumentation) { // Iterate the teed copy for instrumentation
                      chunks.push(chunk); // chunk is already MessageStreamEvent
                    }
                    const { reconstructedMessage, rawOutputChunks } = aggregateAnthropicStreamEvents(chunks);
                    
                    // Attributes for streamed response, to match Python's _MessageResponseExtractor
                    const streamSpanAttributes: Attributes = {};
                    let concatenatedStreamText = "";
                    let toolCallOutputIndex = 0;

                    if (reconstructedMessage.role) { // Should be 'assistant' for output
                        streamSpanAttributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`] = reconstructedMessage.role;
                    }

                    reconstructedMessage.content.forEach(block => {
                        if (block.type === 'text') {
                            concatenatedStreamText += (block as Anthropic.TextBlock).text;
                            // Python's _MessageResponseExtractor -> get_extra_attributes sets MESSAGE_CONTENT per text block encountered in stream
                            // For simplicity here and to align with a single OUTPUT_VALUE, we'll set one combined MESSAGE_CONTENT later.
                        } else if (block.type === 'tool_use') {
                            const toolUseBlock = block as Anthropic.ToolUseBlock;
                            const toolCallPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_TOOL_CALLS}.${toolCallOutputIndex}.`;
                            streamSpanAttributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] = toolUseBlock.name;
                            streamSpanAttributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] = safelyJSONStringify(toolUseBlock.input) ?? "{}";
                            // Python's _stream.py does not seem to add TOOL_CALL_ID for output streams here.
                            toolCallOutputIndex++;
                        }
                    });

                    if (concatenatedStreamText) {
                        // Set MESSAGE_CONTENT based on concatenated text from stream, similar to Python's get_extra_attributes for text blocks
                        streamSpanAttributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`] = concatenatedStreamText;
                        // Mimic Python's _MessageResponseExtractor -> get_attributes() for OUTPUT_VALUE and MIME_TYPE
                        streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = concatenatedStreamText;
                        streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.TEXT;
                    } else if (toolCallOutputIndex > 0) { // If there were tool calls but no text
                        // If only tool_calls, Python's OUTPUT_VALUE defaults to JSON of raw output (reconstructed message)
                        streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = safelyJSONStringify(reconstructedMessage) ?? "";
                        streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.JSON;
                    } else {
                         // Default if no text and no tools (empty message?)
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
                  }
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
                }
              } catch (attributeProcessingError: unknown) {
                diag.error("Error setting span attributes from Anthropic response", attributeProcessingError);
                span.recordException(new Error(`Attribute processing error: ${(attributeProcessingError as Error).message}`));
              }
            })
            .catch((error: Error) => {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
            })
            .finally(() => {
              span.end();
            });

          return originalPromise;
        };
      }) as any
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
    diag.debug(`Removing patch for ${MODULE_NAME}`);
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
