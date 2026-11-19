package traceaianthropic

import "go.opentelemetry.io/otel/attribute"

// Attribute keys. The gen_ai.* keys are the OpenTelemetry GenAI semantic
// convention names, spelled the way anthropic-sdk-go v1.81.0+ spells them on its
// own spans, so a span looks the same whether the SDK or this package opened it.
// input.value, output.value, gen_ai.span.kind, session.id and user.id are the
// traceAI names that the Python and Java instrumentors use, and the ones that
// Future AGI reads for a span's Input, Output, type, session and user.
const (
	keySpanKind      = attribute.Key("gen_ai.span.kind")
	keyOperationName = attribute.Key("gen_ai.operation.name")
	keyProviderName  = attribute.Key("gen_ai.provider.name")

	keyRequestModel         = attribute.Key("gen_ai.request.model")
	keyRequestMaxTokens     = attribute.Key("gen_ai.request.max_tokens")
	keyRequestTemperature   = attribute.Key("gen_ai.request.temperature")
	keyRequestTopP          = attribute.Key("gen_ai.request.top_p")
	keyRequestTopK          = attribute.Key("gen_ai.request.top_k")
	keyRequestStream        = attribute.Key("gen_ai.request.stream")
	keyRequestStopSequences = attribute.Key("gen_ai.request.stop_sequences")
	keyRequestParameters    = attribute.Key("gen_ai.request.parameters")

	keyResponseID            = attribute.Key("gen_ai.response.id")
	keyResponseModel         = attribute.Key("gen_ai.response.model")
	keyResponseFinishReasons = attribute.Key("gen_ai.response.finish_reasons")

	keyUsageInputTokens           = attribute.Key("gen_ai.usage.input_tokens")
	keyUsageOutputTokens          = attribute.Key("gen_ai.usage.output_tokens")
	keyUsageCacheReadInputTokens  = attribute.Key("gen_ai.usage.cache_read.input_tokens")
	keyUsageCacheWriteInputTokens = attribute.Key("gen_ai.usage.cache_write.input_tokens")

	keyInputMessages      = attribute.Key("gen_ai.input.messages")
	keyOutputMessages     = attribute.Key("gen_ai.output.messages")
	keySystemInstructions = attribute.Key("gen_ai.system_instructions")
	keyToolDefinitions    = attribute.Key("gen_ai.tool.definitions")

	keyInputValue     = attribute.Key("input.value")
	keyInputMimeType  = attribute.Key("input.mime_type")
	keyOutputValue    = attribute.Key("output.value")
	keyOutputMimeType = attribute.Key("output.mime_type")

	keySessionID = attribute.Key("session.id")
	keyUserID    = attribute.Key("user.id")

	keyErrorType          = attribute.Key("error.type")
	keyHTTPStatusCode     = attribute.Key("http.response.status_code")
	keyHTTPResendCount    = attribute.Key("http.request.resend_count")
	keyAnthropicStop      = attribute.Key("anthropic.message.stop_reason")
	keyAnthropicTruncated = attribute.Key("anthropic.content.truncated")
)

const (
	spanKindLLM     = "LLM"
	operationChat   = "chat"
	providerName    = "anthropic"
	mimeJSON        = "application/json"
	sdkSpanName     = "anthropic.messages.create"
	redactedValue   = "__REDACTED__"
	errorTypeCancel = "canceled"
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
