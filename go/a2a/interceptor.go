package traceaia2a

import (
	"context"
	"strings"
	"sync"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// ScopeName is the instrumentation scope of every span this package starts.
const ScopeName = "github.com/future-agi/traceAI/go/a2a"

// Version is this module's version, reported as the instrumentation scope
// version.
const Version = "0.1.0"

// Option configures [NewInterceptor].
type Option func(*settings)

type settings struct {
	tp     trace.TracerProvider
	tc     *TraceConfig
	prop   propagation.TextMapPropagator
	noProp bool
}

// WithTracerProvider sets the provider the interceptor starts spans with.
// Without it the OpenTelemetry global provider (otel.SetTracerProvider) is
// used.
func WithTracerProvider(tp trace.TracerProvider) Option {
	return func(s *settings) { s.tp = tp }
}

// WithTraceConfig sets the content switches in code. Without it they are read
// once, from the FI_HIDE_* environment variables, when the interceptor is
// built.
func WithTraceConfig(c TraceConfig) Option {
	return func(s *settings) { s.tc = &c }
}

// WithPropagator sets how trace context is written onto the outbound A2A
// request. The default is W3C Trace Context only (traceparent and
// tracestate), so no baggage leaves the process unless you ask for it.
// Passing nil turns propagation off.
func WithPropagator(p propagation.TextMapPropagator) Option {
	return func(s *settings) {
		s.prop = p
		s.noProp = p == nil
	}
}

// Interceptor is an a2aclient.CallInterceptor that traces A2A client calls.
// Attach it with a2aclient.WithCallInterceptors when you create the client:
//
//	client, err := a2aclient.NewFromCard(ctx, card,
//		a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp))))
//
// SendMessage, SendStreamingMessage, GetTask, CancelTask and SubscribeToTask
// each become one span; other methods pass through untouched. The interceptor
// never changes a request payload, a response or an error. It adds only the
// traceparent (and tracestate) service parameters, which the JSON-RPC and REST
// transports send as HTTP headers.
type Interceptor struct {
	tracer trace.Tracer
	tc     TraceConfig
	prop   propagation.TextMapPropagator
}

var _ a2aclient.CallInterceptor = (*Interceptor)(nil)

// NewInterceptor returns an interceptor that starts spans with the configured
// (or global) tracer provider.
func NewInterceptor(opts ...Option) *Interceptor {
	var s settings
	for _, o := range opts {
		if o != nil {
			o(&s)
		}
	}
	tp := s.tp
	if tp == nil {
		tp = otel.GetTracerProvider()
	}
	tc := TraceConfigFromEnv()
	if s.tc != nil {
		tc = *s.tc
	}
	prop := s.prop
	if prop == nil && !s.noProp {
		prop = propagation.TraceContext{}
	}
	return &Interceptor{
		tracer: tp.Tracer(ScopeName, trace.WithInstrumentationVersion(Version)),
		tc:     tc,
		prop:   prop,
	}
}

type methodInfo struct {
	op        string
	streaming bool
}

// methods lists the a2aclient methods that get a span, by Request.Method.
var methods = map[string]methodInfo{
	"SendMessage":          {op: OperationSend},
	"SendStreamingMessage": {op: OperationSend, streaming: true},
	"GetTask":              {op: OperationGetTask},
	"CancelTask":           {op: OperationCancel},
	"SubscribeToTask":      {op: OperationSubscribe, streaming: true},
}

type stateKey struct{}

// Before starts the call span, records what the request says, and writes the
// trace context into the request's service parameters.
func (i *Interceptor) Before(ctx context.Context, req *a2aclient.Request) (context.Context, any, error) {
	if req == nil {
		return ctx, nil, nil
	}
	info, ok := methods[req.Method]
	if !ok {
		return ctx, nil, nil
	}
	if ctx == nil {
		ctx = context.Background()
	}
	st := &callState{method: req.Method, tc: i.tc, streaming: info.streaming}
	// a2aclient answers SendStreamingMessage with one SendMessage call when the
	// card says the agent cannot stream; After then sees a single result.
	if req.Method == "SendStreamingMessage" && req.Card != nil && !req.Card.Capabilities.Streaming {
		st.streaming = false
	}

	attrs := []attribute.KeyValue{
		attribute.String(GenAISpanKind, SpanKindAgent),
		attribute.String(GenAIOperationName, info.op),
		attribute.Bool(A2AStreaming, info.streaming),
	}
	if u := sanitizeURL(req.BaseURL); u != "" {
		attrs = append(attrs, attribute.String(A2AAgentURL, u))
	}
	if c := req.Card; c != nil {
		if c.Name != "" {
			attrs = append(attrs, attribute.String(A2AAgentCardName, c.Name))
		}
		if c.Version != "" {
			attrs = append(attrs, attribute.String(A2AAgentCardVer, c.Version))
		}
	}
	if id, ok := SessionIDFromContext(ctx); ok {
		attrs = append(attrs, attribute.String(SessionID, id))
		st.sessionSet = true
	}
	if id, ok := UserIDFromContext(ctx); ok {
		attrs = append(attrs, attribute.String(UserID, id))
	}

	var content []attribute.KeyValue
	switch p := req.Payload.(type) {
	case *a2a.SendMessageRequest:
		if p != nil && p.Message != nil {
			m := p.Message
			if m.Role != a2a.MessageRoleUnspecified {
				attrs = append(attrs, attribute.String(A2AMessageRole, string(m.Role)))
			}
			attrs = append(attrs, attribute.Int(A2AMessagePartsCnt, len(m.Parts)))
			attrs = append(attrs, st.taskInfo(string(m.TaskID), m.ContextID)...)
			if text := partsText(m.Parts); text != "" {
				content = append(content,
					attribute.String(InputValue, truncate(text)),
					attribute.String(InputMimeType, MimeTypeText))
			}
		}
	case *a2a.GetTaskRequest:
		if p != nil {
			attrs = append(attrs, st.taskInfo(string(p.ID), "")...)
		}
	case *a2a.CancelTaskRequest:
		if p != nil {
			attrs = append(attrs, st.taskInfo(string(p.ID), "")...)
		}
	case *a2a.SubscribeToTaskRequest:
		if p != nil {
			attrs = append(attrs, st.taskInfo(string(p.ID), "")...)
		}
	}

	ctx, span := i.tracer.Start(ctx, "a2a."+req.Method,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attrs...))
	st.span = span
	i.tc.SetAttributes(span, content...)

	if i.prop != nil {
		if req.ServiceParams == nil {
			req.ServiceParams = a2aclient.ServiceParams{}
		}
		carrier := propagation.MapCarrier{}
		i.prop.Inject(ctx, carrier)
		for k, v := range carrier {
			// Replace, not append: one traceparent per request.
			req.ServiceParams[strings.ToLower(k)] = []string{v}
		}
	}

	if st.streaming {
		// A stream ends without a callback when the caller stops reading
		// early; the span then ends when the caller's context is done.
		// The lock keeps a watcher that fires at once (an already-cancelled
		// context) from reading stopWatch before it is set.
		spanCtx := ctx
		st.mu.Lock()
		st.stopWatch = context.AfterFunc(ctx, func() { st.endWithContextError(spanCtx) })
		st.mu.Unlock()
	}
	return context.WithValue(ctx, stateKey{}, st), nil, nil
}

// After records the result, a streamed event or the error, and ends the span
// when the call is over. It never changes the response and never fails it.
func (i *Interceptor) After(ctx context.Context, resp *a2aclient.Response) error {
	if ctx == nil || resp == nil {
		return nil
	}
	st, _ := ctx.Value(stateKey{}).(*callState)
	if st == nil || resp.Method != st.method {
		return nil
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	if st.ended {
		return nil
	}
	if resp.Err != nil {
		st.fail(resp.Err)
		return nil
	}
	if !st.streaming {
		st.applyResult(resp.Payload)
		st.finish()
		return nil
	}
	if st.applyEvent(resp.Payload) {
		st.finish()
	}
	return nil
}

// callState is one traced call, shared by Before, every After and the
// context watcher of a stream.
type callState struct {
	mu        sync.Mutex
	span      trace.Span
	method    string
	tc        TraceConfig
	streaming bool
	ended     bool
	stopWatch func() bool

	sessionSet bool
	taskID     string
	contextID  string
	state      a2a.TaskState
	events     int

	artifacts strings.Builder // artifact text, in order
	final     string          // reply message or final status message text
}

// taskInfo returns the task and context attributes not yet recorded. The A2A
// context id becomes session.id unless the caller set a session id.
func (s *callState) taskInfo(taskID, contextID string) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if taskID != "" && taskID != s.taskID {
		s.taskID = taskID
		kvs = append(kvs, attribute.String(A2ATaskID, taskID))
	}
	if contextID != "" && contextID != s.contextID {
		s.contextID = contextID
		kvs = append(kvs, attribute.String(A2AContextID, contextID))
		if !s.sessionSet {
			s.sessionSet = true
			kvs = append(kvs, attribute.String(SessionID, contextID))
		}
	}
	return kvs
}

func (s *callState) setTaskInfo(taskID, contextID string) {
	if kvs := s.taskInfo(taskID, contextID); len(kvs) > 0 {
		s.span.SetAttributes(kvs...)
	}
}

// setState records the state a2a-go reported, as a2a-go spells it.
func (s *callState) setState(state a2a.TaskState) {
	if state == a2a.TaskStateUnspecified {
		return
	}
	s.state = state
	s.span.SetAttributes(attribute.String(A2ATaskState, string(state)))
}

// applyResult handles the single result of a non-streaming call.
func (s *callState) applyResult(payload any) {
	switch v := payload.(type) {
	case *a2a.Task:
		s.applyTask(v)
	case *a2a.Message:
		s.applyMessage(v)
	}
}

func (s *callState) applyTask(t *a2a.Task) {
	if t == nil {
		return
	}
	s.setTaskInfo(string(t.ID), t.ContextID)
	s.setState(t.Status.State)
	// A task snapshot carries every artifact so far, so it replaces what
	// earlier artifact events added.
	s.artifacts.Reset()
	for _, a := range t.Artifacts {
		s.addArtifact(a)
	}
	if endsStream(t.Status.State) && t.Status.Message != nil {
		s.final = partsText(t.Status.Message.Parts)
	}
}

func (s *callState) applyMessage(m *a2a.Message) {
	if m == nil {
		return
	}
	s.setTaskInfo(string(m.TaskID), m.ContextID)
	s.final = partsText(m.Parts)
}

func (s *callState) addArtifact(a *a2a.Artifact) {
	if a == nil {
		return
	}
	if t := partsKind(a.Parts); t != "" {
		s.span.SetAttributes(attribute.String(A2AArtifactType, t))
	}
	if text := partsText(a.Parts); text != "" && s.artifacts.Len() < maxContentLen {
		if s.artifacts.Len() > 0 {
			s.artifacts.WriteByte('\n')
		}
		s.artifacts.WriteString(text)
	}
}

// applyEvent handles one streamed event and reports whether the stream is
// over: a reply message, or a task in a terminal or interrupted state.
func (s *callState) applyEvent(payload any) bool {
	s.events++
	ev := []attribute.KeyValue{}
	done := false
	switch v := payload.(type) {
	case *a2a.Task:
		ev = append(ev, attribute.String(A2AEventType, "task"))
		if v != nil {
			s.applyTask(v)
			if v.Status.State != a2a.TaskStateUnspecified {
				ev = append(ev, attribute.String(A2ATaskState, string(v.Status.State)))
			}
			done = endsStream(v.Status.State)
		}
	case *a2a.Message:
		ev = append(ev, attribute.String(A2AEventType, "message"))
		s.applyMessage(v)
		done = true
	case *a2a.TaskStatusUpdateEvent:
		ev = append(ev, attribute.String(A2AEventType, "status-update"))
		if v != nil {
			s.setTaskInfo(string(v.TaskID), v.ContextID)
			s.setState(v.Status.State)
			if v.Status.State != a2a.TaskStateUnspecified {
				ev = append(ev, attribute.String(A2ATaskState, string(v.Status.State)))
			}
			if endsStream(v.Status.State) {
				done = true
				if v.Status.Message != nil {
					s.final = partsText(v.Status.Message.Parts)
				}
			}
		}
	case *a2a.TaskArtifactUpdateEvent:
		ev = append(ev, attribute.String(A2AEventType, "artifact-update"))
		if v != nil {
			s.setTaskInfo(string(v.TaskID), v.ContextID)
			s.addArtifact(v.Artifact)
			if v.Artifact != nil {
				if t := partsKind(v.Artifact.Parts); t != "" {
					ev = append(ev, attribute.String(A2AArtifactType, t))
				}
			}
		}
	}
	s.span.AddEvent(EventStreamEvent, trace.WithAttributes(ev...))
	return done
}

// endsStream reports whether a2a-go closes a stream at this state: the
// terminal states and the two interrupted ones.
func endsStream(state a2a.TaskState) bool {
	return state.Terminal() || state == a2a.TaskStateInputRequired || state == a2a.TaskStateAuthRequired
}

func (s *callState) output() {
	out := s.artifacts.String()
	if s.final != "" {
		if out != "" {
			out += "\n"
		}
		out += s.final
	}
	if out != "" {
		s.tc.SetAttributes(s.span,
			attribute.String(OutputValue, truncate(out)),
			attribute.String(OutputMimeType, MimeTypeText))
	}
	if s.streaming {
		s.span.SetAttributes(attribute.Int(A2AStreamEventCount, s.events))
	}
}

// finish ends a call that returned without an error. A task the agent
// reports as failed or rejected is an ERROR; every other state, canceled
// included, is not.
func (s *callState) finish() {
	s.ended = true
	if s.stopWatch != nil {
		s.stopWatch()
	}
	s.output()
	if s.state == a2a.TaskStateFailed || s.state == a2a.TaskStateRejected {
		s.span.SetAttributes(attribute.String(ErrorType, string(s.state)))
		s.span.SetStatus(codes.Error, string(s.state))
	} else {
		s.span.SetStatus(codes.Ok, "")
	}
	s.span.End()
}

// fail ends the call with status ERROR. Only a class of the error is
// recorded; the remote agent's own error message is added when content is
// not hidden. Transport error text (URLs, host names) is never recorded.
func (s *callState) fail(err error) {
	s.ended = true
	if s.stopWatch != nil {
		s.stopWatch()
	}
	s.output()
	errType, agentMsg := classifyError(err)
	s.span.SetAttributes(attribute.String(ErrorType, errType))
	if agentMsg != "" && s.tc.errorMessageAllowed() {
		s.tc.SetAttributes(s.span, attribute.String(ErrorMessage, truncate(agentMsg)))
	}
	s.span.SetStatus(codes.Error, errType)
	s.span.End()
}

// endWithContextError ends a stream whose context was cancelled or timed out
// before a final event arrived.
func (s *callState) endWithContextError(ctx context.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ended {
		return
	}
	err := ctx.Err()
	if err == nil {
		err = context.Canceled
	}
	s.fail(err)
}
