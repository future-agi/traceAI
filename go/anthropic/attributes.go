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
