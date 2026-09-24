import type * as llamaindex from "llamaindex";

import {
  Tracer,
  Span,
  Context,
  SpanStatusCode,
  trace,
  context,
  DiagLogger,
  Attributes,
} from "@opentelemetry/api";
import { safeExecuteInTheMiddle } from "@opentelemetry/instrumentation";

import { SemanticConventions, FISpanKind } from "@traceai/fi-semantic-conventions";
import { safelyJSONStringify } from "@traceai/fi-core";

import { LlamaIndexInstrumentationConfig } from "./types";
import {
  shouldSendPrompts,
  llmGeneratorWrapper,
  generatorWrapper,
  StreamedChatOutput,
} from "./utils";

type LLM = llamaindex.LLM;

type AsyncResponseType = AsyncIterable<llamaindex.ChatResponseChunk>;

// eslint-disable-next-line
export type Method = Function;
export type MethodWrapper = (original: Method) => Method;

interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
}

interface SerializedToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

interface SerializedMessage {
  role: string;
  content?: string;
  tool_calls?: SerializedToolCall[];
}

function getTextContent(content: llamaindex.MessageContent): string | undefined {
  if (typeof content === "string") {
    return content;
  }
  const first = Array.isArray(content) ? content[0] : undefined;
  return first?.type === "text"
    ? (first as llamaindex.MessageContentTextDetail).text
    : undefined;
}

function getToolCalls(options: object | undefined): SerializedToolCall[] | undefined {
  const toolCalls = (options as Partial<llamaindex.ToolCallOptions> | undefined)?.toolCall;
  if (!Array.isArray(toolCalls) || toolCalls.length === 0) {
    return undefined;
  }
  return toolCalls.map(({ id, name, input }) => ({
    id,
    type: "function",
    function: {
      name,
      arguments: typeof input === "string" ? input : JSON.stringify(input),
    },
  }));
}

function serializeMessage(message: llamaindex.ChatMessage): SerializedMessage {
  const serialized: SerializedMessage = {
    role: message.role,
    content: getTextContent(message.content),
  };
  const toolCalls = getToolCalls(message.options);
  if (toolCalls) {
    serialized.tool_calls = toolCalls;
  }
  return serialized;
}

function getOutputAttributes(
  message: llamaindex.ChatMessage,
  raw: object | null,
): Attributes {
  const usage = (raw as { usage?: TokenUsage | null } | null)?.usage;
  return {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]:
      safelyJSONStringify([serializeMessage(message)]) ?? "[]",
    [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]:
      usage?.prompt_tokens ?? usage?.input_tokens,
    [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]:
      usage?.completion_tokens ?? usage?.output_tokens,
    [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: usage?.total_tokens,
  };
}

function endSpanWithError(span: Span, error: Error) {
  span.recordException(error);
  span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
  span.end();
}

function handleResponse(
    result: llamaindex.ChatResponse,
    span: Span,
    metadata: llamaindex.LLMMetadata,
    config: LlamaIndexInstrumentationConfig,
    diag: DiagLogger,
  ): llamaindex.ChatResponse {
    span.setAttribute(SemanticConventions.LLM_MODEL_NAME, metadata.model);

    if (!shouldSendPrompts()) {
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    }

    try {
      if (result.message) {
        span.setAttributes(getOutputAttributes(result.message, result.raw));
        span.setStatus({ code: SpanStatusCode.OK });
      }
    } catch (e) {
      diag.warn(e as any);
      config.exceptionLogger?.(e as Error);
    }

    span.end();

    return result;
  }

function handleStreamingResponse<T extends AsyncResponseType>(
    result: T,
    span: Span,
    execContext: Context,
    metadata: llamaindex.LLMMetadata,
    config: LlamaIndexInstrumentationConfig,
  ): T {
    span.setAttribute(SemanticConventions.LLM_MODEL_NAME, metadata.model);
    if (!shouldSendPrompts()) {
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    }

    return llmGeneratorWrapper(
      result,
      execContext,
      ({ message, raw }: StreamedChatOutput) => {
        span.setAttributes(getOutputAttributes(message, raw));
        span.setStatus({ code: SpanStatusCode.OK });
        span.end();
      },
      (error) => endSpanWithError(span, error),
    ) as unknown as T;
  }

export function chatWrapper({ className }: { className: string },
    config: LlamaIndexInstrumentationConfig,
    diag: DiagLogger,
    tracer: () => Tracer,
): MethodWrapper {
    return (original: Method) => {
      return function method(this: LLM, ...args: Parameters<LLM["chat"]>) {
        const params = args[0] as
          | llamaindex.LLMChatParamsStreaming
          | llamaindex.LLMChatParamsNonStreaming
          | undefined;
        const messages = params?.messages;
        const streaming = params?.stream;

        const span = tracer()
          .startSpan(`llamaindex.${className}.chat`);

        span.setAttribute(SemanticConventions.FI_SPAN_KIND, FISpanKind.LLM);

        try {
          span.setAttribute(SemanticConventions.LLM_PROVIDER, className);

          span.setAttribute(
            SemanticConventions.LLM_MODEL_NAME,
            this.metadata.model,
          );
          if (shouldSendPrompts() && messages) {
            span.setAttribute(
              SemanticConventions.LLM_INPUT_MESSAGES,
              safelyJSONStringify(messages.map(serializeMessage)) ?? "[]",
            );
          }
        } catch (e) {
          diag.warn(e as any);
          config.exceptionLogger?.(e as Error);
        }

        const execContext = trace.setSpan(context.active(), span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(execContext, () => {
              return original.apply(this, args);
            });
          },
          // eslint-disable-next-line @typescript-eslint/no-empty-function
          () => {},
        );
        const wrappedPromise = execPromise
          .then((result: any) => {
            return new Promise((resolve) => {
              if (streaming) {
                result = handleStreamingResponse(
                  result,
                  span,
                  execContext,
                  this.metadata,
                  config
                );
              } else {
                result = handleResponse(result, span, this.metadata, config, diag);
              }
              resolve(result);
            });
          })
          .catch((error: Error) => {
            return new Promise((_, reject) => {
              endSpanWithError(span, error);
              reject(error);
            });
          });
        return context.bind(execContext, wrappedPromise as any);
      };
    };
  }

export function genericWrapper(
    className: string,
    methodName: string,
    kind: FISpanKind,
    tracer: () => Tracer,
  ): MethodWrapper {
    return (original: Method) => {
      return function method(this: any, ...args: unknown[]) {
        const params = args[0];
        const streaming = params && (params as any).stream;
  
        const name = `${this?.constructor?.name || className}.${methodName}`;
        const span = tracer().startSpan(`${name}`, {}, context.active());
        span.setAttribute(SemanticConventions.FI_SPAN_KIND, kind);
  

        if (shouldSendPrompts()) {
          try {
            if (
              args.length === 1 &&
              typeof args[0] === "object" &&
              !(args[0] instanceof Map)
            ) {
              span.setAttribute(
                SemanticConventions.INPUT_VALUE,
                JSON.stringify({ args: [], kwargs: args[0] }),
              );
            } else {
              span.setAttribute(
                SemanticConventions.INPUT_VALUE,
                JSON.stringify({
                  args: args.map((arg) =>
                    arg instanceof Map ? Array.from(arg.entries()) : arg,
                  ),
                  kwargs: {},
                }),
              );
            }
          } catch {
            /* empty */
          }
        }
  
        const execContext = trace.setSpan(context.active(), span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(execContext, () => {
              return original.apply(this, args);
            });
          },
          // eslint-disable-next-line @typescript-eslint/no-empty-function
          () => {},
        );
        const wrappedPromise = execPromise
          .then((result: any) => {
            return new Promise((resolve) => {
              if (streaming) {
                result = generatorWrapper(result, execContext, () => {
                  span.setStatus({ code: SpanStatusCode.OK });
                  span.end();
                });
                resolve(result);
              } else {
                span.setStatus({ code: SpanStatusCode.OK });
  
                try {
                  if (shouldSendPrompts()) {
                    if (result instanceof Map) {
                      span.setAttribute(
                        SemanticConventions.OUTPUT_VALUE,
                        JSON.stringify(Array.from(result.entries())),
                      );
                    } else {
                      span.setAttribute(
                        SemanticConventions.OUTPUT_VALUE,
                        JSON.stringify(result),
                      );
                    }
                  }
                } finally {
                  span.end();
                  resolve(result);
                }
              }
            });
          })
          .catch((error: Error) => {
            return new Promise((_, reject) => {
              endSpanWithError(span, error);
              reject(error);
            });
          });
        return context.bind(execContext, wrappedPromise as any);
      };
    };
  }
