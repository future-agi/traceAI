package traceai

import "go.opentelemetry.io/otel/attribute"

// GenAI semantic convention keys.
// See https://opentelemetry.io/docs/specs/semconv/gen-ai/
const (
	AttrGenAISystem          = attribute.Key("gen_ai.system")
	AttrGenAIRequestModel    = attribute.Key("gen_ai.request.model")
	AttrGenAIResponseModel   = attribute.Key("gen_ai.response.model")
	AttrGenAIOperationName   = attribute.Key("gen_ai.operation.name")

	AttrGenAIRequestMaxTokens     = attribute.Key("gen_ai.request.max_tokens")
	AttrGenAIRequestTemperature   = attribute.Key("gen_ai.request.temperature")
	AttrGenAIRequestTopP          = attribute.Key("gen_ai.request.top_p")
	AttrGenAIRequestStopSequences = attribute.Key("gen_ai.request.stop_sequences")

	AttrGenAIUsageInputTokens  = attribute.Key("gen_ai.usage.input_tokens")
	AttrGenAIUsageOutputTokens = attribute.Key("gen_ai.usage.output_tokens")

	AttrGenAIResponseFinishReasons = attribute.Key("gen_ai.response.finish_reasons")
	AttrGenAIResponseID            = attribute.Key("gen_ai.response.id")

	AttrGenAIPrompt     = attribute.Key("gen_ai.prompt")
	AttrGenAICompletion = attribute.Key("gen_ai.completion")
)

const (
	GenAISystemOpenAI    = "openai"
	GenAISystemAnthropic = "anthropic"
	GenAISystemCohere    = "cohere"
	GenAISystemGoogle    = "google"
)

const (
	OpChat       = "chat"
	OpCompletion = "completion"
	OpEmbedding  = "embedding"
)
