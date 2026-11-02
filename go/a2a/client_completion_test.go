package traceaia2a

import (
	"context"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// A wrapper context is not enough: only its owning call may wait for completion.
// In particular, nested unwrapped calls must retain the legacy auth boundary.
func TestClientAuthRequiredCompletionBoundary(t *testing.T) {
	for _, method := range []string{"SubscribeToTask", "SendStreamingMessage"} {
		for _, eventType := range []string{"task", "status"} {
			for _, scope := range []string{"none", "owned", "claimed", "other-method", "server"} {
				t.Run(method+"/"+eventType+"/"+scope, func(t *testing.T) {
					rec := tracetest.NewSpanRecorder()
					tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
					t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
					tracing := NewInterceptor(WithTracerProvider(tp), WithTraceConfig(TraceConfig{}))
					ctx, cancel := context.WithCancel(context.Background())
					defer cancel()
					slot := &streamCompletion{method: method}
					key := clientCompletionKey
					switch scope {
					case "claimed":
						slot.state = &callState{method: method}
					case "other-method":
						slot.method = "GetTask"
					case "server":
						key = serverCompletionKey
					}
					previousOwner := slot.state
					if scope != "none" {
						ctx = context.WithValue(ctx, key, slot)
					}
					ctx, _, err := tracing.Before(ctx, &a2aclient.Request{Method: method})
					if err != nil {
						t.Fatal(err)
					}
					status := a2a.TaskStatus{State: a2a.TaskStateAuthRequired}
					var event any = &a2a.Task{ID: "auth-task", Status: status}
					if eventType == "status" {
						event = &a2a.TaskStatusUpdateEvent{TaskID: "auth-task", Status: status}
					}
					if err := tracing.After(ctx, &a2aclient.Response{Method: method, Payload: event}); err != nil {
						t.Fatal(err)
					}
					wantEvents := 1
					if scope == "owned" {
						if len(rec.Ended()) != 0 {
							t.Fatal("wrapped active authorization stream ended prematurely")
						}
						if slot.state != ctx.Value(stateKey{}).(*callState) {
							t.Fatal("wrapper did not claim the call")
						}
						// Retain later events rather than ending at auth-required.
						_ = tracing.After(ctx, &a2aclient.Response{Method: method, Payload: &a2a.TaskStatusUpdateEvent{
							TaskID: "auth-task", Status: a2a.TaskStatus{State: a2a.TaskStateWorking},
						}})
						slot.end(nil)
						wantEvents = 2
					} else if slot.state != previousOwner {
						t.Fatal("unwrapped call changed an unrelated completion slot")
					}
					if len(rec.Started()) != 1 || len(rec.Ended()) != 1 {
						t.Fatalf("want one completed span: started=%d ended=%d", len(rec.Started()), len(rec.Ended()))
					}
					cancel()
					span := rec.Ended()[0]
					if span.Status().Code != codes.Ok || len(span.Events()) != wantEvents {
						t.Fatalf("wrong completion: status=%v events=%d", span.Status(), len(span.Events()))
					}
				})
			}
		}
	}
}
