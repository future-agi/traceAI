import {
    FISpanKind,
    SemanticConventions,
  } from "@traceai/fi-semantic-conventions";
  import {
    AISemanticConvention,
    AISemanticConventions,
  } from "./AISemanticConventions";
  import { FISemanticConventionKey } from "./types";
  
  /**
   * A map of Vercel AI SDK function names to FI span kinds.
   * @see https://sdk.vercel.ai/docs/ai-sdk-core/telemetry#collected-data
   * These are set on Vercel spans as under the operation.name attribute.
   * They are preceded by "ai.<wrapper-name>" and may be followed by a user provided functionID.
   * @example ai.<wrapper-name>.<ai-sdk-function-name> <user provided functionId>
   * @example ai.generateText.doGenerate my-chat-call
   */
  export const VercelSDKFunctionNameToSpanKindMap = new Map([
    ["ai.generateText", FISpanKind.CHAIN],
    ["ai.generateText.doGenerate", FISpanKind.LLM],
    ["ai.generateObject", FISpanKind.CHAIN],
    ["ai.generateObject.doGenerate", FISpanKind.LLM],
    ["ai.streamText", FISpanKind.CHAIN],
    ["ai.streamText.doStream", FISpanKind.LLM],
    ["ai.streamObject", FISpanKind.CHAIN],
    ["ai.streamObject.doStream", FISpanKind.LLM],
    ["ai.embed", FISpanKind.CHAIN],
    ["ai.embed.doEmbed", FISpanKind.EMBEDDING],
    ["ai.embedMany", FISpanKind.CHAIN],
    ["ai.embedMany.doEmbed", FISpanKind.EMBEDDING],
    ["ai.toolCall", FISpanKind.TOOL],
  ]);
  
  export const AISemConvToFISemConvMap: Record<
    AISemanticConvention,
    FISemanticConventionKey
  > = {
    [AISemanticConventions.MODEL_ID]: SemanticConventions.LLM_MODEL_NAME,
    [AISemanticConventions.SETTINGS]:
      SemanticConventions.LLM_INVOCATION_PARAMETERS,
    [AISemanticConventions.METADATA]: SemanticConventions.METADATA,
    [AISemanticConventions.TOKEN_COUNT_COMPLETION]:
      SemanticConventions.LLM_TOKEN_COUNT_COMPLETION,
    [AISemanticConventions.TOKEN_COUNT_PROMPT]:
      SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
    [AISemanticConventions.RESPONSE_TEXT]: SemanticConventions.OUTPUT_VALUE,
    [AISemanticConventions.RESPONSE_TOOL_CALLS]:
      SemanticConventions.MESSAGE_TOOL_CALLS,
    [AISemanticConventions.RESPONSE_OBJECT]: SemanticConventions.OUTPUT_VALUE,
    [AISemanticConventions.PROMPT]: SemanticConventions.INPUT_VALUE,
    [AISemanticConventions.PROMPT_MESSAGES]:
      SemanticConventions.LLM_INPUT_MESSAGES,
    [AISemanticConventions.EMBEDDING_TEXT]: SemanticConventions.EMBEDDING_TEXT,
    [AISemanticConventions.EMBEDDING_VECTOR]:
      SemanticConventions.EMBEDDING_VECTOR,
    [AISemanticConventions.EMBEDDING_TEXTS]: SemanticConventions.EMBEDDING_TEXT,
    [AISemanticConventions.EMBEDDING_VECTORS]:
      SemanticConventions.EMBEDDING_VECTOR,
    [AISemanticConventions.TOOL_CALL_ID]: SemanticConventions.TOOL_CALL_ID,
    [AISemanticConventions.TOOL_CALL_NAME]: SemanticConventions.TOOL_NAME,
    [AISemanticConventions.TOOL_CALL_ARGS]: SemanticConventions.TOOL_PARAMETERS,
    [AISemanticConventions.TOOL_CALL_RESULT]: SemanticConventions.OUTPUT_VALUE,
  } as const;