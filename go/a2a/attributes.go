package traceaia2a

// Attribute keys this package writes. The gen_ai.a2a.* names are the ones
// fi_instrumentation.fi_types.SpanAttributes defines for the Python traceai-a2a
// package; they are written only when a2a-go reports the same fact. The rest
// are the shared traceAI names (input.value, session.id, ...) and OpenTelemetry
// semantic-convention names. They are exported so spans your own code starts
// can use the same names.
const (
	// Resource attributes Future AGI requires on the TracerProvider's
	// resource (not on spans).
	ResourceProjectName = "project_name"
	ResourceProjectType = "project_type"

	// GenAISpanKind types a span in Future AGI (LLM, TOOL, AGENT, CHAIN, ...).
	// Every span this package starts is an AGENT span: a call to a remote
	// agent. It is never LLM; a model call inside the remote agent is not
	// observed here.
	GenAISpanKind      = "gen_ai.span.kind"
	GenAIOperationName = "gen_ai.operation.name"

	// A2A names shared with Python traceai-a2a.
	A2ATaskID          = "gen_ai.a2a.task.id"
	A2ATaskState       = "gen_ai.a2a.task.state"
	A2AAgentURL        = "gen_ai.a2a.agent.url"
	A2AAgentCardName   = "gen_ai.a2a.agent.card.name"
	A2AAgentCardVer    = "gen_ai.a2a.agent.card.version"
	A2AMessageRole     = "gen_ai.a2a.message.role"
	A2AMessagePartsCnt = "gen_ai.a2a.message.parts.count"
	A2AArtifactType    = "gen_ai.a2a.artifact.type"
	A2AStreaming       = "gen_ai.a2a.streaming"

	// Go-only A2A names (not in the Python enum yet).
	A2AContextID        = "gen_ai.a2a.context.id"
	A2AStreamEventCount = "gen_ai.a2a.stream.event_count"
	A2AEventType        = "gen_ai.a2a.event.type"

	// Span event name for one streamed A2A event.
	EventStreamEvent = "a2a.event"

	// traceAI names shared with Python/TypeScript/Java traceAI.
	InputValue     = "input.value"
	InputMimeType  = "input.mime_type"
	OutputValue    = "output.value"
	OutputMimeType = "output.mime_type"
	SessionID      = "session.id"
	UserID         = "user.id"
	ErrorType      = "error.type"
	ErrorMessage   = "error.message"
	MimeTypeText   = "text/plain"
	MimeTypeJSON   = "application/json"

	// Keys TraceConfig masks on spans your own code starts (the same rules as
	// the other traceAI packages). This package does not write them.
	GenAIInputMessages      = "gen_ai.input.messages"
	GenAIOutputMessages     = "gen_ai.output.messages"
	GenAIRequestParameters  = "gen_ai.request.parameters"
	GenAISystemInstructions = "gen_ai.system_instructions"
	GenAIToolCallArguments  = "gen_ai.tool.call.arguments"
	GenAIToolCallResult     = "gen_ai.tool.call.result"
	MessageContent          = "message.content"
	MessageContents         = "message.contents"
	MessageContentText      = "message_content.text"
	MessageContentImage     = "message_content.image"
)

// Values of [GenAISpanKind] (fi_instrumentation.fi_types.FiSpanKindValues).
const (
	SpanKindAgent = "AGENT"
	SpanKindTool  = "TOOL"
	SpanKindChain = "CHAIN"
)

// Values of [GenAIOperationName] per traced a2aclient method.
const (
	OperationSend      = "a2a.send"
	OperationGetTask   = "a2a.get_task"
	OperationCancel    = "a2a.cancel_task"
	OperationSubscribe = "a2a.subscribe_to_task"
)
