// Package traceaiopenai traces the official OpenAI Go SDK
// (github.com/openai/openai-go/v3) for Future AGI.
//
// [Middleware] is an option.Middleware, so the SDK client keeps its own HTTP
// client and transport policy. Chat Completions and Responses creation calls
// (streaming or not) produce LLM client spans; Embeddings calls produce
// EMBEDDING client spans. Names are "chat <model>", "generate_content <model>"
// and "embeddings <model>". Each HTTP attempt records model attribution, usage
// when returned by the API, and supported content using traceAI attributes.
// Embedding vectors and opaque file/audio/hosted-tool payloads are not captured.
//
// Content is captured by default, as in every traceAI instrumentor. The
// traceAI switches turn it off: FI_HIDE_INPUTS, FI_HIDE_OUTPUTS,
// FI_HIDE_INPUT_MESSAGES, FI_HIDE_OUTPUT_MESSAGES, FI_HIDE_INPUT_TEXT,
// FI_HIDE_OUTPUT_TEXT, FI_HIDE_INPUT_IMAGES, FI_HIDE_LLM_INVOCATION_PARAMETERS
// and FI_BASE64_IMAGE_MAX_LENGTH, or a [TraceConfig] in code.
//
// The package depends only on the OpenTelemetry API and the OpenAI SDK. It
// does not create an exporter: build a TracerProvider with the stock OTLP/HTTP
// exporter pointed at Future AGI (see the example directory) and pass it with
// [WithTracerProvider], or install it as the global provider.
//
//	client := openai.NewClient(
//		option.WithMiddleware(traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))),
//	)
package traceaiopenai
