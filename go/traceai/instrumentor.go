package traceai

import "go.opentelemetry.io/otel/trace"

// Instrumentor wraps an AI framework client with OTel tracing.
type Instrumentor interface {
	Instrument(tp trace.TracerProvider) error
	Uninstrument() error
}

func Tracer(tp trace.TracerProvider, name string, opts ...trace.TracerOption) trace.Tracer {
	if tp == nil {
		// fall back to the global provider set by Register()
		return trace.NewNoopTracerProvider().Tracer(name)
	}
	return tp.Tracer(name, opts...)
}
