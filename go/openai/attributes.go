package traceaiopenai

// Attribute keys this package writes. They are the same strings as
// fi_instrumentation.fi_types.SpanAttributes in Python traceAI (and the
// OpenTelemetry GenAI semantic-convention names where one exists), so a span
// from this package reads the same in Future AGI as one from traceai-openai.
// They are exported so spans your own code starts (an agent step, a tool call)
// can use the same names.
const (
	// Resource attributes Future AGI requires on the TracerProvider's
	// resource (not on spans).
	ResourceProjectName = "project_name"
	ResourceProjectType = "project_type"

	// GenAISpanKind types a span in Future AGI (LLM, TOOL, AGENT, CHAIN, ...).
	// This package sets it to SpanKindLLM, as traceai-openai does.
	GenAISpanKind = "gen_ai.span.kind"

	// GenAI semantic conventions.
	GenAIOperationName           = "gen_ai.operation.name"
	GenAIProviderName            = "gen_ai.provider.name"
	GenAIRequestModel            = "gen_ai.request.model"
	GenAIRequestTemperature      = "gen_ai.request.temperature"
	GenAIRequestTopP             = "gen_ai.request.top_p"
	GenAIRequestMaxTokens        = "gen_ai.request.max_tokens"
	GenAIRequestFrequencyPenalty = "gen_ai.request.frequency_penalty"
	GenAIRequestPresencePenalty  = "gen_ai.request.presence_penalty"
	GenAIRequestSeed             = "gen_ai.request.seed"
	GenAIRequestStopSequences    = "gen_ai.request.stop_sequences"
	GenAIResponseID              = "gen_ai.response.id"
	GenAIResponseModel           = "gen_ai.response.model"
	GenAIResponseFinishReasons   = "gen_ai.response.finish_reasons"
	GenAIUsageInputTokens        = "gen_ai.usage.input_tokens"
	GenAIUsageOutputTokens       = "gen_ai.usage.output_tokens"
	GenAIToolName                = "gen_ai.tool.name"
	GenAIToolCallID              = "gen_ai.tool.call.id"
	GenAIToolCallArguments       = "gen_ai.tool.call.arguments"
	GenAIToolCallResult          = "gen_ai.tool.call.result"
	GenAISystemInstructions      = "gen_ai.system_instructions"
	HTTPResponseStatusCode       = "http.response.status_code"
	HTTPRequestResendCount       = "http.request.resend_count"
	ErrorType                    = "error.type"

	// traceAI names (shared with Python/TypeScript/Java traceAI).
	GenAIUsageTotalTokens         = "gen_ai.usage.total_tokens"
	GenAIUsageCacheReadTokens     = "gen_ai.usage.cache_read_tokens"
	GenAIUsageReasoningTokens     = "gen_ai.usage.output_tokens.reasoning"
	GenAIRequestParameters        = "gen_ai.request.parameters"
	GenAIToolDefinitions          = "gen_ai.tool.definitions"
	GenAIInputMessages            = "gen_ai.input.messages"
	GenAIOutputMessages           = "gen_ai.output.messages"
	InputValue                    = "input.value"
	InputMimeType                 = "input.mime_type"
	OutputValue                   = "output.value"
	OutputMimeType                = "output.mime_type"
	ErrorMessage                  = "error.message"
	SessionID                     = "session.id"
	UserID                        = "user.id"
	MessageRole                   = "message.role"
	MessageContent                = "message.content"
	MessageContents               = "message.contents"
	MessageName                   = "message.name"
	MessageToolCallID             = "message.tool_call_id"
	MessageToolCalls              = "message.tool_calls"
	MessageContentType            = "message_content.type"
	MessageContentText            = "message_content.text"
	MessageContentImage           = "message_content.image"
	ToolCallID                    = "tool_call.id"
	ToolCallFunctionName          = "tool_call.function.name"
	ToolCallFunctionArgumentsJSON = "tool_call.function.arguments"
	ToolJSONSchema                = "tool.json_schema"
	MimeTypeJSON                  = "application/json"
	MimeTypeText                  = "text/plain"
)

// Values of [GenAISpanKind] (fi_instrumentation.fi_types.FiSpanKindValues).
const (
	SpanKindLLM   = "LLM"
	SpanKindTool  = "TOOL"
	SpanKindAgent = "AGENT"
	SpanKindChain = "CHAIN"
)
