// Package traceaigenai traces the Google Gen AI Go SDK
// (google.golang.org/genai) for Future AGI.
//
// The SDK has no middleware option; its hook is ClientConfig.HTTPClient. Pass
// the client from [NewHTTPClient] (or wrap your own with [WrapClient]) and
// every Models.GenerateContent and Models.GenerateContentStream call, and the
// chat helpers built on them, becomes one OpenTelemetry client span named
// "generate_content <model>" with gen_ai.span.kind=LLM, the GenAI
// semantic-convention attributes, token usage when the API returns it, and the
// messages, the same attribute names traceai-google-genai writes in Python.
// Other SDK requests pass through without a span.
//
// The tested backend is the Gemini Developer API (genai.BackendGeminiAPI).
// The retired github.com/google/generative-ai-go module is not supported.
//
// Content is captured by default, as in every traceAI instrumentor. The
// traceAI switches turn it off: FI_HIDE_INPUTS, FI_HIDE_OUTPUTS,
// FI_HIDE_INPUT_MESSAGES, FI_HIDE_OUTPUT_MESSAGES, FI_HIDE_INPUT_TEXT,
// FI_HIDE_OUTPUT_TEXT, FI_HIDE_INPUT_IMAGES, FI_HIDE_LLM_INVOCATION_PARAMETERS
// and FI_BASE64_IMAGE_MAX_LENGTH, or a [TraceConfig] in code. The Gemini API
// key, request headers and the request URL are never exported.
//
// The package depends only on the OpenTelemetry API and the standard library.
// It does not create an exporter: build a TracerProvider with the stock
// OTLP/HTTP exporter pointed at Future AGI (see the example directory) and
// pass it with [WithTracerProvider], or install it as the global provider.
//
//	client, err := genai.NewClient(ctx, &genai.ClientConfig{
//		Backend:    genai.BackendGeminiAPI,
//		HTTPClient: traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
//	})
package traceaigenai
