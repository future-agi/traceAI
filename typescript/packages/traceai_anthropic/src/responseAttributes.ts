import { SemanticConventions, LLMSystem, LLMProvider, MimeType } from "@traceai/fi-semantic-conventions";
import { Attributes, AttributeValue } from "@opentelemetry/api";
import Anthropic from "@anthropic-ai/sdk";
import { safelyJSONStringify } from "@traceai/fi-core";

/**
 * Extracts input messages as a JSON blob from Anthropic's MessageParam array and system prompt.
 */
export function getAnthropicInputMessagesAttributes(
  params: Anthropic.MessageCreateParams,
): Attributes {
  const messages: Record<string, unknown>[] = [];

  if (params.system) {
    messages.push({
      role: "system",
      content: typeof params.system === 'string' ? params.system : params.system,
    });
  }

  params.messages.forEach((message: Anthropic.MessageParam) => {
    const obj: Record<string, unknown> = { role: message.role };

    if (typeof message.content === "string") {
      obj.content = message.content;
    } else if (Array.isArray(message.content)) {
      obj.content = message.content.map((contentBlock: any) => {
        if (contentBlock.type === "text") {
          return { type: "text", text: contentBlock.text };
        } else if (contentBlock.type === "image") {
          return { type: "image", source: contentBlock.source };
        } else if (contentBlock.type === "tool_use") {
          return contentBlock;
        } else if (contentBlock.type === "tool_result") {
          return contentBlock;
        }
        return { type: contentBlock.type };
      });
    }
    messages.push(obj);
  });

  return {
    [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
  };
}

/**
 * Extracts output messages as a JSON blob from an Anthropic Message object.
 */
export function getAnthropicOutputMessagesAttributes(
  response: Anthropic.Message,
): Attributes {
  const outputMessage: Record<string, unknown> = { role: response.role };
  let lastTextContent = "";
  const toolCalls: Record<string, unknown>[] = [];

  if (response.content && Array.isArray(response.content)) {
    response.content.forEach((contentBlock: Anthropic.ContentBlock) => {
      if (contentBlock.type === "text") {
        lastTextContent = (contentBlock as Anthropic.TextBlock).text;
      } else if (contentBlock.type === "tool_use") {
        const toolUseBlock = contentBlock as Anthropic.ToolUseBlock;
        toolCalls.push({
          function: { name: toolUseBlock.name, arguments: safelyJSONStringify(toolUseBlock.input) ?? "{}" },
        });
      }
    });
  }

  if (lastTextContent) {
    outputMessage.content = lastTextContent;
  }
  if (toolCalls.length > 0) {
    outputMessage.tool_calls = toolCalls;
  }

  return {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([outputMessage]) ?? "[]",
  };
}

/**
 * Extracts usage attributes from an Anthropic API response.
 */
export function getAnthropicUsageAttributes(
  response: Anthropic.Message | { usage?: Anthropic.Usage },
): Attributes {
  const usage = response.usage;
  if (usage) {
    const attributes: Attributes = {
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: usage.input_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: usage.output_tokens,
    };
    if (usage.input_tokens !== undefined && usage.output_tokens !== undefined) {
        attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] = usage.input_tokens + usage.output_tokens;
    }
    return attributes;
  }
  return {};
}

/**
 * Extracts tool definitions as a JSON blob from Anthropic params.
 */
export function getAnthropicToolsAttributes(
  params: Anthropic.MessageCreateParams,
): Attributes {
  if (params.tools && Array.isArray(params.tools)) {
    return {
      [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(params.tools) ?? "[]",
    };
  }
  return {};
}

/**
 * Aggregates Anthropic message stream events into a final Message object structure
 * and collects all chunks for raw output.
 */
export function aggregateAnthropicStreamEvents(
  chunks: Anthropic.MessageStreamEvent[],
): { reconstructedMessage: Anthropic.Message & { usage: Anthropic.Usage }, rawOutputChunks: Anthropic.MessageStreamEvent[] } {
  const reconstructedMessage: Partial<Anthropic.Message & { usage: Anthropic.Usage }> = {
    id: "", 
    content: [],
    model: "", 
    role: "assistant", 
    stop_reason: null,
    stop_sequence: null,
    type: "message", 
    usage: { input_tokens: 0, output_tokens: 0 }, 
  };
  const rawOutputChunks = [...chunks]; 

  let currentTextContent = "";
  let currentToolUseBlock: Partial<Anthropic.ToolUseBlock & { inputJson?: string}> = {}; 
  let contentBlocks: Anthropic.ContentBlock[] = [];

  chunks.forEach((event: Anthropic.MessageStreamEvent) => {
    switch (event.type) {
      case "message_start":
        reconstructedMessage.id = event.message.id;
        reconstructedMessage.model = event.message.model;
        reconstructedMessage.role = event.message.role;
        reconstructedMessage.type = event.message.type;
        if (event.message.usage) {
            reconstructedMessage.usage = { 
                input_tokens: event.message.usage.input_tokens,
                output_tokens: reconstructedMessage.usage?.output_tokens || 0 
            };
        }
        break;
      case "message_delta":
        if (event.delta.stop_reason) reconstructedMessage.stop_reason = event.delta.stop_reason;
        if (event.delta.stop_sequence) reconstructedMessage.stop_sequence = event.delta.stop_sequence;
        if (event.usage?.output_tokens && reconstructedMessage.usage) {
             reconstructedMessage.usage.output_tokens = (reconstructedMessage.usage.output_tokens || 0) + event.usage.output_tokens;
        }
        break;
      case "content_block_start":
        const contentBlockStart = event.content_block;
        if (contentBlockStart.type === "text") {
        } else if (contentBlockStart.type === "tool_use") {
          if (currentTextContent) {
            contentBlocks.push({ type: "text", text: currentTextContent } as Anthropic.TextBlock);
            currentTextContent = "";
          }
          currentToolUseBlock = { 
            type: "tool_use", 
            id: contentBlockStart.id, 
            name: contentBlockStart.name,
            input: {},
            inputJson: "", 
          };
        }
        break;
      case "content_block_delta":
        const contentBlockDelta = event.delta;
        if (contentBlockDelta.type === "text_delta") {
          currentTextContent += contentBlockDelta.text;
        } else if (contentBlockDelta.type === "input_json_delta" && currentToolUseBlock.type === "tool_use") {
          currentToolUseBlock.inputJson = (currentToolUseBlock.inputJson || "") + contentBlockDelta.partial_json;
        }
        break;
      case "content_block_stop":
        if (currentToolUseBlock.type === "tool_use" && currentToolUseBlock.id && currentToolUseBlock.name) {
          try {
            currentToolUseBlock.input = JSON.parse(currentToolUseBlock.inputJson || "{}");
          } catch (e) {
            console.error("Failed to parse tool_use input JSON from stream:", e);
            currentToolUseBlock.input = {}; 
          }
          const { inputJson, ...finalToolBlock } = currentToolUseBlock; 
          contentBlocks.push(finalToolBlock as Anthropic.ToolUseBlock);
          currentToolUseBlock = {}; 
        } else if (currentTextContent) {
          contentBlocks.push({ type: "text", text: currentTextContent } as Anthropic.TextBlock);
          currentTextContent = "";
        }
        break;
      case "message_stop":
        if (currentTextContent) {
          contentBlocks.push({ type: "text", text: currentTextContent } as Anthropic.TextBlock);
          currentTextContent = "";
        }
        break;
    }
  });

  if (currentTextContent) {
    contentBlocks.push({ type: "text", text: currentTextContent } as Anthropic.TextBlock);
  }
  
  reconstructedMessage.content = contentBlocks;

  if (reconstructedMessage.usage && reconstructedMessage.usage.input_tokens !== undefined && reconstructedMessage.usage.output_tokens !== undefined) {
    // This value is for our span, not to modify the Anthropic.Usage object itself.
    // (reconstructedMessage.usage as Anthropic.Usage).total_tokens = reconstructedMessage.usage.input_tokens + reconstructedMessage.usage.output_tokens;
    // The line above is removed as Anthropic.Usage type doesn't have total_tokens.
    // The actual total_tokens for the span will be set directly in getAnthropicUsageAttributes.
  }

  return { reconstructedMessage: reconstructedMessage as Anthropic.Message & { usage: Anthropic.Usage }, rawOutputChunks };
}
