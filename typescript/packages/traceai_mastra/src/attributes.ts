import {
  FISpanKind,
  MimeType,
  SemanticConventions,
} from "@traceai/fi-semantic-conventions";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

const MASTRA_SPAN_TYPE_ATTR = "mastra.span.type";

// Keys @mastra/otel-exporter writes during Mastra → OTel conversion.
const GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages";
const GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages";
const GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments";
const GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result";
const GEN_AI_TOOL_NAME = "gen_ai.tool.name";

const MASTRA_SPAN_TYPE_TO_FI_KIND: Record<string, FISpanKind> = {
  agent_run: FISpanKind.AGENT,
  tool_call: FISpanKind.TOOL,
  mcp_tool_call: FISpanKind.TOOL,
  model_generation: FISpanKind.LLM,
  model_step: FISpanKind.LLM,
  model_chunk: FISpanKind.LLM,
  scorer_run: FISpanKind.EVALUATOR,
  scorer_step: FISpanKind.EVALUATOR,
  workflow_run: FISpanKind.CHAIN,
  workflow_step: FISpanKind.CHAIN,
  workflow_conditional: FISpanKind.CHAIN,
  workflow_conditional_eval: FISpanKind.CHAIN,
  workflow_parallel: FISpanKind.CHAIN,
  workflow_loop: FISpanKind.CHAIN,
  workflow_sleep: FISpanKind.CHAIN,
  workflow_wait_event: FISpanKind.CHAIN,
  processor_run: FISpanKind.CHAIN,
  rag_embedding: FISpanKind.EMBEDDING,
  rag_vector_operation: FISpanKind.VECTOR_DB,
  rag_action: FISpanKind.RETRIEVER,
  rag_ingestion: FISpanKind.CHAIN,
  graph_action: FISpanKind.CHAIN,
  memory_operation: FISpanKind.CHAIN,
  workspace_action: FISpanKind.TOOL,
  generic: FISpanKind.CHAIN,
};

const safeJsonStringify = (obj: unknown): string => {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
};

const safeJsonParse = (value: unknown): unknown => {
  if (typeof value !== "string") return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
};

const isValidJsonString = (value: unknown): boolean => {
  const parsed = safeJsonParse(value);
  return typeof parsed === "object" && parsed !== null;
};

const getMimeTypeFromValue = (value: unknown): MimeType =>
  isValidJsonString(value) ? MimeType.JSON : MimeType.TEXT;

const setIfMissing = (
  span: ReadableSpan,
  key: string,
  value: unknown,
): void => {
  if (value === undefined || value === null) return;
  if (span.attributes[key] !== undefined) return;
  span.attributes[key] = value as never;
};

const setIOValue = (
  span: ReadableSpan,
  isInput: boolean,
  value: unknown,
): void => {
  if (value === undefined || value === null) return;
  const valueKey = isInput
    ? SemanticConventions.INPUT_VALUE
    : SemanticConventions.OUTPUT_VALUE;
  const mimeKey = isInput
    ? SemanticConventions.INPUT_MIME_TYPE
    : SemanticConventions.OUTPUT_MIME_TYPE;
  const asString = typeof value === "string" ? value : safeJsonStringify(value);
  setIfMissing(span, valueKey, asString);
  setIfMissing(span, mimeKey, getMimeTypeFromValue(asString));
};

/**
 * Extract a plain-text summary from a Mastra agent_run output payload.
 * Mastra emits agent_run output as `{ "files": [...], "text": "..." }`;
 * the dashboard's OUTPUT_VALUE looks best as the unwrapped text.
 */
const extractAgentOutputText = (raw: unknown): string | undefined => {
  const parsed = typeof raw === "string" ? safeJsonParse(raw) : raw;
  if (parsed && typeof parsed === "object" && "text" in parsed) {
    const text = (parsed as { text?: unknown }).text;
    if (typeof text === "string" && text.length > 0) return text;
  }
  return undefined;
};

/**
 * Map Mastra's `mastra.span.type` to FI's span kind, and copy
 * inputs/outputs/tool info onto FI's canonical attribute keys so the
 * Future AGI dashboard renders them in the Input/Output/Tool panels.
 */
export const addFIAttributesToMastraSpan = (span: ReadableSpan) => {
  const mastraType = span.attributes[MASTRA_SPAN_TYPE_ATTR];
  if (typeof mastraType !== "string") return;

  if (!span.attributes[SemanticConventions.FI_SPAN_KIND]) {
    const kind = MASTRA_SPAN_TYPE_TO_FI_KIND[mastraType];
    if (kind) span.attributes[SemanticConventions.FI_SPAN_KIND] = kind;
  }

  const attrs = span.attributes;
  const spanTypeKey = mastraType.toLowerCase();
  const rawInput = attrs[`mastra.${spanTypeKey}.input`];
  const rawOutput = attrs[`mastra.${spanTypeKey}.output`];

  switch (mastraType) {
    case "agent_run": {
      setIOValue(span, true, rawInput);
      if (rawOutput !== undefined) {
        setIfMissing(
          span,
          SemanticConventions.RAW_OUTPUT,
          typeof rawOutput === "string" ? rawOutput : safeJsonStringify(rawOutput),
        );
        const text = extractAgentOutputText(rawOutput);
        if (text !== undefined) {
          setIOValue(span, false, text);
        } else {
          setIOValue(span, false, rawOutput);
        }
      }
      break;
    }

    case "tool_call":
    case "mcp_tool_call": {
      const toolName = attrs[GEN_AI_TOOL_NAME];
      if (typeof toolName === "string") {
        setIfMissing(span, SemanticConventions.TOOL_NAME, toolName);
      }
      setIOValue(span, true, attrs[GEN_AI_TOOL_CALL_ARGUMENTS] ?? rawInput);
      setIOValue(span, false, attrs[GEN_AI_TOOL_CALL_RESULT] ?? rawOutput);
      break;
    }

    case "model_generation":
    case "model_step":
    case "model_chunk": {
      // Tokens (gen_ai.usage.*), model (gen_ai.request.model), and messages
      // (gen_ai.input.messages / gen_ai.output.messages) are already on the
      // FI canonical keys — no remapping needed. Expose RAW_INPUT/RAW_OUTPUT
      // and MIME types so the dashboard renders the LLM panel.
      const inputMessages = attrs[GEN_AI_INPUT_MESSAGES];
      const outputMessages = attrs[GEN_AI_OUTPUT_MESSAGES];
      if (inputMessages !== undefined) {
        setIfMissing(
          span,
          SemanticConventions.RAW_INPUT,
          typeof inputMessages === "string"
            ? inputMessages
            : safeJsonStringify(inputMessages),
        );
        setIfMissing(span, SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
      }
      if (outputMessages !== undefined) {
        setIfMissing(
          span,
          SemanticConventions.RAW_OUTPUT,
          typeof outputMessages === "string"
            ? outputMessages
            : safeJsonStringify(outputMessages),
        );
        setIfMissing(span, SemanticConventions.OUTPUT_MIME_TYPE, MimeType.JSON);
      }
      // Fallback for spans where Mastra wrote mastra.<type>.input/.output too.
      setIOValue(span, true, rawInput);
      setIOValue(span, false, rawOutput);
      break;
    }

    default: {
      setIOValue(span, true, rawInput);
      setIOValue(span, false, rawOutput);
      break;
    }
  }
};
