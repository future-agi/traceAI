import { SemanticConventions, LLMSystem, LLMProvider, MimeType } from "@traceai/fi-semantic-conventions";
import { Attributes, AttributeValue } from "@opentelemetry/api";
import Anthropic from "@anthropic-ai/sdk";
import { safelyJSONStringify } from "@traceai/fi-core";

// Using Anthropic.TypeName based on SDK documentation for v0.27.3

/**
 * Extracts attributes from Anthropic's MessageParam array and system prompt for LLM input.
 */
export function getAnthropicInputMessagesAttributes(
  params: Anthropic.MessageCreateParams,
): Attributes {
  const attributes: Attributes = {};
  let messageIndex = 0;

  if (params.system) {
    const systemMessagePrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.${messageIndex}.`;
    attributes[`${systemMessagePrefix}${SemanticConventions.MESSAGE_ROLE}`] = "system";
    attributes[`${systemMessagePrefix}${SemanticConventions.MESSAGE_CONTENT}`] = 
        typeof params.system === 'string' ? params.system : safelyJSONStringify(params.system) ?? "";
    messageIndex++;
  }

  params.messages.forEach((message: Anthropic.MessageParam) => {
    const currentMessagePrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.${messageIndex}.`;
    attributes[`${currentMessagePrefix}${SemanticConventions.MESSAGE_ROLE}`] = message.role;

    if (typeof message.content === "string") {
      attributes[`${currentMessagePrefix}${SemanticConventions.MESSAGE_CONTENT}`] = message.content;
    } else if (Array.isArray(message.content)) {
      message.content.forEach((contentBlock: Anthropic.TextBlockParam | Anthropic.ImageBlockParam | Anthropic.ToolUseBlockParam | Anthropic.ToolResultBlockParam, blockIndex: number) => {
        const contentBlockPrefix = `${currentMessagePrefix}${SemanticConventions.MESSAGE_CONTENTS}.${blockIndex}.`;
        attributes[`${contentBlockPrefix}${SemanticConventions.MESSAGE_CONTENT_TYPE}`] = contentBlock.type;
        
        if (contentBlock.type === "text") {
          attributes[`${contentBlockPrefix}${SemanticConventions.MESSAGE_CONTENT_TEXT}`] = contentBlock.text;
        } else if (contentBlock.type === "image") {
          attributes[`${contentBlockPrefix}${SemanticConventions.MESSAGE_CONTENT_IMAGE}`] = safelyJSONStringify(contentBlock.source) ?? "";
        } else if (contentBlock.type === "tool_use") {
             attributes[`${contentBlockPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = safelyJSONStringify(contentBlock) ?? "";
        } else if (contentBlock.type === "tool_result") {
             attributes[`${contentBlockPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = safelyJSONStringify(contentBlock) ?? "";
        }
      });
    }
    messageIndex++;
  });
  return attributes;
}

/**
 * Extracts attributes from an Anthropic Message object for LLM output.
 * This is simplified to match Python SDK's output structure.
 */
export function getAnthropicOutputMessagesAttributes(
  response: Anthropic.Message,
): Attributes {
  const attributes: Attributes = {};
  const messagePrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.`; 

  attributes[`${messagePrefix}${SemanticConventions.MESSAGE_ROLE}`] = response.role;

  let lastTextContent = "";

  if (response.content && Array.isArray(response.content)) {
    let toolCallCounter = 0;
    response.content.forEach((contentBlock: Anthropic.ContentBlock) => {
      if (contentBlock.type === "text") {
        lastTextContent = (contentBlock as Anthropic.TextBlock).text;
      } else if (contentBlock.type === "tool_use") {
        const toolCallPrefix = `${messagePrefix}${SemanticConventions.MESSAGE_TOOL_CALLS}.${toolCallCounter}.`;
        const toolUseBlock = contentBlock as Anthropic.ToolUseBlock;
        // Python SDK does not seem to add TOOL_CALL_ID for output tool_use in _get_output_messages.
        // attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_ID}`] = toolUseBlock.id;
        attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] = toolUseBlock.name;
        attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] = safelyJSONStringify(toolUseBlock.input) ?? "";
        toolCallCounter++;
      }
    });
  }
  
  if (lastTextContent) {
    attributes[`${messagePrefix}${SemanticConventions.MESSAGE_CONTENT}`] = lastTextContent;
  }
  // If only tool_calls and no text, MESSAGE_CONTENT remains unset, matching Python behavior.

  return attributes;
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
 * Extracts attributes from Anthropic tool definitions if provided in params.
 */
export function getAnthropicToolsAttributes(
  params: Anthropic.MessageCreateParams,
): Attributes {
  const attributes: Attributes = {};
  if (params.tools && Array.isArray(params.tools)) {
    params.tools.forEach((tool: Anthropic.Tool, index: number) => {
      const toolPrefix = `${SemanticConventions.LLM_TOOLS}.${index}.`;
      attributes[`${toolPrefix}${SemanticConventions.TOOL_NAME}`] = tool.name;
      if (tool.description) {
        attributes[`${toolPrefix}${SemanticConventions.TOOL_DESCRIPTION}`] = tool.description;
      }
      attributes[`${toolPrefix}${SemanticConventions.TOOL_JSON_SCHEMA}`] = safelyJSONStringify(tool.input_schema) ?? "";
    });
  }
  return attributes;
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
