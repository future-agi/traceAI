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

// Coding-agent usage and provenance. fi.agent.* is metadata, not an aggregation gate.
const (
	// GenAICostTotal: Total cost in USD for this additive usage owner only; omit unknown costs, preserve an explicitly reported zero.
	GenAICostTotal = "gen_ai.cost.total"
	// GenAICostInput: Input cost in USD for this additive usage owner only; total cost takes precedence over input plus output.
	GenAICostInput = "gen_ai.cost.input"
	// GenAICostOutput: Output cost in USD for this additive usage owner only; total cost takes precedence over input plus output.
	GenAICostOutput = "gen_ai.cost.output"
	// GenAIServerTimeToFirstToken: Measured time to first token in milliseconds for the Future AGI span attribute consumer. Not the OTel histogram unit (seconds). Convert seconds to milliseconds before emission; omit for reconstructed timing. The current gateway seconds writer is inconsistent with the dashboard and requires a separate migration.
	GenAIServerTimeToFirstToken = "gen_ai.server.time_to_first_token"
	// ToolName: Source tool name on TOOL spans; do not rely on the display span name.
	ToolName = "tool.name"
	// ToolCallID: Source tool-call identifier when available; omit rather than invent an identifier.
	ToolCallID = "tool_call.id"
	// FIUsageScope: Scope of canonical usage: request, attempt, turn or unknown. A turn aggregate is not a measured individual request.
	FIUsageScope = "fi.usage.scope"
	// FIUsageContribution: Use none for non-additive summaries and additive for the sole owner of canonical usage. Never duplicate canonical tokens or cost on summaries: older aggregate readers sum all span kinds.
	FIUsageContribution = "fi.usage.contribution"
	// FICostKind: Cost provenance understood by Future AGI: provider_reported, estimated or unknown. Unknown must not be represented by an invented zero.
	FICostKind = "fi.cost.kind"
	// FIAgentFidelity: Evidence fidelity: native-request, reconstructed-message, aggregate-turn or unknown. Only source-observed requests/messages may be LLM spans.
	FIAgentFidelity = "fi.agent.fidelity"
	// FIAgentTiming: Timing provenance: source, reconstructed or observed-event. Reconstructed message timing is not measured request latency or TTFT.
	FIAgentTiming = "fi.agent.timing"
	// FIAgentOutcome: Terminal outcome: success, error, interrupted or incomplete; missing stop events are incomplete, not success.
	FIAgentOutcome = "fi.agent.outcome"
	// FIAgentUsageAdditive: Boolean provenance flag for canonical usage ownership; not itself a backend aggregation gate. Request/attempt plans count each observed model operation once. For turn-only totals, place canonical usage exactly once on the last source-observed generation (marked scope=turn), or the AGENT root if there is no generation. Keep all other spans summary-only; never fabricate a model request.
	FIAgentUsageAdditive = "fi.agent.usage.additive"
	// FIAgentUsagePlan: One immutable plan per turn: request, attempt, turn or unknown. Never mix per-request counters and overlapping turn totals; unknown plans remain summary-only.
	FIAgentUsagePlan = "fi.agent.usage.plan"
	// FIAgentUsageCacheInInput: Boolean: source input count already includes cache reads/writes. Normalize input once; cache detail counters are subsets, not additional total tokens.
	FIAgentUsageCacheInInput = "fi.agent.usage.cache_in_input"
	// FIAgentUsageReasoningInOutput: Boolean: source output count already includes reasoning. Normalize output once; reasoning is a subset, not extra total tokens.
	FIAgentUsageReasoningInOutput = "fi.agent.usage.reasoning_in_output"
	// FIAgentCostSource: Source of the cost, for example reported or host-price-table; independent of whether this span contributes to totals.
	FIAgentCostSource = "fi.agent.cost.source"
	// FIAgentCostEstimator: Identifier/version of the estimator or price table for estimated costs; omit for reported costs. Never include credentials.
	FIAgentCostEstimator = "fi.agent.cost.estimator"
	// FIAgentGap: Bounded diagnostic code for missing evidence, for example MISSING_PARENT, MISSING_CHILD_STOP or UNRESOLVED_USAGE; not raw error content.
	FIAgentGap = "fi.agent.gap"
	// FIAgentTransport: Versioned transport contract identifier; not an endpoint URL or credential.
	FIAgentTransport = "fi.agent.transport"
	// FIAgentSourceCallID: Opaque integration correlation identifier; not a substitute for a source-provided tool_call.id.
	FIAgentSourceCallID = "fi.agent.source_call_id"
	// FIAgentSummaryInput: Non-additive normalized input token summary; never summed into canonical usage.
	FIAgentSummaryInput = "fi.agent.summary.input"
	// FIAgentSummaryOutput: Non-additive normalized output token summary; never summed into canonical usage.
	FIAgentSummaryOutput = "fi.agent.summary.output"
	// FIAgentSummaryTotal: Non-additive input plus output token summary; never summed into canonical usage.
	FIAgentSummaryTotal = "fi.agent.summary.total"
	// FIAgentSummaryCacheRead: Non-additive cache-read token summary, a subset of normalized input.
	FIAgentSummaryCacheRead = "fi.agent.summary.cache_read"
	// FIAgentSummaryCacheWrite: Non-additive cache-write token summary, a subset of normalized input.
	FIAgentSummaryCacheWrite = "fi.agent.summary.cache_write"
	// FIAgentSummaryReasoning: Non-additive reasoning token summary, a subset of normalized output.
	FIAgentSummaryReasoning = "fi.agent.summary.reasoning"
	// FIAgentSummaryCostUSD: Non-additive cost summary in USD; never place this amount in canonical cost as well unless this span owns the usage.
	FIAgentSummaryCostUSD = "fi.agent.summary.cost_usd"
	// FIAgentTags: JSON-encoded array of configured bounded tags, subject to the integration privacy policy.
	FIAgentTags = "fi.agent.tags"
	// FIAgentEvent: Agent lifecycle event type, for example compaction; not a model invocation.
	FIAgentEvent = "fi.agent.event"
	// FIAgentInputCompleteness: Source input completeness, for example partial; do not present an excerpt as the full model input.
	FIAgentInputCompleteness = "fi.agent.input.completeness"
	// FIAgentContentSource: Provenance of captured content, for example a source transcript; not evidence of native request capture.
	FIAgentContentSource = "fi.agent.content.source"
)
