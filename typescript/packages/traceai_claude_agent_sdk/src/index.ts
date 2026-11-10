export {
  ClaudeAgentSDKInstrumentation,
  INSTRUMENTATION_NAME,
  instrumentClaudeAgentSDK,
  resolveContentPolicy,
  shutdown,
  wrapQuery,
} from "./instrumentation";
export type { ClaudeAgentSDKInstrumentationConfig } from "./instrumentation";
export { isWrappedQuery } from "./queryWrapper";
export type { ContentPolicy } from "./spans";
export {
  BUILTIN_TOOLS,
  ClaudeAgentAttributes,
  ClaudeAgentSpanKind,
  FI_SPAN_KIND_BY_CLAUDE_KIND,
  SUBAGENT_TOOLS,
  SpanNames,
  TraceAIAttributes,
  getToolSource,
} from "./attributes";
export type { ClaudeAgentSpanKindValue, ToolSource } from "./attributes";
export { VERSION } from "./version";
