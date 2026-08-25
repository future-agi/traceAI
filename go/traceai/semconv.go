package traceai

import "go.opentelemetry.io/otel/attribute"

// GenAI semantic convention keys.
// See https://opentelemetry.io/docs/specs/semconv/gen-ai/
const (
	AttrGenAISystem        = attribute.Key("gen_ai.system")
	AttrGenAIRequestModel  = attribute.Key("gen_ai.request.model")
	AttrGenAIResponseModel = attribute.Key("gen_ai.response.model")
	AttrGenAIOperationName = attribute.Key("gen_ai.operation.name")

	AttrGenAIRequestMaxTokens   = attribute.Key("gen_ai.request.max_tokens")
	AttrGenAIRequestTemperature = attribute.Key("gen_ai.request.temperature")
	AttrGenAIRequestTopP        = attribute.Key("gen_ai.request.top_p")

	AttrGenAIUsageInputTokens  = attribute.Key("gen_ai.usage.input_tokens")
	AttrGenAIUsageOutputTokens = attribute.Key("gen_ai.usage.output_tokens")

	AttrGenAIResponseFinishReasons = attribute.Key("gen_ai.response.finish_reasons")
	AttrGenAIResponseID            = attribute.Key("gen_ai.response.id")

	AttrGenAIPrompt     = attribute.Key("gen_ai.prompt")
	AttrGenAICompletion = attribute.Key("gen_ai.completion")

	// platform uses this to classify the span as an LLM call
	AttrGenAISpanKind = attribute.Key("gen_ai.span.kind")
)

const SpanKindLLM = "LLM"

const GenAISystemOpenAI = "openai"

const (
	OpChat       = "chat"
	OpCompletion = "text_completion"
	OpEmbedding  = "embeddings"
)
