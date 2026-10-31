import { expect, it } from '@jest/globals';
import * as conventions from '../index';

// Assignment to these literal types must fail if an export widens to string.
// Keep this separate from runtime coverage, which also runs on pre-fix code.
type GenAIAttributeTypes = {
  LLM_INPUT_MESSAGES: "gen_ai.input.messages";
  GEN_AI_INPUT_MESSAGES: "gen_ai.input.messages";
  LLM_PROMPTS: "gen_ai.prompts";
  GEN_AI_PROMPTS: "gen_ai.prompts";
  LLM_INVOCATION_PARAMETERS: "gen_ai.request.parameters";
  GEN_AI_REQUEST_PARAMETERS: "gen_ai.request.parameters";
  LLM_OUTPUT_MESSAGES: "gen_ai.output.messages";
  GEN_AI_OUTPUT_MESSAGES: "gen_ai.output.messages";
  LLM_MODEL_NAME: "gen_ai.request.model";
  GEN_AI_REQUEST_MODEL: "gen_ai.request.model";
  LLM_PROVIDER: "gen_ai.provider.name";
  GEN_AI_PROVIDER_NAME: "gen_ai.provider.name";
  LLM_SYSTEM: "gen_ai.provider.name";
  LLM_TOKEN_COUNT_COMPLETION: "gen_ai.usage.output_tokens";
  GEN_AI_USAGE_OUTPUT_TOKENS: "gen_ai.usage.output_tokens";
  LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING: "gen_ai.usage.output_tokens.reasoning";
  GEN_AI_USAGE_OUTPUT_TOKENS_REASONING: "gen_ai.usage.output_tokens.reasoning";
  LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO: "gen_ai.usage.output_tokens.audio";
  GEN_AI_USAGE_OUTPUT_TOKENS_AUDIO: "gen_ai.usage.output_tokens.audio";
  LLM_TOKEN_COUNT_PROMPT: "gen_ai.usage.input_tokens";
  GEN_AI_USAGE_INPUT_TOKENS: "gen_ai.usage.input_tokens";
  LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE: "gen_ai.usage.cache_write_tokens";
  GEN_AI_USAGE_CACHE_WRITE_TOKENS: "gen_ai.usage.cache_write_tokens";
  LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ: "gen_ai.usage.cache_read_tokens";
  GEN_AI_USAGE_CACHE_READ_TOKENS: "gen_ai.usage.cache_read_tokens";
  LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO: "gen_ai.usage.input_tokens.audio";
  GEN_AI_USAGE_INPUT_TOKENS_AUDIO: "gen_ai.usage.input_tokens.audio";
  LLM_TOKEN_COUNT_TOTAL: "gen_ai.usage.total_tokens";
  GEN_AI_USAGE_TOTAL_TOKENS: "gen_ai.usage.total_tokens";
  LLM_FUNCTION_CALL: "gen_ai.tool.call";
  GEN_AI_TOOL_CALL: "gen_ai.tool.call";
  LLM_TOOLS: "gen_ai.tool.definitions";
  GEN_AI_TOOL_DEFINITIONS: "gen_ai.tool.definitions";
};

it('preserves canonical and legacy literal types on both public surfaces', () => {
  const named: GenAIAttributeTypes = conventions;
  const attributes: GenAIAttributeTypes = conventions.SemanticConventions;
  expect(named.GEN_AI_INPUT_MESSAGES).toBe(attributes.LLM_INPUT_MESSAGES);
});
