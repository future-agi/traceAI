package traceaia2a_test

import (
	"context"
	"iter"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
)

type streamHandler struct {
	a2asrv.RequestHandler
	stream func(context.Context) iter.Seq2[a2a.Event, error]
}

func (h streamHandler) SubscribeToTask(ctx context.Context, _ *a2a.SubscribeToTaskRequest) iter.Seq2[a2a.Event, error] {
	return h.stream(ctx)
}

func TestStreamCompletionLifecycle(t *testing.T) {
	for _, outcome := range []string{"empty", "snapshot", "break", "error", "panic", "cancel", "timeout"} {
		t.Run(outcome, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))
			var innerCtx context.Context
			rejection := a2a.NewError(a2a.ErrUnauthorized, marker)
			calls := 0
			base := streamHandler{stream: func(ctx context.Context) iter.Seq2[a2a.Event, error] {
				return func(yield func(a2a.Event, error) bool) {
					calls++
					innerCtx = ctx
					if outcome == "empty" {
						return
					}
					if outcome == "panic" {
						panic("synthetic panic")
					}
					if outcome == "error" {
						yield(nil, rejection)
						return
					}
					if outcome == "cancel" || outcome == "timeout" {
						<-ctx.Done()
						return // Completion must check context even without an error event.
					}
					yield(&a2a.Task{ID: "snapshot", Status: a2a.TaskStatus{State: a2a.TaskStateAuthRequired,
						Message: a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("authorize"))}}, nil)
				}
			}}
			h := traceaia2a.WrapHandler(&a2asrv.InterceptedHandler{Handler: base, Interceptors: []a2asrv.CallInterceptor{tracing}})
			// Double-wrapping must not create or end duplicate spans.
			h = traceaia2a.WrapHandler(h)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if outcome == "cancel" {
				cancel()
			}
			if outcome == "timeout" {
				var stop context.CancelFunc
				ctx, stop = context.WithDeadline(ctx, time.Now().Add(-time.Second))
				defer stop()
			}
			seq := h.SubscribeToTask(ctx, &a2a.SubscribeToTaskRequest{ID: "snapshot"})
			if calls != 0 || len(rec.Started()) != 0 {
				t.Fatal("constructing an iterator executed the call")
			}
			for attempt := 0; attempt < 2; attempt++ {
				panicked := false
				func() {
					defer func() {
						if p := recover(); p != nil {
							if p != "synthetic panic" {
								t.Fatalf("panic changed: %v", p)
							}
							panicked = true
						}
					}()
					for event, err := range seq {
						if outcome == "error" {
							if err != rejection {
								t.Fatal("error identity changed")
							}
						} else if err != nil || event == nil {
							t.Fatalf("event or error changed: %v %v", event, err)
						}
						if outcome == "break" {
							break
						}
					}
				}()
				if panicked != (outcome == "panic") {
					t.Fatal("panic was swallowed or invented")
				}
				if calls != attempt+1 || len(rec.Ended()) != attempt+1 || len(rec.Started()) != attempt+1 {
					t.Fatalf("iterator state not per-call: calls=%d started=%d ended=%d", calls, len(rec.Started()), len(rec.Ended()))
				}
				span := rec.Ended()[attempt]
				wantError := outcome != "empty" && outcome != "snapshot"
				if (span.Status().Code == codes.Error) != wantError {
					t.Fatalf("incorrect completion: %v", span.Status())
				}
				if outcome == "snapshot" && str(attrs(span), traceaia2a.OutputValue) != "authorize" {
					t.Fatal("EOF lost the auth snapshot's output")
				}
				if outcome == "timeout" && str(attrs(span), traceaia2a.ErrorType) != "timeout" {
					t.Fatal("deadline was misclassified")
				}
				if innerCtx.Err() == nil {
					t.Fatal("wrapper did not release its child context")
				}
				if outcome != "cancel" && outcome != "timeout" && ctx.Err() != nil {
					t.Fatal("wrapper canceled its caller's context")
				}
			}
		})
	}
}

func TestAuthStreamWaitsForCompletion(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, abandon := range []bool{false, true} {
			t.Run(transport+map[bool]string{false: "/complete", true: "/abandon"}[abandon], func(t *testing.T) {
				tp, rec := serverRecorder(t)
				release := make(chan struct{})
				defer close(release)
				exec := &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
					if !yield(a2a.NewSubmittedTask(ec, ec.Message), nil) ||
						!yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateAuthRequired, a2a.NewMessageForTask(a2a.MessageRoleAgent, ec, a2a.NewTextPart("stale auth prompt"))), nil) {
						return
					}
					select {
					case <-release:
					case <-ctx.Done():
						return
					}
					yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, nil), nil)
				}}
				client := serverClient(t, transport, exec, tp, traceaia2a.TraceConfig{})
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				sawAuth := false
				for event, err := range client.SendStreamingMessage(ctx, userMessage("protected task")) {
					if err != nil {
						t.Fatal(err)
					}
					if status, ok := event.(*a2a.TaskStatusUpdateEvent); ok && status.Status.State == a2a.TaskStateAuthRequired {
						sawAuth = true
						if len(rec.Ended()) != 0 {
							t.Fatal("auth-required prematurely completed an active stream")
						}
						if abandon {
							break
						}
						release <- struct{}{}
					}
				}
				if !sawAuth || ctx.Err() != nil {
					t.Fatal("missing auth event or caller context was canceled")
				}
				waitEnded(t, rec, 2)
				if len(rec.Started()) != 2 {
					t.Fatal("duplicate spans")
				}
				for _, span := range rec.Ended() {
					if (span.Status().Code == codes.Error) != abandon {
						t.Fatalf("wrong completion status: %v", span.Status())
					}
					if !abandon && str(attrs(span), traceaia2a.OutputValue) != "" {
						t.Fatal("completed stream retained a stale authorization prompt")
					}
				}
			})
		}
	}
}
