import * as lodash from "lodash";
import type * as llamaindex from "llamaindex";

import {
  Tracer,
  Span,
  Context,
  SpanKind,
  SpanStatusCode,
  trace,
  context,
  DiagLogger,
} from "@opentelemetry/api";
import { safeExecuteInTheMiddle } from "@opentelemetry/instrumentation";

import { SemanticConventions, FISpanKind } from "@traceai/fi-semantic-conventions";

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
        span.setAttribute(
          `${SemanticConventions.LLM_INPUT_MESSAGES}.0.role`,
          (result as llamaindex.ChatResponse).message.role,
        );
        const content = (result as llamaindex.ChatResponse).message.content;
        if (typeof content === "string") {
          span.setAttribute(
            `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.content`,
            content,
          );
        } else if (content[0].type === "text") {
          span.setAttribute(
            `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.content`,
            content[0].text,
          );
        }
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
      span.setAttribute(`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.content`, message);
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
          .startSpan(`llamaindex.${lodash.snakeCase(className)}.chat`, {
            kind: SpanKind.CLIENT,
          });

        try {
          span.setAttribute(SemanticConventions.LLM_SYSTEM, className);
          span.setAttribute(
            SemanticConventions.LLM_MODEL_NAME,
            this.metadata.model,
          );
          if (shouldSendPrompts()) {
            for (const messageIdx in messages) {
              const content = messages[messageIdx].content;
              if (typeof content === "string") {
                span.setAttribute(
                  `${SemanticConventions.LLM_INPUT_MESSAGES}.${messageIdx}.content`,
                  content as string,
                );
              } else if (
                (content as llamaindex.MessageContentDetail[])[0].type ===
                "text"
              ) {
                span.setAttribute(
                  `${SemanticConventions.LLM_INPUT_MESSAGES}.${messageIdx}.content`,
                  (content as llamaindex.MessageContentTextDetail[])[0].text,
                );
              }

              span.setAttribute(
                `${SemanticConventions.LLM_INPUT_MESSAGES}.${messageIdx}.role`,
                messages[messageIdx].role,
              );
            }
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
  
        const name = `${lodash.snakeCase(className)}.${lodash.snakeCase(methodName)}`;
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
