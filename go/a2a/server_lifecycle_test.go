package traceaia2a_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func serverRecorder(t *testing.T) (*sdktrace.TracerProvider, *tracetest.SpanRecorder) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return tp, rec
}

func serverSpans(rec *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	var result []sdktrace.ReadOnlySpan
	for _, s := range rec.Ended() {
		if s.SpanKind() == trace.SpanKindServer {
			result = append(result, s)
		}
	}
	return result
}

func TestServerTaskLifecycle(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			client := serverClient(t, transport, blocks(), tp, traceaia2a.TraceConfig{})
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			req := userMessage("background")
			req.Config = &a2a.SendMessageConfig{ReturnImmediately: true}
			res, err := client.SendMessage(ctx, req)
			if err != nil {
				t.Fatal(err)
			}
			task := res.(*a2a.Task)
			if _, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: task.ID}); err != nil {
				t.Fatal(err)
			}
			n := 0
			for _, err := range client.SubscribeToTask(ctx, &a2a.SubscribeToTaskRequest{ID: task.ID}) {
				if err != nil {
					t.Fatal(err)
				}
				n++
				if n == 1 {
					if _, err := client.CancelTask(ctx, &a2a.CancelTaskRequest{ID: task.ID}); err != nil {
						t.Fatal(err)
					}
				}
			}
			spans := serverSpans(rec)
			if len(spans) != 4 || len(rec.Ended()) != 8 {
				t.Fatalf("want 4 client/server pairs, got %d server, %d total", len(spans), len(rec.Ended()))
			}
			want := map[string]string{"a2a.SendMessage": "a2a.send", "a2a.GetTask": "a2a.get_task", "a2a.CancelTask": "a2a.cancel_task", "a2a.SubscribeToTask": "a2a.subscribe_to_task"}
			for _, s := range spans {
				a := attrs(s)
				if want[s.Name()] == "" || str(a, traceaia2a.GenAIOperationName) != want[s.Name()] || str(a, traceaia2a.A2ATaskID) != string(task.ID) || s.Status().Code != codes.Ok {
					t.Fatalf("unexpected span: %s %v %v", s.Name(), a, s.Status())
				}
				delete(want, s.Name())
			}
			if len(want) != 0 {
				t.Fatalf("missing calls: %v", want)
			}
		})
	}
}

func TestServerTerminalStatesAndPrivacy(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, state := range []a2a.TaskState{a2a.TaskStateCompleted, a2a.TaskStateFailed, a2a.TaskStateRejected, a2a.TaskStateCanceled, a2a.TaskStateInputRequired} {
			t.Run(transport+"/"+string(state), func(t *testing.T) {
				tp, rec := serverRecorder(t)
				client := serverClient(t, transport, endsIn(state, "private output "+marker), tp, traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true})
				ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				for _, err := range client.SendStreamingMessage(ctx, userMessage("private input "+marker)) {
					if err != nil {
						t.Fatal(err)
					}
				}
				spans := serverSpans(rec)
				if len(spans) != 1 {
					t.Fatalf("want one server span, got %d", len(spans))
				}
				s := spans[0]
				a := attrs(s)
				if strings.Contains(everything(s), marker) || str(a, traceaia2a.InputValue) != "__REDACTED__" || str(a, traceaia2a.OutputValue) != "__REDACTED__" {
					t.Fatalf("content privacy not applied: %v", a)
				}
				if str(a, traceaia2a.A2ATaskState) != string(state) {
					t.Fatal("missing task state")
				}
				wantError := state == a2a.TaskStateFailed || state == a2a.TaskStateRejected
				if (s.Status().Code == codes.Error) != wantError {
					t.Fatalf("unexpected status %v for %s", s.Status(), state)
				}
			})
		}
	}
}

func TestServerProtocolErrors(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			client := serverClient(t, transport, completes(), tp, traceaia2a.TraceConfig{HideOutputs: true})
			_, err := client.GetTask(context.Background(), &a2a.GetTaskRequest{ID: "missing"})
			if !errors.Is(err, a2a.ErrTaskNotFound) {
				t.Fatalf("protocol error was changed: %v", err)
			}
			spans := serverSpans(rec)
			if len(spans) != 1 {
				t.Fatalf("want one server span, got %d", len(spans))
			}
			s := spans[0]
			if s.Status().Code != codes.Error || str(attrs(s), traceaia2a.ErrorType) != "TASK_NOT_FOUND" || has(attrs(s), traceaia2a.ErrorMessage) {
				t.Fatalf("unexpected error span: %v %v", s.Status(), attrs(s))
			}
		})
	}
}

func TestServerClientDisconnectEndsStream(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			client := serverClient(t, transport, blocks(), tp, traceaia2a.TraceConfig{})
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			for _, err := range client.SendStreamingMessage(ctx, userMessage("cancel me")) {
				if err != nil {
					t.Fatal(err)
				}
				break
			}
			cancel()
			waitEnded(t, rec, 2)
			spans := serverSpans(rec)
			if len(spans) != 1 || spans[0].Status().Code != codes.Error || str(attrs(spans[0]), traceaia2a.ErrorType) != "canceled" {
				t.Fatalf("missing canceled server span: %v", spans)
			}
		})
	}
}

// Exercise extraction through the real handler hook without relying on an HTTP
// transport to normalize malformed metadata. Also covers in-process callers.
func TestServerPropagationPolicies(t *testing.T) {
	remote := "00-11111111111111111111111111111111-2222222222222222-01"
	for _, name := range []string{"w3c", "absent", "invalid", "duplicate", "disabled", "local-parent", "baggage-opt-in"} {
		t.Run(name, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			ctx := context.Background()
			params := map[string][]string{"TraceParent": {remote}, "TraceState": {"vendor=value"}, "baggage": {"private=" + marker}, "Authorization": {"Bearer " + marker}}
			opts := []traceaia2a.Option{traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{})}
			var local trace.Span
			switch name {
			case "absent":
				params = nil
			case "invalid":
				params["TraceParent"] = []string{"bad"}
			case "duplicate":
				params["TraceParent"] = []string{remote, remote}
			case "disabled":
				opts = append(opts, traceaia2a.WithPropagator(nil))
			case "local-parent":
				ctx, local = tp.Tracer("transport").Start(ctx, "transport")
				defer local.End()
			case "baggage-opt-in":
				opts = append(opts, traceaia2a.WithPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{})))
			}
			exec := repliesWithMessage()
			inner := exec.execute
			exec.execute = func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
				if got := baggage.FromContext(ctx).Member("private").Value(); (got == marker) != (name == "baggage-opt-in") {
					t.Errorf("unexpected baggage extraction for %s", name)
				}
				inner(ctx, ec, yield)
			}
			h := a2asrv.NewHandler(exec, a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(opts...)))
			ctx, _ = a2asrv.NewCallContext(ctx, a2asrv.NewServiceParams(params))
			if _, err := h.SendMessage(ctx, userMessage("hello")); err != nil {
				t.Fatal(err)
			}
			s := serverSpans(rec)[0]
			switch name {
			case "w3c", "baggage-opt-in":
				if s.Parent().SpanID().String() != "2222222222222222" || !s.Parent().IsRemote() || s.SpanContext().TraceState().String() != "vendor=value" {
					t.Fatal("W3C context not extracted")
				}
			case "local-parent":
				if s.Parent().SpanID() != local.SpanContext().SpanID() || s.Parent().IsRemote() {
					t.Fatal("local transport parent was replaced")
				}
			default:
				if s.Parent().IsValid() {
					t.Fatal("unexpected parent")
				}
			}
			if strings.Contains(everything(s), marker) {
				t.Fatal("headers or baggage reached span attributes")
			}
		})
	}
}

// Rejection by another official interceptor must still finish our span, and
// return precisely the original result/error without creating executor spans.
type rejectingServer struct {
	a2asrv.PassthroughCallInterceptor
	err error
}

func (i rejectingServer) Before(ctx context.Context, _ *a2asrv.CallContext, _ *a2asrv.Request) (context.Context, any, error) {
	return ctx, nil, i.err
}

func TestServerInterceptorRejectionAndUntracedMethods(t *testing.T) {
	tp, rec := serverRecorder(t)
	failure := a2a.NewError(a2a.ErrUnauthorized, "private "+marker)
	h := a2asrv.NewHandler(completes(), a2asrv.WithCallInterceptors(
		traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{HideInputs: true})), rejectingServer{err: failure}))
	for _, streaming := range []bool{false, true} {
		if streaming {
			for _, err := range h.SendStreamingMessage(context.Background(), userMessage(marker)) {
				if err != failure {
					t.Fatalf("error changed: %v", err)
				}
			}
		} else if _, err := h.SendMessage(context.Background(), userMessage(marker)); err != failure {
			t.Fatalf("error changed: %v", err)
		}
	}
	if _, err := h.GetExtendedAgentCard(context.Background(), &a2a.GetExtendedAgentCardRequest{}); err != failure {
		t.Fatalf("error changed: %v", err)
	}
	spans := rec.Ended()
	if len(spans) != 2 {
		t.Fatalf("want exactly two protocol spans, got %d", len(spans))
	}
	for _, s := range spans {
		if s.Status().Code != codes.Error || strings.Contains(everything(s), marker) {
			t.Fatal("incorrect rejection span")
		}
	}
}

func TestServerNestedOutboundCallDoesNotCorruptState(t *testing.T) {
	tp, rec := serverRecorder(t)
	remote := serverClient(t, "jsonrpc", repliesWithMessage(), tp, traceaia2a.TraceConfig{})
	exec := repliesWithMessage()
	inner := exec.execute
	exec.execute = func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
		if _, err := remote.SendMessage(ctx, userMessage("nested")); err != nil {
			yield(nil, err)
			return
		}
		inner(ctx, ec, yield)
	}
	h := a2asrv.NewHandler(exec, a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))))
	if _, err := h.SendMessage(context.Background(), userMessage("outer")); err != nil {
		t.Fatal(err)
	}
	spans := rec.Ended()
	if len(spans) != 3 {
		t.Fatalf("want outer server -> client -> remote server, got %d spans", len(spans))
	}
	byID := map[trace.SpanID]sdktrace.ReadOnlySpan{}
	for _, s := range spans {
		byID[s.SpanContext().SpanID()] = s
	}
	for _, s := range spans {
		if s.Parent().IsValid() && byID[s.Parent().SpanID()] == nil {
			t.Fatal("broken nested call tree")
		}
		if s.Status().Code != codes.Ok {
			t.Fatal("nested call did not complete")
		}
	}
}

func TestServerAuthRequiredDoesNotEndActiveStream(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			exec := &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
				_ = yield(a2a.NewSubmittedTask(ec, ec.Message), nil) &&
					yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateAuthRequired, nil), nil) &&
					yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, a2a.NewMessageForTask(a2a.MessageRoleAgent, ec, a2a.NewTextPart("authenticated"))), nil)
			}}
			client := serverClient(t, transport, exec, tp, traceaia2a.TraceConfig{})
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			for _, err := range client.SendStreamingMessage(ctx, userMessage("go")) {
				if err != nil {
					t.Fatal(err)
				}
			}
			if len(rec.Ended()) != 2 {
				t.Fatalf("want one client/server pair, got %d", len(rec.Ended()))
			}
			for _, s := range rec.Ended() {
				if str(attrs(s), traceaia2a.A2ATaskState) != string(a2a.TaskStateCompleted) || str(attrs(s), traceaia2a.OutputValue) != "authenticated" || len(s.Events()) != 3 {
					t.Fatalf("span ended at auth-required: %s %v", s.Name(), attrs(s))
				}
			}
		})
	}
}
