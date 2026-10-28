package traceaieino

import (
	"context"
	"errors"
	"io"
	"sync"
	"sync/atomic"

	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/schema"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const (
	// Version is the version of this module.
	Version = "0.1.0"
	// ScopeName is the instrumentation scope of the spans this package starts.
	ScopeName = "github.com/future-agi/traceAI/go/eino"
)

type config struct {
	tp    trace.TracerProvider
	tc    TraceConfig
	tcSet bool
}

// Option configures [NewHandler].
type Option func(*config)

// WithTracerProvider sets the provider spans are started on. Without it the
// OpenTelemetry global provider (otel.GetTracerProvider) is used.
func WithTracerProvider(tp trace.TracerProvider) Option {
	return func(c *config) { c.tp = tp }
}

// WithTraceConfig sets the privacy switches. Without it they are read once
// from the FI_HIDE_* environment variables when [NewHandler] is called.
func WithTraceConfig(tc TraceConfig) Option {
	return func(c *config) { c.tc, c.tcSet = tc, true }
}

// Handler is an Eino callbacks.Handler that turns every component run Eino
// reports (graphs, chains, workflows, nodes, chat models, tools, retrievers,
// embedders, prompts, lambdas, ADK agents) into one OpenTelemetry span.
//
// Register it once, before any graph or agent runs:
//
//	callbacks.AppendGlobalHandlers(traceaieino.NewHandler(traceaieino.WithTracerProvider(tp)))
//
// or pass it to a single run with compose.WithCallbacks / adk.WithCallbacks.
// Registering the same handler twice gives two spans per component.
//
// A Handler is safe for concurrent use. Its callbacks never change a value
// Eino passes in, never return an error to Eino, and recover from their own
// panics.
type Handler struct {
	tp      trace.TracerProvider
	tracer  trace.Tracer
	tc      TraceConfig
	scrub   *scrubber
	closed  atomic.Bool
	pending sync.WaitGroup
}

var (
	_ callbacks.Handler = (*Handler)(nil)
)

// NewHandler returns a Handler. It does not register it: pass it to
// callbacks.AppendGlobalHandlers (once per process) or to a run's
// compose.WithCallbacks / adk.WithCallbacks option.
func NewHandler(opts ...Option) *Handler {
	c := &config{}
	for _, o := range opts {
		o(c)
	}
	if !c.tcSet {
		c.tc = TraceConfigFromEnv()
	}
	tp := c.tp
	if tp == nil {
		tp = otel.GetTracerProvider()
	}
	return &Handler{
		tp:     tp,
		tracer: tp.Tracer(ScopeName, trace.WithInstrumentationVersion(Version)),
		tc:     c.tc,
		scrub:  newScrubber(),
	}
}

// Shutdown stops the handler: callbacks that arrive afterwards start no span
// and return at once. It then waits, until ctx is done, for spans that are
// still being finished in the background (streamed outputs and agent event
// streams that are still being read), and asks the tracer provider to flush
// when it has a ForceFlush method (the SDK's *sdktrace.TracerProvider has
// one). It does not shut the provider down: call the provider's own Shutdown
// on exit so the last batch is exported. Shutdown is safe to call more than
// once.
func (h *Handler) Shutdown(ctx context.Context) error {
	h.closed.Store(true)
	done := make(chan struct{})
	go func() {
		h.pending.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	if f, ok := h.tp.(interface{ ForceFlush(context.Context) error }); ok {
		return f.ForceFlush(ctx)
	}
	return nil
}

// runState is what OnStart leaves in the context for the OnEnd / OnError /
// OnEndWithStreamOutput of the same run. Eino hands each timing the context
// the previous timing of the same handler returned.
type runState struct {
	span     trace.Span // nil when the run is not traced (handler shut down)
	info     *callbacks.RunInfo
	kind     string
	prev     *runState  // the state the context carried before this run
	prevSpan trace.Span // the OpenTelemetry span the context carried before
	ended    atomic.Bool
}

type stateKey struct{}

func stateFrom(ctx context.Context) *runState {
	st, _ := ctx.Value(stateKey{}).(*runState)
	return st
}

// running returns the state OnStart stored for this run, or nil. A state for
// another run (an enclosing graph, when Eino calls OnEnd or OnError without an
// OnStart for this run) is never returned: the RunInfo pointer is the same for
// every timing of one run.
func running(ctx context.Context, info *callbacks.RunInfo) *runState {
	st := stateFrom(ctx)
	if st == nil || st.info != info {
		return nil
	}
	return st
}

// restore returns ctx as it was before the run started, so that a second
// handler (or this one registered twice) sees its own state next.
func restore(ctx context.Context, st *runState) context.Context {
	ctx = context.WithValue(ctx, stateKey{}, st.prev)
	return trace.ContextWithSpan(ctx, st.prevSpan)
}

// parentContext picks the parent of a new span. A top-level run takes the
// caller's span, if any. A nested run takes the context's current span when
// it comes from the same tracer provider as the enclosing run's span (that is
// the enclosing span, or a span the caller started inside it), and otherwise
// the enclosing run's span, so a span from another callback handler on another
// provider (Langfuse, APMPlus, ...) is never made the parent.
func parentContext(ctx context.Context, prev *runState, ambient trace.Span) context.Context {
	if prev == nil || prev.span == nil {
		return ctx
	}
	if ambient.SpanContext().IsValid() && ambient.TracerProvider() == prev.span.TracerProvider() {
		return ctx
	}
	return trace.ContextWithSpan(ctx, prev.span)
}

// start opens the span for a run and returns the context to hand back to
// Eino: the caller's context with the new span as the current span (so spans
// started inside a tool or a lambda nest under it) and the run state.
func (h *Handler) start(ctx context.Context, info *callbacks.RunInfo, attrs []attribute.KeyValue) (context.Context, *runState) {
	prevSpan := trace.SpanFromContext(ctx)
	st := &runState{info: info, prev: stateFrom(ctx), prevSpan: prevSpan}
	if h.closed.Load() {
		st.ended.Store(true)
		return context.WithValue(ctx, stateKey{}, st), st
	}
	st.kind = spanKind(info)
	base := append(identityAttributes(info, st.kind), ContextAttributes(ctx)...)
	_, span := h.tracer.Start(parentContext(ctx, st.prev, prevSpan), spanName(info),
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(h.tc.Filter(append(base, attrs...))...),
	)
	st.span = span
	ctx = trace.ContextWithSpan(ctx, span)
	return context.WithValue(ctx, stateKey{}, st), st
}

// finish ends the span once. Later calls (OnEnd after OnError, a stream that
// ends after OnError) are no-ops.
func (h *Handler) finish(st *runState, attrs []attribute.KeyValue, err error) {
	if st == nil || st.span == nil || !st.ended.CompareAndSwap(false, true) {
		return
	}
	if len(attrs) > 0 {
		st.span.SetAttributes(h.tc.Filter(attrs)...)
	}
	if err != nil {
		h.setError(st.span, err)
	} else {
		st.span.SetStatus(codes.Ok, "")
	}
	st.span.End()
}

// setError marks the span ERROR with error.type, and adds a scrubbed one-line
// error.message unless inputs or outputs are hidden. The raw error text (which
// can carry a stack trace, a URL or a local path) is never recorded, and no
// exception event is added.
func (h *Handler) setError(span trace.Span, err error) {
	typ := errorType(err)
	span.SetAttributes(attribute.String(ErrorType, typ))
	if h.tc.errorMessageAllowed() {
		if msg := h.scrub.message(err); msg != "" {
			span.SetAttributes(attribute.String(ErrorMessage, msg))
		}
	}
	span.SetStatus(codes.Error, typ)
}

func (h *Handler) recoverPanic() {
	_ = recover() // a tracing bug must never take the user's graph down
}

// OnStart starts the span for a component run.
func (h *Handler) OnStart(ctx context.Context, info *callbacks.RunInfo, input callbacks.CallbackInput) (out context.Context) {
	out = ctx
	defer h.recoverPanic()
	if ctx == nil {
		return ctx
	}
	attrs := h.inputAttributes(ctx, info, input)
	out, _ = h.start(ctx, info, attrs)
	return out
}

// OnEnd ends the span of a run that returned a value. For an ADK agent, whose
// output is a stream of events, the span ends when that stream ends.
func (h *Handler) OnEnd(ctx context.Context, info *callbacks.RunInfo, output callbacks.CallbackOutput) (out context.Context) {
	out = ctx
	defer h.recoverPanic()
	st := running(ctx, info)
	if st != nil {
		out = restore(ctx, st)
	}
	// An agent's output is this handler's own copy of the event stream: it is
	// read to the end (and its message streams closed) even when the run is
	// not traced, or Eino cannot free it.
	if h.drainAgentAsync(st, output) {
		return out
	}
	if st == nil || st.span == nil || st.ended.Load() {
		return out
	}
	h.finish(st, h.outputAttributes(st, output), nil)
	return out
}

// OnError ends the span of a run with status ERROR. When Eino reports an error
// for a run whose OnStart this handler did not see (a component that failed
// while building its request), it records one ERROR span for it.
func (h *Handler) OnError(ctx context.Context, info *callbacks.RunInfo, err error) (out context.Context) {
	out = ctx
	defer h.recoverPanic()
	if err == nil {
		err = errors.New("unknown error")
	}
	st := running(ctx, info)
	if st == nil {
		if ctx == nil {
			return ctx
		}
		_, st = h.start(ctx, info, nil)
		h.finish(st, nil, err)
		return ctx
	}
	out = restore(ctx, st)
	h.finish(st, nil, err)
	return out
}

// OnStartWithStreamInput starts the span for a run that receives a stream
// (Collect / Transform). The span starts once; the chunks are read from the
// handler's own copy in the background and, when content capture is on,
// recorded as the input when the stream ends.
func (h *Handler) OnStartWithStreamInput(ctx context.Context, info *callbacks.RunInfo,
	input *schema.StreamReader[callbacks.CallbackInput]) (out context.Context) {
	out = ctx
	defer h.recoverPanic()
	if ctx == nil {
		if input != nil {
			input.Close()
		}
		return ctx
	}
	out, st := h.start(ctx, info, nil)
	if input == nil {
		return out
	}
	if st.span == nil || h.tc.HideInputs {
		input.Close()
		return out
	}
	h.pending.Add(1)
	go func() {
		defer h.pending.Done()
		defer h.recoverPanic()
		chunks := readStream(input, maxStreamChunks)
		if st.ended.Load() {
			return
		}
		if attrs := h.streamInputAttributes(st, chunks); len(attrs) > 0 {
			st.span.SetAttributes(h.tc.Filter(attrs)...)
		}
	}()
	return out
}

// OnEndWithStreamOutput ends the span of a run that returned a stream, once,
// after the handler's copy of the stream is read to the end. Chunks are not
// spans. Token usage comes from the chunk that reports it and is never summed.
func (h *Handler) OnEndWithStreamOutput(ctx context.Context, info *callbacks.RunInfo,
	output *schema.StreamReader[callbacks.CallbackOutput]) (out context.Context) {
	out = ctx
	defer h.recoverPanic()
	st := running(ctx, info)
	if st == nil {
		if output != nil {
			output.Close()
		}
		return ctx
	}
	out = restore(ctx, st)
	if output == nil {
		h.finish(st, nil, nil)
		return out
	}
	if st.span == nil || st.ended.Load() {
		output.Close()
		return out
	}
	h.pending.Add(1)
	go func() {
		defer h.pending.Done()
		defer h.recoverPanic()
		h.drainOutput(st, output)
	}()
	return out
}

// maxStreamChunks bounds how many chunks of a streamed input or of a
// non-model streamed output are kept for input.value / output.value. The
// stream is still read to the end.
const maxStreamChunks = 256

// readStream reads a stream copy to the end (or to its first error), keeps at
// most limit chunks and always closes it.
func readStream[T any](s *schema.StreamReader[T], limit int) []T {
	defer s.Close()
	var out []T
	for {
		v, err := s.Recv()
		if err != nil {
			return out
		}
		if len(out) < limit {
			out = append(out, v)
		}
	}
}

// drainOutput reads the handler's copy of a streamed output and ends the span.
func (h *Handler) drainOutput(st *runState, output *schema.StreamReader[callbacks.CallbackOutput]) {
	defer output.Close()
	acc := newStreamAccumulator(st.kind, h.tc)
	var streamErr error
	for {
		chunk, err := output.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			streamErr = err
			break
		}
		acc.add(chunk)
	}
	h.finish(st, acc.attributes(h), streamErr)
}

// drainAgentAsync ends an ADK agent span when the agent's event stream ends.
// It reports false when output is not an ADK agent output.
func (h *Handler) drainAgentAsync(st *runState, output callbacks.CallbackOutput) bool {
	if o := adk.ConvAgentCallbackOutput(output); o != nil && o.Events != nil {
		h.pending.Add(1)
		go func() {
			defer h.pending.Done()
			defer h.recoverPanic()
			drainAgentEvents(h, st, o.Events)
		}()
		return true
	}
	if o := adk.ConvTypedCallbackOutput[*schema.Message](output); o != nil && o.Events != nil {
		h.pending.Add(1)
		go func() {
			defer h.pending.Done()
			defer h.recoverPanic()
			drainAgentEvents(h, st, o.Events)
		}()
		return true
	}
	if o := adk.ConvTypedCallbackOutput[*schema.AgenticMessage](output); o != nil && o.Events != nil {
		h.pending.Add(1)
		go func() {
			defer h.pending.Done()
			defer h.recoverPanic()
			drainAgentEvents(h, st, o.Events)
		}()
		return true
	}
	return false
}
