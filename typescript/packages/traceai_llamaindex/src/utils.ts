import type * as llamaindex from "llamaindex";
import { context, Context } from "@opentelemetry/api";

import { LLM } from "llamaindex";

export const shouldSendPrompts = () => {
  return true;
};

// Adopted from https://github.com/open-telemetry/opentelemetry-js/issues/2951#issuecomment-1214587378
export function bindAsyncGenerator<T = unknown, TReturn = any, TNext = unknown>(
  ctx: Context,
  generator: AsyncGenerator<T, TReturn, TNext>,
): AsyncGenerator<T, TReturn, TNext> {
  return {
    next: context.bind(ctx, generator.next.bind(generator)),
    return: context.bind(ctx, generator.return.bind(generator)),
    throw: context.bind(ctx, generator.throw.bind(generator)),

    [Symbol.asyncIterator]() {
      return bindAsyncGenerator(ctx, generator[Symbol.asyncIterator]());
    },

    [Symbol.asyncDispose]() {
      return Promise.resolve();
    },
  };
}

export async function* generatorWrapper(
  streamingResult: AsyncGenerator,
  ctx: Context,
  fn: () => void,
) {
  for await (const chunk of bindAsyncGenerator(ctx, streamingResult)) {
    yield chunk;
  }
  fn();
}

export interface StreamedChatOutput {
  message: llamaindex.ChatMessage;
  raw: object | null;
}

export async function* llmGeneratorWrapper(
  streamingResult: AsyncIterable<llamaindex.ChatResponseChunk>,
  ctx: Context,
  onEnd: (output: StreamedChatOutput) => void,
  onError: (error: Error) => void,
) {
  let content = "";
  // keyed by id: a call streams in partial chunks, the last chunk for an id is the complete one
  const toolCalls = new Map<string, llamaindex.PartialToolCall | llamaindex.ToolCall>();
  let usageRaw: object | null = null;
  let failed = false;

  try {
    for await (const chunk of bindAsyncGenerator(
      ctx,
      streamingResult as AsyncGenerator,
    )) {
      const { delta, options: chunkOptions, raw } =
        chunk as llamaindex.ChatResponseChunk;
      content += delta ?? "";
      if (chunkOptions && "toolCall" in chunkOptions) {
        for (const toolCall of (chunkOptions as llamaindex.ToolCallOptions).toolCall) {
          toolCalls.set(toolCall.id, { ...toolCall });
        }
      }
      if (raw && (raw as { usage?: unknown }).usage) {
        usageRaw = raw;
      }
      yield chunk;
    }
  } catch (error) {
    failed = true;
    onError(error as Error);
    throw error;
  } finally {
    if (!failed) {
      const options = toolCalls.size
        ? { toolCall: Array.from(toolCalls.values()) }
        : undefined;
      onEnd({ message: { role: "assistant", content, options }, raw: usageRaw });
    }
  }
}

export function isLLM(llm: any): llm is LLM {
  return (
    llm &&
    (llm as LLM).complete !== undefined &&
    (llm as LLM).chat !== undefined
  );
}
