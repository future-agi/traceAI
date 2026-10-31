// Package traceailambda adds Future AGI lifecycle policies to the maintained
// OpenTelemetry Lambda instrumentor. It uses the caller's tracer provider and
// creates no additional invocation span or exporter.
package traceailambda

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"go.opentelemetry.io/contrib/instrumentation/github.com/aws/aws-lambda-go/otellambda"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

const (
	// FlushTimeout bounds the per-invocation flush attempt with an OTel SDK provider.
	FlushTimeout = 2 * time.Second
	// FlushReserve leaves time for the runtime to return the application result.
	FlushReserve = 250 * time.Millisecond
)

// Provider is implemented by the OpenTelemetry SDK's TracerProvider. Custom
// implementations must honor the context deadline passed to ForceFlush.
// The provider is owned by the application; the wrapper never shuts it down.
type Provider interface {
	trace.TracerProvider
	ForceFlush(context.Context) error
}

// WrapHandler wraps an AWS Lambda handler with otellambda's single SERVER span.
// Build the provider and wrapper once, outside the handler, and register the
// result with lambda.StartHandler. Do not also wrap it with otellambda.
//
// HTTP API v2 / function URL events can supply a W3C traceparent. Other headers,
// baggage, event and result content, error messages and stack traces are never
// captured by this adapter. Errors and panics are marked with their Go type only;
// the original error or panic is preserved for the Lambda runtime.
//
// After the span ends, flushing uses a cancellation-independent context with a
// two-second maximum and a 250 ms invocation-deadline reserve. A deadline with
// no reserve left skips flushing. Export failures do not change the result.
// A hard platform timeout cannot guarantee delivery. A nil provider disables
// instrumentation and returns the original handler.
func WrapHandler(handler lambda.Handler, provider Provider) lambda.Handler {
	if provider == nil {
		return handler
	}
	return otellambda.WrapHandler(&metadataHandler{handler: handler},
		otellambda.WithTracerProvider(provider),
		otellambda.WithFlusher(boundedFlusher{provider: provider}),
		otellambda.WithPropagator(propagation.TraceContext{}),
		otellambda.WithEventToCarrier(eventCarrier),
	)
}

type metadataHandler struct {
	handler lambda.Handler
	warm    atomic.Bool
}

func (h *metadataHandler) Invoke(ctx context.Context, payload []byte) ([]byte, error) {
	span := trace.SpanFromContext(ctx)
	// Preserve the name used by the former docs recipe. otellambda owns this span.
	span.SetName("lambda.handler")
	span.SetAttributes(
		attribute.String("fi.span.kind", "CHAIN"),
		attribute.Bool("faas.coldstart", !h.warm.Swap(true)),
		attribute.String("faas.trigger", eventTrigger(payload)),
	)
	defer func() {
		if value := recover(); value != nil {
			markFailure(span, value)
			panic(value)
		}
	}()
	response, err := h.handler.Invoke(ctx, payload)
	if err != nil {
		markFailure(span, err)
	}
	return response, err
}

func markFailure(span trace.Span, value any) {
	// Do not call Error or String: their text may contain customer content.
	typ := fmt.Sprintf("%T", value)
	span.SetAttributes(attribute.String("error.type", typ))
	span.SetStatus(codes.Error, typ)
}

type httpEvent struct {
	Version        string            `json:"version"`
	Headers        map[string]string `json:"headers"`
	RequestContext json.RawMessage   `json:"requestContext"`
}

func eventCarrier(payload []byte) propagation.TextMapCarrier {
	carrier := propagation.MapCarrier{}
	var event httpEvent
	if json.Unmarshal(payload, &event) != nil || event.Version != "2.0" {
		return carrier
	}
	// Explicit allowlist: authentication headers, baggage and tracestate never
	// enter span attributes or downstream propagation through this adapter.
	for name, value := range event.Headers {
		if strings.EqualFold(name, "traceparent") {
			carrier.Set("traceparent", value)
			break
		}
	}
	return carrier
}

func eventTrigger(payload []byte) string {
	var event httpEvent
	if json.Unmarshal(payload, &event) == nil && event.Version == "2.0" && len(event.RequestContext) > 0 && string(event.RequestContext) != "null" {
		return "http"
	}
	return "other"
}

type boundedFlusher struct {
	provider interface{ ForceFlush(context.Context) error }
}

func (f boundedFlusher) ForceFlush(ctx context.Context) error {
	deadline := time.Now().Add(FlushTimeout)
	if invocationDeadline, ok := ctx.Deadline(); ok && invocationDeadline.Add(-FlushReserve).Before(deadline) {
		deadline = invocationDeadline.Add(-FlushReserve)
	}
	if time.Until(deadline) <= 0 {
		otel.Handle(errors.New("traceai lambda: flush skipped; invocation deadline reserve exhausted"))
		return nil
	}
	flushCtx, cancel := context.WithDeadline(context.WithoutCancel(ctx), deadline)
	defer cancel()
	if err := f.provider.ForceFlush(flushCtx); err != nil {
		// otellambda logs returned errors verbatim. Keep exporter URLs, response
		// bodies and credentials out of both its logger and the OTel error handler.
		otel.Handle(errors.New("traceai lambda: flush did not complete; spans may be lost"))
	}
	return nil
}
