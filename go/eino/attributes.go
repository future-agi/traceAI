package traceaieino

// Attribute keys this package writes. They are the same strings as
// fi_instrumentation.fi_types.SpanAttributes in Python traceAI (and the
// OpenTelemetry GenAI semantic-convention names where one exists), so a span
// from this package reads the same in Future AGI as one from a Python traceAI
// instrumentor. They are exported so spans your own code starts can use the
// same names.
const (
	// Resource attributes Future AGI requires on the TracerProvider's
	// resource (not on spans).
	ResourceProjectName = "project_name"
	ResourceProjectType = "project_type"

	// GenAISpanKind types a span in Future AGI (LLM, TOOL, AGENT, CHAIN, ...).
	GenAISpanKind = "gen_ai.span.kind"

	// GenAI semantic conventions.
	GenAIOperationName         = "gen_ai.operation.name"
	GenAIRequestModel          = "gen_ai.request.model"
	GenAIRequestTemperature    = "gen_ai.request.temperature"
	GenAIRequestTopP           = "gen_ai.request.top_p"
	GenAIRequestMaxTokens      = "gen_ai.request.max_tokens"
	GenAIRequestStopSequences  = "gen_ai.request.stop_sequences"
	GenAIResponseFinishReasons = "gen_ai.response.finish_reasons"
	GenAIUsageInputTokens      = "gen_ai.usage.input_tokens"
	GenAIUsageOutputTokens     = "gen_ai.usage.output_tokens"
	GenAIToolName              = "gen_ai.tool.name"
	GenAIToolCallID            = "gen_ai.tool.call.id"
	GenAIToolCallArguments     = "gen_ai.tool.call.arguments"
	GenAIToolCallResult        = "gen_ai.tool.call.result"
	GenAIAgentName             = "gen_ai.agent.name"
	GenAIRetrievalQuery        = "gen_ai.retrieval.query"
	GenAIRetrievalTopK         = "gen_ai.retrieval.top_k"
	GenAIEmbeddingsDimension   = "gen_ai.embeddings.dimension.count"
	ErrorType                  = "error.type"

	// traceAI names (shared with Python/TypeScript/Java traceAI).
	GenAIUsageTotalTokens     = "gen_ai.usage.total_tokens"
	GenAIUsageCacheReadTokens = "gen_ai.usage.cache_read_tokens"
	GenAIUsageReasoningTokens = "gen_ai.usage.output_tokens.reasoning"
	GenAIRequestParameters    = "gen_ai.request.parameters"
	GenAIToolDefinitions      = "gen_ai.tool.definitions"
	GenAIInputMessages        = "gen_ai.input.messages"
	GenAIOutputMessages       = "gen_ai.output.messages"
	RetrievalDocuments        = "retrieval.documents"
	DocumentID                = "document.id"
	DocumentScore             = "document.score"
	DocumentContent           = "document.content"
	InputValue                = "input.value"
	InputMimeType             = "input.mime_type"
	OutputValue               = "output.value"
	OutputMimeType            = "output.mime_type"
	ErrorMessage              = "error.message"
	SessionID                 = "session.id"
	UserID                    = "user.id"
	MessageRole               = "message.role"
	MessageContent            = "message.content"
	MessageContents           = "message.contents"
	MessageName               = "message.name"
	MessageToolCallID         = "message.tool_call_id"
	MessageToolCalls          = "message.tool_calls"
	MessageContentType        = "message_content.type"
	MessageContentText        = "message_content.text"
	MessageContentImage       = "message_content.image"
	ToolCallID                = "tool_call.id"
	ToolCallFunctionName      = "tool_call.function.name"
	ToolCallFunctionArguments = "tool_call.function.arguments"
	ToolJSONSchema            = "tool.json_schema"
	MimeTypeJSON              = "application/json"
	MimeTypeText              = "text/plain"

	// Eino run identity, copied from callbacks.RunInfo. Go-only names.
	EinoComponent = "eino.component" // RunInfo.Component: ChatModel, Tool, Graph, Lambda, Agent, ...
	EinoType      = "eino.type"      // RunInfo.Type: the implementation, e.g. OpenAI
	EinoName      = "eino.name"      // RunInfo.Name: the node, tool or agent name
	// EinoAgentInterrupted is true on an agent span whose run stopped at an
	// interrupt (ADK interrupt/resume).
	EinoAgentInterrupted = "eino.agent.interrupted"
	// EinoAgentResumed is true on an agent span that resumed from an interrupt.
	EinoAgentResumed = "eino.agent.resumed"
	// EinoRetrieverDocumentCount is the number of documents a retriever
	// returned.
	EinoRetrieverDocumentCount = "eino.retriever.documents.count"
	// EinoEmbeddingCount is the number of vectors an embedder returned.
	EinoEmbeddingCount = "eino.embedding.count"
	// EinoIndexerDocumentCount is the number of documents an indexer stored.
	EinoIndexerDocumentCount = "eino.indexer.documents.count"
)

// Values of [GenAISpanKind] (fi_instrumentation.fi_types.FiSpanKindValues).
const (
	SpanKindLLM       = "LLM"
	SpanKindTool      = "TOOL"
	SpanKindAgent     = "AGENT"
	SpanKindChain     = "CHAIN"
	SpanKindRetriever = "RETRIEVER"
	SpanKindEmbedding = "EMBEDDING"
	SpanKindUnknown   = "UNKNOWN"
)
