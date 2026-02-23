import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { Attributes } from "@opentelemetry/api";
import {
  ResponseCreateParamsBase,
  ResponseInputItem,
  ResponseOutputItem,
  ResponseStreamEvent,
  Response as ResponseType,
} from "openai/resources/responses/responses";
import { Stream } from "openai/streaming";
import { safelyJSONStringify } from "@traceai/fi-core";

/**
 * Serialize a non-role-based response item to a plain object
 */
function serializeResponseItem(
  item: Exclude<ResponseInputItem | ResponseOutputItem, { role: string }>,
): Record<string, unknown> {
  switch (item.type) {
    case "function_call":
      return {
        role: "assistant",
        tool_calls: [{ id: item.call_id, function: { name: item.name, arguments: item.arguments } }],
      };
    case "function_call_output":
      return { role: "tool", tool_call_id: item.call_id, content: item.output };
    case "reasoning":
      return {
        role: "assistant",
        content: item.summary.map((s) =>
          s.type === "summary_text" ? { type: "summary_text", text: s.text } : { type: s.type },
        ),
      };
    case "item_reference":
      return { type: "item_reference" };
    case "file_search_call":
      if (!item.results) {
        return {
          role: "assistant",
          tool_calls: [{ id: item.id, function: { name: item.type, arguments: JSON.stringify(item.queries) } }],
        };
      }
      return { role: "tool", tool_call_id: item.id, content: JSON.stringify(item.results) };
    case "computer_call":
      return {
        role: "assistant",
        tool_calls: [{ id: item.id, function: { name: item.type, arguments: JSON.stringify(item.action) } }],
      };
    case "computer_call_output":
      return { role: "tool", tool_call_id: item.call_id, content: JSON.stringify(item.output) };
    case "web_search_call":
      return {
        role: "assistant",
        tool_calls: [{ id: item.id, function: { name: item.type } }],
      };
  }
  return {};
}

/**
 * Serialize a response item (message or non-message) to a plain object for JSON blob
 */
function serializeResponseItemMessage(
  itemMessage: ResponseInputItem | ResponseOutputItem,
): Record<string, unknown> {
  const message =
    typeof itemMessage === "string"
      ? ({ content: itemMessage, role: "user" } satisfies ResponseInputItem)
      : itemMessage;
  if (!("role" in message)) {
    return serializeResponseItem(message);
  }
  const obj: Record<string, unknown> = { role: message.role };
  if (typeof message.content === "string") {
    obj.content = message.content;
  } else if (Array.isArray(message.content)) {
    obj.content = message.content.map((part) => {
      if (part.type === "input_text") return { type: "input_text", text: part.text };
      if (part.type === "output_text") return { type: "output_text", text: part.text };
      if (part.type === "input_image") return { type: "input_image", image_url: part.image_url };
      if (part.type === "refusal") return { type: "refusal", refusal: part.refusal };
      return { type: part.type };
    });
  }
  return obj;
}

export function getResponsesInputMessagesAttributes(
  body: ResponseCreateParamsBase,
): Attributes {
  const items: ResponseInputItem[] = [];
  if (body.instructions) {
    items.push({ content: body.instructions, role: "system" });
  }
  if (typeof body.input === "string") {
    items.push({ content: body.input, role: "user" });
  } else {
    items.push(...body.input);
  }
  const messages = items.map((item) => serializeResponseItemMessage(item));
  return {
    [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
  };
}

export function getResponsesUsageAttributes(
  response: ResponseType,
): Attributes {
  if (response.usage) {
    return {
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]:
        response.usage.output_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: response.usage.input_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: response.usage.total_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]:
        response.usage.input_tokens_details?.cached_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]:
        response.usage.output_tokens_details?.reasoning_tokens,
    };
  }
  return {};
}

export function getResponsesOutputMessagesAttributes(
  response: ResponseType,
): Attributes {
  const messages = response.output.map((item) => serializeResponseItemMessage(item));
  return {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
  };
}

export async function consumeResponseStreamEvents(
  stream: Stream<ResponseStreamEvent>,
): Promise<ResponseType | undefined> {
  let response: ResponseType | undefined;

  for await (const event of stream) {
    switch (event.type) {
      case "response.completed": {
        response = event.response;
        break;
      }
    }
  }

  return response;
}