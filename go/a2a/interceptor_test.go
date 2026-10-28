package traceaia2a_test

import (
	"context"
	"errors"
	"iter"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	traceaia2a "github.com/future-agi/traceAI/go/a2a"
)

const marker = "MARKER-7f3a9c"

// agent is a real a2a-go server (a2asrv) whose executor plays a script.
type agent struct {
	execute func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool)
	cancel  func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool)
}

func (a *agent) Execute(ctx context.Context, ec *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) { a.execute(ctx, ec, yield) }
}

func (a *agent) Cancel(ctx context.Context, ec *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		if a.cancel != nil {
			a.cancel(ctx, ec, yield)
			return
		}
		yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCanceled, nil), nil)
	}
}

func text(ec *a2asrv.ExecutorContext) string {
	if ec.Message == nil || len(ec.Message.Parts) == 0 {
		return ""
	}
	return ec.Message.Parts[0].Text()
}

// completes: submitted, working, one artifact, completed with a status message.
func completes() *agent {
	return &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
		_ = yield(a2a.NewSubmittedTask(ec, ec.Message), nil) &&
			yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateWorking, nil), nil) &&
			yield(a2a.NewArtifactEvent(ec, a2a.NewTextPart("answer to "+text(ec))), nil) &&
			yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted,
				a2a.NewMessageForTask(a2a.MessageRoleAgent, ec, a2a.NewTextPart("done "+text(ec)))), nil)
	}}
}

func endsIn(state a2a.TaskState, msg string) *agent {
	return &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
		var m *a2a.Message
		if msg != "" {
			m = a2a.NewMessageForTask(a2a.MessageRoleAgent, ec, a2a.NewTextPart(msg))
		}
		_ = yield(a2a.NewSubmittedTask(ec, ec.Message), nil) &&
			yield(a2a.NewStatusUpdateEvent(ec, state, m), nil)
	}}
}

func repliesWithMessage() *agent {
	return &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
		yield(a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("hello "+text(ec))), nil)
	}}
}

// works until its context ends.
func blocks() *agent {
	return &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
		if !(yield(a2a.NewSubmittedTask(ec, ec.Message), nil) &&
			yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateWorking, nil), nil)) {
			return
		}
		<-ctx.Done()
	}}
}

type server struct {
	*httptest.Server
	mu      sync.Mutex
	headers []http.Header
}

func (s *server) seen() []http.Header {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]http.Header(nil), s.headers...)
}

func startAgent(t *testing.T, a *agent, rest bool) *server {
	t.Helper()
	h := a2asrv.NewHandler(a)
	var inner http.Handler
	if rest {
		inner = a2asrv.NewRESTHandler(h)
	} else {
		inner = a2asrv.NewJSONRPCHandler(h)
	}
	s := &server{}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		s.headers = append(s.headers, r.Header.Clone())
		s.mu.Unlock()
		inner.ServeHTTP(w, r)
	}))
	t.Cleanup(s.Close)
	return s
}

func card(url string, rest, streaming bool) *a2a.AgentCard {
	proto := a2a.TransportProtocolJSONRPC
	if rest {
		proto = a2a.TransportProtocolHTTPJSON
	}
	return &a2a.AgentCard{
		Name:                "Test Agent",
		Version:             "1.2.3",
		SupportedInterfaces: []*a2a.AgentInterface{a2a.NewAgentInterface(url, proto)},
		Capabilities:        a2a.AgentCapabilities{Streaming: streaming},
	}
}

func newClient(t *testing.T, c *a2a.AgentCard, opts ...traceaia2a.Option) (*a2aclient.Client, *tracetest.SpanRecorder, *sdktrace.TracerProvider) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	opts = append([]traceaia2a.Option{traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{})}, opts...)
	client, err := a2aclient.NewFromCard(context.Background(), c,
		a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(opts...)))
	if err != nil {
		t.Fatal(err)
	}
	return client, rec, tp
}

func userMessage(s string) *a2a.SendMessageRequest {
	return &a2a.SendMessageRequest{Message: a2a.NewMessage(a2a.MessageRoleUser, a2a.NewTextPart(s))}
}

func attrs(s sdktrace.ReadOnlySpan) map[attribute.Key]attribute.Value {
	m := map[attribute.Key]attribute.Value{}
	for _, kv := range s.Attributes() {
		m[kv.Key] = kv.Value
	}
	return m
}

func only(t *testing.T, rec *tracetest.SpanRecorder) sdktrace.ReadOnlySpan {
	t.Helper()
	ended := rec.Ended()
	if len(ended) != 1 {
		t.Fatalf("want 1 ended span, got %d (started %d)", len(ended), len(rec.Started()))
	}
	return ended[0]
}

func str(m map[attribute.Key]attribute.Value, k string) string { return m[attribute.Key(k)].AsString() }

func has(m map[attribute.Key]attribute.Value, k string) bool {
	_, ok := m[attribute.Key(k)]
	return ok
}

// everything returns every string the span would export: name, status,
// attributes, events and resource.
func everything(s sdktrace.ReadOnlySpan) string {
	var b strings.Builder
	b.WriteString(s.Name() + "\n" + s.Status().Description + "\n")
	for _, kv := range s.Attributes() {
		b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	for _, e := range s.Events() {
		b.WriteString(e.Name + "\n")
		for _, kv := range e.Attributes {
			b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
		}
	}
	for _, kv := range s.Resource().Attributes() {
		b.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	return b.String()
}

func TestSendMessageCompletedTask(t *testing.T) {
	for _, rest := range []bool{false, true} {
		name := map[bool]string{false: "jsonrpc", true: "rest"}[rest]
		t.Run(name, func(t *testing.T) {
			srv := startAgent(t, completes(), rest)
			client, rec, _ := newClient(t, card(srv.URL, rest, true))

			res, err := client.SendMessage(context.Background(), userMessage("question "+marker))
			if err != nil {
				t.Fatal(err)
			}
			task, ok := res.(*a2a.Task)
			if !ok || task.Status.State != a2a.TaskStateCompleted {
				t.Fatalf("result %T %+v", res, res)
			}

			s := only(t, rec)
			a := attrs(s)
			if s.Name() != "a2a.SendMessage" || s.SpanKind() != trace.SpanKindClient {
				t.Errorf("name %q kind %v", s.Name(), s.SpanKind())
			}
			if s.Parent().IsValid() {
				t.Error("one send without a parent must be a root span")
			}
			want := map[string]string{
				traceaia2a.GenAISpanKind:      "AGENT",
				traceaia2a.GenAIOperationName: "a2a.send",
				traceaia2a.A2ATaskID:          string(task.ID),
				traceaia2a.A2ATaskState:       "TASK_STATE_COMPLETED",
				traceaia2a.A2AContextID:       task.ContextID,
				traceaia2a.SessionID:          task.ContextID,
				traceaia2a.A2AAgentURL:        srv.URL,
				traceaia2a.A2AAgentCardName:   "Test Agent",
				traceaia2a.A2AAgentCardVer:    "1.2.3",
				traceaia2a.A2AMessageRole:     "ROLE_USER",
				traceaia2a.A2AArtifactType:    "text",
				traceaia2a.InputValue:         "question " + marker,
				traceaia2a.OutputValue:        "answer to question " + marker + "\ndone question " + marker,
			}
			for k, v := range want {
				if got := str(a, k); got != v {
					t.Errorf("%s = %q, want %q", k, got, v)
				}
			}
			if a[traceaia2a.A2AStreaming].AsBool() || a[traceaia2a.A2AMessagePartsCnt].AsInt64() != 1 {
				t.Errorf("streaming/parts: %v %v", a[traceaia2a.A2AStreaming], a[traceaia2a.A2AMessagePartsCnt])
			}
			if s.Status().Code != codes.Ok {
				t.Errorf("status %v", s.Status())
			}
		})
	}
}

func TestStreamIsOneSpanInOneTrace(t *testing.T) {
	srv := startAgent(t, completes(), false)
	client, rec, tp := newClient(t, card(srv.URL, false, true))
	ctx, parent := tp.Tracer("test").Start(context.Background(), "my-agent")

	var kinds []string
	for ev, err := range client.SendStreamingMessage(ctx, userMessage("stream "+marker)) {
		if err != nil {
			t.Fatal(err)
		}
		kinds = append(kinds, typeName(ev))
	}
	parent.End()
	if len(kinds) < 3 {
		t.Fatalf("the agent streamed %d events: %v", len(kinds), kinds)
	}

	var s sdktrace.ReadOnlySpan
	for _, sp := range rec.Ended() {
		if sp.Name() == "a2a.SendStreamingMessage" {
			s = sp
		}
	}
	if s == nil || len(rec.Ended()) != 2 {
		t.Fatalf("want my-agent and one stream span, got %d", len(rec.Ended()))
	}
	if s.SpanContext().TraceID() != parent.SpanContext().TraceID() || s.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Error("stream span is not a child of the caller's span")
	}
	if len(s.Events()) != len(kinds) {
		t.Errorf("want one span event per streamed event (%d), got %d", len(kinds), len(s.Events()))
	}
	for i, e := range s.Events() {
		if e.Name != traceaia2a.EventStreamEvent {
			t.Errorf("event %d name %q", i, e.Name)
		}
	}
	a := attrs(s)
	if !a[traceaia2a.A2AStreaming].AsBool() || a[traceaia2a.A2AStreamEventCount].AsInt64() != int64(len(kinds)) {
		t.Errorf("streaming %v count %v", a[traceaia2a.A2AStreaming], a[traceaia2a.A2AStreamEventCount])
	}
	if str(a, traceaia2a.A2ATaskState) != "TASK_STATE_COMPLETED" || str(a, traceaia2a.A2ATaskID) == "" {
		t.Errorf("state %q id %q", str(a, traceaia2a.A2ATaskState), str(a, traceaia2a.A2ATaskID))
	}
	if got := str(a, traceaia2a.OutputValue); got != "answer to stream "+marker+"\ndone stream "+marker {
		t.Errorf("output %q", got)
	}
	if s.Status().Code != codes.Ok {
		t.Errorf("status %v", s.Status())
	}
}

func typeName(ev a2a.Event) string {
	switch ev.(type) {
	case *a2a.Task:
		return "task"
	case *a2a.Message:
		return "message"
	case *a2a.TaskStatusUpdateEvent:
		return "status"
	case *a2a.TaskArtifactUpdateEvent:
		return "artifact"
	}
	return "?"
}

func TestFailedTaskIsError(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(map[bool]string{false: "send", true: "stream"}[stream], func(t *testing.T) {
			srv := startAgent(t, endsIn(a2a.TaskStateFailed, "tool crashed "+marker), false)
			client, rec, _ := newClient(t, card(srv.URL, false, true))
			if stream {
				for _, err := range client.SendStreamingMessage(context.Background(), userMessage("go")) {
					if err != nil {
						t.Fatal(err)
					}
				}
			} else if _, err := client.SendMessage(context.Background(), userMessage("go")); err != nil {
				t.Fatal(err)
			}
			s := only(t, rec)
			a := attrs(s)
			if s.Status().Code != codes.Error || s.Status().Description != "TASK_STATE_FAILED" {
				t.Errorf("status %+v", s.Status())
			}
			if str(a, traceaia2a.A2ATaskState) != "TASK_STATE_FAILED" || str(a, traceaia2a.ErrorType) != "TASK_STATE_FAILED" {
				t.Errorf("state %q error.type %q", str(a, traceaia2a.A2ATaskState), str(a, traceaia2a.ErrorType))
			}
			if str(a, traceaia2a.OutputValue) != "tool crashed "+marker {
				t.Errorf("the failure message is the agent's output: %q", str(a, traceaia2a.OutputValue))
			}
		})
	}
}

func TestRejectedTaskIsError(t *testing.T) {
	srv := startAgent(t, endsIn(a2a.TaskStateRejected, ""), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	if _, err := client.SendMessage(context.Background(), userMessage("go")); err != nil {
		t.Fatal(err)
	}
	s := only(t, rec)
	if s.Status().Code != codes.Error || str(attrs(s), traceaia2a.A2ATaskState) != "TASK_STATE_REJECTED" {
		t.Errorf("status %+v attrs %v", s.Status(), attrs(s))
	}
}

func TestCanceledTaskStateIsRecordedNotAnError(t *testing.T) {
	srv := startAgent(t, endsIn(a2a.TaskStateCanceled, ""), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	if _, err := client.SendMessage(context.Background(), userMessage("go")); err != nil {
		t.Fatal(err)
	}
	s := only(t, rec)
	if str(attrs(s), traceaia2a.A2ATaskState) != "TASK_STATE_CANCELED" || s.Status().Code == codes.Error {
		t.Errorf("status %+v state %q", s.Status(), str(attrs(s), traceaia2a.A2ATaskState))
	}
}

func TestInputRequiredEndsTheStream(t *testing.T) {
	srv := startAgent(t, endsIn(a2a.TaskStateInputRequired, "which city?"), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	for _, err := range client.SendStreamingMessage(context.Background(), userMessage("weather")) {
		if err != nil {
			t.Fatal(err)
		}
	}
	s := only(t, rec)
	a := attrs(s)
	if str(a, traceaia2a.A2ATaskState) != "TASK_STATE_INPUT_REQUIRED" || s.Status().Code != codes.Ok || str(a, traceaia2a.OutputValue) != "which city?" {
		t.Errorf("status %+v attrs %v", s.Status(), a)
	}
}

func TestMessageReplyWritesNoTaskID(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(map[bool]string{false: "send", true: "stream"}[stream], func(t *testing.T) {
			srv := startAgent(t, repliesWithMessage(), false)
			client, rec, _ := newClient(t, card(srv.URL, false, true))
			if stream {
				for _, err := range client.SendStreamingMessage(context.Background(), userMessage("hi")) {
					if err != nil {
						t.Fatal(err)
					}
				}
			} else if _, err := client.SendMessage(context.Background(), userMessage("hi")); err != nil {
				t.Fatal(err)
			}
			s := only(t, rec)
			a := attrs(s)
			if has(a, traceaia2a.A2ATaskID) || has(a, traceaia2a.A2ATaskState) {
				t.Errorf("a message reply has no task: %v", a)
			}
			if str(a, traceaia2a.OutputValue) != "hello hi" || s.Status().Code != codes.Ok {
				t.Errorf("output %q status %+v", str(a, traceaia2a.OutputValue), s.Status())
			}
		})
	}
}

func TestTraceparentGoesOutAndFutureAGIKeysDoNot(t *testing.T) {
	for _, rest := range []bool{false, true} {
		t.Run(map[bool]string{false: "jsonrpc", true: "rest"}[rest], func(t *testing.T) {
			t.Setenv("FI_API_KEY", "fi-key-"+marker)
			t.Setenv("FI_SECRET_KEY", "fi-secret-"+marker)
			srv := startAgent(t, completes(), rest)
			client, rec, _ := newClient(t, card(srv.URL, rest, true))
			if _, err := client.SendMessage(context.Background(), userMessage("hi")); err != nil {
				t.Fatal(err)
			}
			s := only(t, rec)
			hs := srv.seen()
			if len(hs) == 0 {
				t.Fatal("no request reached the agent")
			}
			h := hs[len(hs)-1]
			tp := h.Get("Traceparent")
			want := "00-" + s.SpanContext().TraceID().String() + "-" + s.SpanContext().SpanID().String() + "-01"
			if tp != want || len(h.Values("Traceparent")) != 1 {
				t.Errorf("traceparent %q (%d values), want %q", tp, len(h.Values("Traceparent")), want)
			}
			for k, vs := range h {
				lk := strings.ToLower(k)
				if lk == "x-api-key" || lk == "x-secret-key" || lk == "baggage" {
					t.Errorf("agent request carries %s", k)
				}
				for _, v := range vs {
					if strings.Contains(v, marker) {
						t.Errorf("agent request header %s carries a Future AGI key", k)
					}
				}
			}
		})
	}
}

func TestPropagationCanBeTurnedOff(t *testing.T) {
	srv := startAgent(t, completes(), false)
	client, _, _ := newClient(t, card(srv.URL, false, true), traceaia2a.WithPropagator(nil))
	if _, err := client.SendMessage(context.Background(), userMessage("hi")); err != nil {
		t.Fatal(err)
	}
	if h := srv.seen(); h[len(h)-1].Get("Traceparent") != "" {
		t.Error("WithPropagator(nil) must not send traceparent")
	}
}

func TestAgentURLLosesUserInfoAndQuery(t *testing.T) {
	srv := startAgent(t, completes(), false)
	u := strings.Replace(srv.URL, "http://", "http://alice:pw-"+marker+"@", 1) + "/?token=" + marker + "#frag"
	client, rec, _ := newClient(t, card(u, false, true))
	if _, err := client.SendMessage(context.Background(), userMessage("hi")); err != nil {
		t.Fatal(err)
	}
	s := only(t, rec)
	if got := str(attrs(s), traceaia2a.A2AAgentURL); got != srv.URL+"/" {
		t.Errorf("agent url %q, want %q", got, srv.URL+"/")
	}
	if strings.Contains(everything(s), marker) || strings.Contains(everything(s), "alice") {
		t.Error("credentials from the agent URL reached the span")
	}
}

func TestContextCancelEndsAStreamAndReturns(t *testing.T) {
	srv := startAgent(t, blocks(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		var last error
		n := 0
		for _, err := range client.SendStreamingMessage(ctx, userMessage("long job")) {
			if err != nil {
				last = err
				break
			}
			n++
			if n == 2 {
				cancel()
			}
		}
		done <- last
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("the stream did not return after cancel")
	}
	waitEnded(t, rec, 1)
	s := rec.Ended()[0]
	a := attrs(s)
	if s.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "canceled" {
		t.Errorf("status %+v error.type %q", s.Status(), str(a, traceaia2a.ErrorType))
	}
	if str(a, traceaia2a.A2ATaskState) != "TASK_STATE_WORKING" {
		t.Errorf("last reported state %q", str(a, traceaia2a.A2ATaskState))
	}
}

func TestBreakingOutThenCancellingEndsTheSpan(t *testing.T) {
	srv := startAgent(t, blocks(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx, cancel := context.WithCancel(context.Background())
	for _, err := range client.SendStreamingMessage(ctx, userMessage("long job")) {
		if err != nil {
			t.Fatal(err)
		}
		break // stop reading after the first event
	}
	if len(rec.Ended()) != 0 {
		t.Fatal("the span ended before the call was over")
	}
	cancel()
	waitEnded(t, rec, 1)
	if s := rec.Ended()[0]; s.Status().Code != codes.Error || str(attrs(s), traceaia2a.ErrorType) != "canceled" {
		t.Errorf("status %+v", s.Status())
	}
}

func TestAlreadyCancelledStreamEndsOnce(t *testing.T) {
	srv := startAgent(t, completes(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, err := range client.SendStreamingMessage(ctx, userMessage("hi")) {
		if err == nil {
			t.Fatal("want the context error")
		}
	}
	waitEnded(t, rec, 1)
	time.Sleep(50 * time.Millisecond)
	if n := len(rec.Ended()); n != 1 {
		t.Fatalf("want exactly one ended span, got %d", n)
	}
	if s := rec.Ended()[0]; s.Status().Code != codes.Error || str(attrs(s), traceaia2a.ErrorType) != "canceled" {
		t.Errorf("status %+v", s.Status())
	}
}

func TestTimeoutIsError(t *testing.T) {
	srv := startAgent(t, blocks(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	_, err := client.SendMessage(ctx, userMessage("long job"))
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err %v", err)
	}
	s := only(t, rec)
	if s.Status().Code != codes.Error || str(attrs(s), traceaia2a.ErrorType) != "timeout" || s.Status().Description != "timeout" {
		t.Errorf("status %+v", s.Status())
	}
}

func waitEnded(t *testing.T, rec *tracetest.SpanRecorder, n int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for len(rec.Ended()) < n {
		if time.Now().After(deadline) {
			t.Fatalf("want %d ended spans, got %d", n, len(rec.Ended()))
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestGetTaskAndCancelTask(t *testing.T) {
	srv := startAgent(t, blocks(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx := context.Background()
	req := userMessage("background job")
	req.Config = &a2a.SendMessageConfig{ReturnImmediately: true}
	res, err := client.SendMessage(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	task := res.(*a2a.Task)
	if _, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: task.ID}); err != nil {
		t.Fatal(err)
	}
	canceled, err := client.CancelTask(ctx, &a2a.CancelTaskRequest{ID: task.ID})
	if err != nil {
		t.Fatal(err)
	}
	if canceled.Status.State != a2a.TaskStateCanceled {
		t.Fatalf("cancel returned %s", canceled.Status.State)
	}
	spans := rec.Ended()
	if len(spans) != 3 {
		t.Fatalf("want 3 spans, got %d", len(spans))
	}
	for i, want := range []struct{ name, op, state string }{
		{"a2a.SendMessage", "a2a.send", ""},
		{"a2a.GetTask", "a2a.get_task", ""},
		{"a2a.CancelTask", "a2a.cancel_task", "TASK_STATE_CANCELED"},
	} {
		a := attrs(spans[i])
		if spans[i].Name() != want.name || str(a, traceaia2a.GenAIOperationName) != want.op || str(a, traceaia2a.A2ATaskID) != string(task.ID) {
			t.Errorf("span %d: %s %v", i, spans[i].Name(), a)
		}
		if want.state != "" && str(a, traceaia2a.A2ATaskState) != want.state {
			t.Errorf("span %d state %q", i, str(a, traceaia2a.A2ATaskState))
		}
		if spans[i].Status().Code != codes.Ok {
			t.Errorf("span %d status %+v", i, spans[i].Status())
		}
	}
}

func TestSubscribeToTaskIsOneSpanEndingAtCancel(t *testing.T) {
	srv := startAgent(t, blocks(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx := context.Background()
	req := userMessage("background job")
	req.Config = &a2a.SendMessageConfig{ReturnImmediately: true}
	res, err := client.SendMessage(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	task := res.(*a2a.Task)
	done := make(chan []string, 1)
	go func() {
		var kinds []string
		for ev, err := range client.SubscribeToTask(ctx, &a2a.SubscribeToTaskRequest{ID: task.ID}) {
			if err != nil {
				break
			}
			kinds = append(kinds, typeName(ev))
			if len(kinds) == 1 {
				if _, err := client.CancelTask(ctx, &a2a.CancelTaskRequest{ID: task.ID}); err != nil {
					t.Error(err)
				}
			}
		}
		done <- kinds
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("subscription did not end at cancel")
	}
	var sub sdktrace.ReadOnlySpan
	for _, s := range rec.Ended() {
		if s.Name() == "a2a.SubscribeToTask" {
			sub = s
		}
	}
	if sub == nil {
		t.Fatal("no SubscribeToTask span")
	}
	a := attrs(sub)
	if str(a, traceaia2a.GenAIOperationName) != "a2a.subscribe_to_task" || str(a, traceaia2a.A2ATaskID) != string(task.ID) ||
		str(a, traceaia2a.A2ATaskState) != "TASK_STATE_CANCELED" || !a[traceaia2a.A2AStreaming].AsBool() || sub.Status().Code == codes.Error {
		t.Errorf("status %+v attrs %v", sub.Status(), a)
	}
}

func TestProtocolErrorKeepsTheAgentMessageOnlyWhenContentIsOn(t *testing.T) {
	srv := startAgent(t, completes(), false)
	for _, hide := range []bool{false, true} {
		t.Run(map[bool]string{false: "default", true: "hidden"}[hide], func(t *testing.T) {
			client, rec, _ := newClient(t, card(srv.URL, false, true),
				traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{HideInputs: hide, HideOutputs: hide}))
			_, err := client.GetTask(context.Background(), &a2a.GetTaskRequest{ID: "no-such-task"})
			if !errors.Is(err, a2a.ErrTaskNotFound) {
				t.Fatalf("err %v", err)
			}
			s := only(t, rec)
			a := attrs(s)
			if s.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "TASK_NOT_FOUND" || s.Status().Description != "TASK_NOT_FOUND" {
				t.Errorf("status %+v error.type %q", s.Status(), str(a, traceaia2a.ErrorType))
			}
			if has(a, traceaia2a.ErrorMessage) == hide {
				t.Errorf("error.message present=%v with hide=%v", has(a, traceaia2a.ErrorMessage), hide)
			}
		})
	}
}

func TestTransportErrorRecordsOnlyAClass(t *testing.T) {
	srv := startAgent(t, completes(), false)
	c := card(srv.URL, false, true)
	srv.Close()
	client, rec, _ := newClient(t, c)
	if _, err := client.SendMessage(context.Background(), userMessage("hi")); err == nil {
		t.Fatal("want an error from a closed server")
	}
	s := only(t, rec)
	a := attrs(s)
	if s.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "transport" || s.Status().Description != "transport" || has(a, traceaia2a.ErrorMessage) {
		t.Errorf("status %+v attrs %v", s.Status(), a)
	}
	if strings.Contains(everything(s), "connection refused") || strings.Contains(everything(s), "failed to send") {
		t.Error("transport error text reached the span")
	}
}

func TestHideSwitchesRemoveContent(t *testing.T) {
	for _, viaEnv := range []bool{false, true} {
		t.Run(map[bool]string{false: "TraceConfig", true: "env"}[viaEnv], func(t *testing.T) {
			srv := startAgent(t, completes(), false)
			rec := tracetest.NewSpanRecorder()
			tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
			opts := []traceaia2a.Option{traceaia2a.WithTracerProvider(tp)}
			if viaEnv {
				t.Setenv("FI_HIDE_INPUTS", "true")
				t.Setenv("FI_HIDE_OUTPUTS", "true")
			} else {
				opts = append(opts, traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true}))
			}
			client, err := a2aclient.NewFromCard(context.Background(), card(srv.URL, false, true),
				a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(opts...)))
			if err != nil {
				t.Fatal(err)
			}
			for _, err := range client.SendStreamingMessage(context.Background(), userMessage("secret "+marker)) {
				if err != nil {
					t.Fatal(err)
				}
			}
			s := only(t, rec)
			if strings.Contains(everything(s), marker) {
				t.Errorf("content reached the span with the hide switches on:\n%s", everything(s))
			}
			a := attrs(s)
			if str(a, traceaia2a.InputValue) != traceaia2a.RedactedValue || str(a, traceaia2a.OutputValue) != traceaia2a.RedactedValue {
				t.Errorf("input %q output %q", str(a, traceaia2a.InputValue), str(a, traceaia2a.OutputValue))
			}
			if str(a, traceaia2a.A2ATaskState) != "TASK_STATE_COMPLETED" || str(a, traceaia2a.A2ATaskID) == "" {
				t.Error("the hide switches must not remove task facts")
			}
		})
	}
}

func TestCallerSessionWinsOverContextID(t *testing.T) {
	srv := startAgent(t, completes(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx := traceaia2a.ContextWithUserID(traceaia2a.ContextWithSessionID(context.Background(), "sess-1"), "user-9")
	res, err := client.SendMessage(ctx, userMessage("hi"))
	if err != nil {
		t.Fatal(err)
	}
	a := attrs(only(t, rec))
	if str(a, traceaia2a.SessionID) != "sess-1" || str(a, traceaia2a.UserID) != "user-9" || str(a, traceaia2a.A2AContextID) != res.(*a2a.Task).ContextID {
		t.Errorf("attrs %v", a)
	}
}

func TestNoSpanIsAnLLMSpan(t *testing.T) {
	srv := startAgent(t, completes(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	ctx := context.Background()
	if _, err := client.SendMessage(ctx, userMessage("a")); err != nil {
		t.Fatal(err)
	}
	for _, err := range client.SendStreamingMessage(ctx, userMessage("b")) {
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, s := range rec.Ended() {
		a := attrs(s)
		if str(a, traceaia2a.GenAISpanKind) != "AGENT" {
			t.Errorf("%s has span kind %q", s.Name(), str(a, traceaia2a.GenAISpanKind))
		}
		for k := range a {
			if strings.HasPrefix(string(k), "llm.") || strings.HasPrefix(string(k), "gen_ai.usage") || strings.HasPrefix(string(k), "gen_ai.request.model") {
				t.Errorf("%s writes model attribute %s", s.Name(), k)
			}
		}
	}
}

func TestUntracedMethodsPassThrough(t *testing.T) {
	srv := startAgent(t, completes(), false)
	client, rec, _ := newClient(t, card(srv.URL, false, true))
	_, _ = client.ListTasks(context.Background(), &a2a.ListTasksRequest{})
	if n := len(rec.Started()); n != 0 {
		t.Errorf("ListTasks started %d spans", n)
	}
}

func TestStreamingFallbackOnANonStreamingCardIsOneSpan(t *testing.T) {
	srv := startAgent(t, endsIn(a2a.TaskStateWorking, ""), false)
	client, rec, _ := newClient(t, card(srv.URL, false, false))
	req := userMessage("hi")
	req.Config = &a2a.SendMessageConfig{ReturnImmediately: true}
	for _, err := range client.SendStreamingMessage(context.Background(), req) {
		if err != nil {
			t.Fatal(err)
		}
	}
	s := only(t, rec)
	if s.Name() != "a2a.SendStreamingMessage" || s.Status().Code != codes.Ok {
		t.Errorf("%s %+v", s.Name(), s.Status())
	}
}

func TestGlobalProviderIsTheDefault(t *testing.T) {
	// With no provider the interceptor must not panic and must not start
	// recorded spans anywhere.
	srv := startAgent(t, completes(), false)
	client, err := a2aclient.NewFromCard(context.Background(), card(srv.URL, false, true),
		a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor()))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.SendMessage(context.Background(), userMessage("hi")); err != nil {
		t.Fatal(err)
	}
}
