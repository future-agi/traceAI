import type * as llamaindex from "llamaindex";

import {
  Tracer,
  Span,
  Context,
  SpanStatusCode,
  trace,
  context,
  DiagLogger,
} from "@opentelemetry/api";
import { safeExecuteInTheMiddle } from "@opentelemetry/instrumentation";

import { SemanticConventions, FISpanKind } from "@traceai/fi-semantic-conventions";
import { safelyJSONStringify } from "@traceai/fi-core";

import { LlamaIndexInstrumentationConfig } from "./types";
import { shouldSendPrompts, llmGeneratorWrapper, generatorWrapper } from "./utils";

type LLM = llamaindex.LLM;

type ResponseType = llamaindex.ChatResponse | llamaindex.CompletionResponse;
type AsyncResponseType =
  | AsyncIterable<llamaindex.ChatResponseChunk>
  | AsyncIterable<llamaindex.CompletionResponse>;

function handleResponse<T extends ResponseType>(
    result: T,
    span: Span,
    metadata: llamaindex.LLMMetadata,
    config: LlamaIndexInstrumentationConfig,
    diag: DiagLogger,
  ): T {
    span.setAttribute(SemanticConventions.LLM_MODEL_NAME, metadata.model);

    if (!shouldSendPrompts()) {
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    }

    try {
      if ((result as llamaindex.ChatResponse).message) {
        const chatResult = result as llamaindex.ChatResponse;
        const outputContent = typeof chatResult.message.content === "string"
          ? chatResult.message.content
          : chatResult.message.content[0]?.type === "text"
            ? (chatResult.message.content[0] as llamaindex.MessageContentTextDetail).text
            : undefined;
        span.setAttribute(
          SemanticConventions.LLM_OUTPUT_MESSAGES,
          safelyJSONStringify([{ role: chatResult.message.role, content: outputContent }]) ?? "[]",
        );
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

    return llmGeneratorWrapper(result, execContext, (message) => {
      span.setAttribute(
        SemanticConventions.LLM_OUTPUT_MESSAGES,
        safelyJSONStringify([{ role: "assistant", content: message }]) ?? "[]",
      );
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
    }) as any;
  }

export function chatWrapper({ className }: { className: string },
    config: LlamaIndexInstrumentationConfig,
    diag: DiagLogger,
    tracer: () => Tracer,
) {
    return (original: LLM["chat"]) => {
      return function method(this: LLM, ...args: Parameters<LLM["chat"]>) {
        const params = args[0] as any;
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
            const serialized = messages.map((msg: any) => {
              const content = msg.content;
              const textContent = typeof content === "string"
                ? content
                : Array.isArray(content) && content[0]?.type === "text"
                  ? content[0].text
                  : undefined;
              return { role: msg.role, content: textContent };
            });
            span.setAttribute(
              SemanticConventions.LLM_INPUT_MESSAGES,
              safelyJSONStringify(serialized) ?? "[]",
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
              span.setStatus({
                code: SpanStatusCode.ERROR,
                message: error.message,
              });
              span.end();
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
  ) {
    // eslint-disable-next-line
    return (original: Function) => {
      return function method(this: any, ...args: unknown[]) {
        const params = args[0];
        const streaming = params && (params as any).stream;
  
        const name = `${className}.${methodName}`;
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
              span.setStatus({
                code: SpanStatusCode.ERROR,
                message: error.message,
              });
              span.end();
              reject(error);
            });
          });
        return context.bind(execContext, wrappedPromise as any);
      };
    };
  }
