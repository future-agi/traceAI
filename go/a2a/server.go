package traceaia2a

import (
	"context"
	"strings"

	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// ServerInterceptor traces incoming A2A calls through the official server hook.
// Attach it once, before other interceptors, with a2asrv.WithCallInterceptors.
// It covers the same five methods as [Interceptor], with one SERVER span per
// call or stream, and never modifies a request, response or error.
//
// The default propagator extracts W3C trace context from HTTP headers or gRPC
// metadata. If transport middleware has already started a local span, that span
// remains the parent so the transport and protocol spans form a single tree.
// Headers, credentials, baggage and authenticated user details are not recorded.
// Use [WithTraceConfig] or FI_HIDE_INPUTS / FI_HIDE_OUTPUTS to hide message content.
//
// a2asrv calls After for each streamed event, not on iterator close. Streams
// therefore end at a terminal/interrupted event or when the request context is
// canceled. Direct in-process callers must cancel their context if they stop
// reading early; HTTP and gRPC transports cancel it when the request ends.
// Detached background task execution can outlive the request span.
// This interceptor does not wrap transport handlers or create executor spans.
//
//	handler := a2asrv.NewHandler(executor,
//		a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(
//			traceaia2a.WithTracerProvider(tp))))
//
// Use that handler with a2asrv.NewJSONRPCHandler, a2asrv.NewRESTHandler or
// a2agrpc/v1.NewHandler.
type ServerInterceptor struct {
	config *Interceptor
}

var _ a2asrv.CallInterceptor = (*ServerInterceptor)(nil)

// NewServerInterceptor returns a server interceptor with the same options and
// privacy defaults as [NewInterceptor]. It does not register global telemetry.
func NewServerInterceptor(opts ...Option) *ServerInterceptor {
	return &ServerInterceptor{config: NewInterceptor(opts...)}
}

// Separate state keys keep outbound calls from an executor from overwriting
// the inbound call's lifecycle state.
type serverStateKey struct{}

// Before extracts incoming context and starts a protocol SERVER span.
func (i *ServerInterceptor) Before(ctx context.Context, callCtx *a2asrv.CallContext, req *a2asrv.Request) (context.Context, any, error) {
	if callCtx == nil || req == nil {
		return ctx, nil, nil
	}
	info, ok := methods[callCtx.Method()]
	if !ok {
		return ctx, nil, nil
	}
	if ctx == nil {
		ctx = context.Background()
	}
	config := i.config
	parent := trace.SpanContextFromContext(ctx)
	if config.prop != nil && (!parent.IsValid() || parent.IsRemote()) {
		ctx = config.prop.Extract(ctx, serviceParamsCarrier{callCtx.ServiceParams()})
	}
	st := &callState{method: callCtx.Method(), tc: config.tc, streaming: info.streaming}
	attrs, content := st.requestAttributes(ctx, req.Payload)
	attrs = append(attrs,
		attribute.String(GenAISpanKind, SpanKindAgent),
		attribute.String(GenAIOperationName, info.op),
		attribute.Bool(A2AStreaming, info.streaming))
	ctx, st.span = config.tracer.Start(ctx, "a2a."+st.method,
		trace.WithSpanKind(trace.SpanKindServer), trace.WithAttributes(attrs...))
	config.tc.SetAttributes(st.span, content...)

	// Watch every inbound call: the SDK may reject a request after Before
	// without running After (e.g. a required extension was not requested).
	// Synchronize with an already-canceled request before publishing the stop fn.
	spanCtx := ctx
	st.mu.Lock()
	st.stopWatch = context.AfterFunc(ctx, func() { st.endWithContextError(spanCtx) })
	st.mu.Unlock()
	return context.WithValue(ctx, serverStateKey{}, st), nil, nil
}

// After observes a response without changing protocol behavior.
func (i *ServerInterceptor) After(ctx context.Context, callCtx *a2asrv.CallContext, resp *a2asrv.Response) error {
	if ctx == nil || callCtx == nil || resp == nil {
		return nil
	}
	st, _ := ctx.Value(serverStateKey{}).(*callState)
	if st != nil && st.method == callCtx.Method() {
		st.recordResponse(resp.Payload, resp.Err)
	}
	return nil
}

// This read-only carrier uses the SDK's case-insensitive lookup and never
// copies the full header map into span attributes. Joining repeated values
// makes duplicate traceparent headers invalid instead of trusting one of them.
type serviceParamsCarrier struct{ params *a2asrv.ServiceParams }

func (c serviceParamsCarrier) Get(key string) string {
	values, _ := c.params.Get(key)
	return strings.Join(values, ",")
}

func (serviceParamsCarrier) Set(string, string) {}

func (c serviceParamsCarrier) Keys() []string {
	var keys []string
	for key := range c.params.List() {
		keys = append(keys, key)
	}
	return keys
}
