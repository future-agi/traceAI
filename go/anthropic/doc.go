// Package traceaianthropic traces the Messages API calls of
// github.com/anthropics/anthropic-sdk-go for Future AGI.
//
// Install it as a client middleware:
//
//	client := anthropic.NewClient(option.WithMiddleware(traceaianthropic.Middleware()))
//
// Each Messages call, streaming or not, becomes one LLM span with the request
// and response (input.value, output.value, gen_ai.input.messages,
// gen_ai.output.messages), model, token counts, finish reason, and the
// session.id and user.id that [ContextWithSessionID] and [ContextWithUserID]
// put in the call's context. From anthropic-sdk-go v1.81.0 the SDK opens that
// span itself and the middleware adds these attributes to it; with the SDK's
// spans turned off, the middleware opens the span.
//
// Content is captured by default, as in every traceAI instrumentor. The
// FI_HIDE_INPUTS, FI_HIDE_OUTPUTS, FI_HIDE_INPUT_MESSAGES,
// FI_HIDE_OUTPUT_MESSAGES, FI_HIDE_INPUT_TEXT, FI_HIDE_OUTPUT_TEXT,
// FI_HIDE_INPUT_IMAGES and FI_HIDE_LLM_INVOCATION_PARAMETERS environment
// variables, or a [TraceConfig], turn it off.
//
// The package depends on the OpenTelemetry API only. The application sets up
// the tracer provider and its exporter; the example in the example directory
// sends spans to Future AGI over OTLP/HTTP.
package traceaianthropic

// Version is the version of this package.
const Version = "0.1.0"
