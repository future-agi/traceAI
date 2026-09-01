package traceai_openai

import "go.opentelemetry.io/otel/trace"

type options struct {
	tracerProvider trace.TracerProvider
	captureContent bool
}

func defaultOptions() options {
	return options{
		captureContent: true,
	}
}

type Option func(*options)

// WithTracerProvider sets a specific TracerProvider instead of the global one.
func WithTracerProvider(tp trace.TracerProvider) Option {
	return func(o *options) { o.tracerProvider = tp }
}

// WithContentCapture toggles recording prompt/completion content. Default true.
func WithContentCapture(capture bool) Option {
	return func(o *options) { o.captureContent = capture }
}
