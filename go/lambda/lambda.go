// Package traceailambda adds Future AGI lifecycle policies to the maintained
// OpenTelemetry Lambda instrumentor. It uses the caller's tracer provider and
// creates no additional invocation span or exporter.
package traceailambda

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync/atomic"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-lambda-go/lambda/handlertrace"
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
// result with lambda.Start or lambda.StartHandler. Do not also wrap it with
// otellambda.
//
// For handlers built with lambda.NewHandler, JSON results that do not implement
// io.Reader retain application/json. Reader results must be raw-only: their JSON
// encoding must fail or produce an empty object, as with strings.Reader and
// lambdaurl.Wrap. Readers that also encode to non-empty JSON are not supported;
// return JSON as a non-reader value instead. Reader ContentType is captured when
// the handler returns, before serialization, reading or closing, and must be
// valid then. Untyped readers use application/octet-stream. The result is read
// inside the invocation span, so a function URL response is delivered whole
// rather than incrementally. The event is decoded as JSON, so an empty payload fails even
// for a handler that takes no event. lambda.Start and lambda.StartWithOptions
// use such a wrapped handler as it is, so pass runtime options such as
// lambda.WithEnableSIGTERM, lambda.WithContextValue or
// lambdaurl.WithDetectContentType to WrapHandler instead. JSON options such as
// lambda.WithUseNumber stay on lambda.NewHandlerWithOptions for the handler you
// wrap.
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
// instrumentation; without options it returns the original handler.
func WrapHandler(handler lambda.Handler, provider Provider, options ...lambda.Option) lambda.Handler {
	if provider == nil && len(options) == 0 {
		return handler
	}
	invoke := handler
	if provider != nil {
		invoke = otellambda.WrapHandler(&metadataHandler{handler: handler},
			otellambda.WithTracerProvider(provider),
			otellambda.WithFlusher(boundedFlusher{provider: provider}),
			otellambda.WithPropagator(propagation.TraceContext{}),
			otellambda.WithEventToCarrier(eventCarrier),
		)
	}
	// aws-lambda-go starts its own handlers as they are and sends their typed
	// result; any other lambda.Handler result is sent as untyped bytes.
	if lambda.NewHandler(handler) != handler {
		if len(options) == 0 {
			return invoke
		}
		return lambda.NewHandlerWithOptions(invoke, options...)
	}
	return lambda.NewHandlerWithOptions(func(ctx context.Context, payload json.RawMessage) (io.Reader, error) {
		result := &resultCapture{}
		ctx = handlertrace.NewContext(ctx, handlertrace.HandlerTrace{ResponseEvent: result.record})
		body, err := invoke.Invoke(ctx, payload)
		if err != nil {
			return nil, err
		}
		return &typedResponse{Reader: bytes.NewReader(body), contentType: result.contentType(body)}, nil
	}, options...)
}

const (
	contentTypeJSON  = "application/json"
	contentTypeBytes = "application/octet-stream"
)

// resultCapture snapshots MIME metadata while the response is still live.
// Handler.Invoke consumes/closes readers and exposes only bytes, not the SDK's
// encoding decision. Re-encoding the value would call user serialization twice;
// sniffing the bytes would mistake raw JSON-shaped reader content for JSON.
// Consequently reader responses are supported only under the raw-only contract
// documented on WrapHandler.
type resultCapture struct {
	seen bool
	mime string
}

func (c *resultCapture) record(_ context.Context, value any) {
	c.seen, c.mime = true, contentTypeJSON
	if _, ok := value.(io.Reader); !ok {
		return
	}
	c.mime = contentTypeBytes
	if typed, ok := value.(interface{ ContentType() string }); ok {
		c.mime = typed.ContentType()
	}
}

func (c *resultCapture) contentType(body []byte) string {
	if !c.seen {
		// Handlers without a result value send JSON null. A handler built from a
		// custom lambda.Handler reports no result and sends untyped bytes.
		if bytes.Equal(bytes.TrimSpace(body), []byte("null")) {
			return contentTypeJSON
		}
		return contentTypeBytes
	}
	return c.mime
}

var errRawResponse = errors.New("traceai lambda: raw response")

// typedResponse is the handler result bytes with the runtime Content-Type.
type typedResponse struct {
	*bytes.Reader
	contentType string
}

func (r *typedResponse) ContentType() string { return r.contentType }

// MarshalJSON fails so aws-lambda-go sends the reader instead of encoding it.
func (r *typedResponse) MarshalJSON() ([]byte, error) { return nil, errRawResponse }

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
