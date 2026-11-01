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
// Use [ServerInterceptor.Wrap] for other interceptors that can return an After
// error: the SDK otherwise skips the tracing After, leaving only cancellation
// to finish the span without the rejection error.
//
// Requires a2a-go v2.7.0 or later (the module minimum): older SDKs skip After
// when rejecting unary calls for missing required extensions.
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

// Wrap observes errors returned by another interceptor's After, which otherwise
// stop the SDK from calling the tracing After. Register i first, then i.Wrap(policy)
// in the same a2asrv.WithCallInterceptors list. Wrap each interceptor that can
// reject a response this way. The wrapped interceptor's Before, response changes,
// returned error and SDK callback ordering are preserved; no extra span is created.
func (i *ServerInterceptor) Wrap(interceptor a2asrv.CallInterceptor) a2asrv.CallInterceptor {
	return &serverResponseObserver{CallInterceptor: interceptor, tracing: i}
}

type serverResponseObserver struct {
	a2asrv.CallInterceptor
	tracing *ServerInterceptor
}

func (o *serverResponseObserver) After(ctx context.Context, callCtx *a2asrv.CallContext, resp *a2asrv.Response) error {
	err := o.CallInterceptor.After(ctx, callCtx, resp)
	if err != nil {
		// The SDK discards the payload on rejection. Observe that outcome without
		// changing the response or allowing earlier policy callbacks to run.
		_ = o.tracing.After(ctx, callCtx, &a2asrv.Response{Err: err})
	}
	return err
}

// Separate state keys keep outbound calls from an executor from overwriting
// the inbound call's lifecycle state.
type serverStateKey struct{}

// Before extracts incoming context and starts a protocol SERVER span.
func (i *ServerInterceptor) Before(ctx context.Context, callCtx *a2asrv.CallContext, req *a2asrv.Request) (context.Context, any, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	// Even untraced nested calls need a state boundary: their After must not
	// finalize the outer call's span inherited through the executor context.
	ctx = context.WithValue(ctx, serverStateKey{}, (*callState)(nil))
	if callCtx == nil || req == nil {
		return ctx, nil, nil
	}
	info, ok := methods[callCtx.Method()]
	if !ok {
		return ctx, nil, nil
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

	// Watch for cancellation even if later middleware short-circuits After.
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
	// The SDK reuses CallContext for nested in-process calls, mutating Method.
	// Only the context returned by Before identifies this call's state.
	st, _ := ctx.Value(serverStateKey{}).(*callState)
	if st != nil {
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
