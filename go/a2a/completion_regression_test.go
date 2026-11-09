package traceaia2a_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"github.com/a2aproject/a2a-go/v2/a2asrv/taskstore"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
)

// Exercise the newly introduced server spans with the shared FI error-privacy contract.
func TestServerErrorPrivacyFromEnv(t *testing.T) {
	for _, flag := range []string{"FI_HIDE_ERROR_DETAILS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES"} {
		t.Run(flag, func(t *testing.T) {
			for _, name := range []string{"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES"} {
				t.Setenv(name, "false")
			}
			if flag != "FI_HIDE_ERROR_DETAILS" {
				t.Setenv("FI_HIDE_ERROR_DETAILS", "")
			}
			t.Setenv(flag, "true")
			tp, rec := serverRecorder(t)
			tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))
			rejection := a2a.NewError(a2a.ErrUnauthorized, "synthetic-private-error-92831")
			h := a2asrv.NewHandler(repliesWithMessage(), a2asrv.WithCallInterceptors(tracing, tracing.Wrap(responsePolicy{after: func(context.Context, *a2asrv.CallContext, *a2asrv.Response) error { return rejection }})))
			_, err := h.SendMessage(context.Background(), userMessage("safe input"))
			if err != rejection {
				t.Fatalf("error identity changed: %v", err)
			}
			spans := serverSpans(rec)
			if len(spans) != 1 || spans[0].Status().Code != codes.Error {
				t.Fatal("expected one ERROR server span")
			}
			if strings.Contains(everything(spans[0]), "synthetic-private-error-92831") {
				t.Errorf("%s=true leaked error.message=%q", flag, str(attrs(spans[0]), traceaia2a.ErrorMessage))
			}
		})
	}
}

// A persisted auth-required task has no active execution after a restart. The
// official handler returns its snapshot and closes the subscription successfully.
func TestAuthSnapshotSubscriptionEOF(t *testing.T) {
	for _, transport := range []string{"direct", "jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			store := taskstore.NewInMemory(nil)
			task := &a2a.Task{ID: "persisted-auth-task", ContextID: "persisted-context", Status: a2a.TaskStatus{State: a2a.TaskStateAuthRequired}}
			if _, err := store.Create(context.Background(), task); err != nil {
				t.Fatal(err)
			}
			tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))
			h := a2asrv.NewHandler(repliesWithMessage(), a2asrv.WithTaskStore(store), a2asrv.WithCallInterceptors(tracing))
			subscribe := traceaia2a.WrapHandler(h).SubscribeToTask
			want := 1
			if transport != "direct" {
				client := serverClient(t, transport, repliesWithMessage(), tp, traceaia2a.TraceConfig{}, a2asrv.WithTaskStore(store))
				subscribe, want = client.SubscribeToTask, 2
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			n := 0
			for ev, err := range subscribe(ctx, &a2a.SubscribeToTaskRequest{ID: task.ID}) {
				if err != nil {
					t.Fatalf("unexpected protocol failure: %v", err)
				}
				got, ok := ev.(*a2a.Task)
				if !ok || got.Status.State != a2a.TaskStateAuthRequired {
					t.Fatalf("unexpected snapshot: %v", ev)
				}
				n++
			}
			if n != 1 || ctx.Err() != nil {
				t.Fatalf("not a clean EOF: n=%d ctx=%v", n, ctx.Err())
			}
			t.Logf("clean EOF before caller cancellation: started=%d ended=%d", len(rec.Started()), len(rec.Ended()))
			if len(rec.Ended()) != want {
				t.Errorf("completed subscription left spans open: started=%d ended=%d want=%d", len(rec.Started()), len(rec.Ended()), want)
			}
			cancel()
			waitEnded(t, rec, want)
			for _, s := range rec.Ended() {
				t.Logf("after caller cancellation: %s status=%v error.type=%s", s.SpanKind(), s.Status(), str(attrs(s), traceaia2a.ErrorType))
				if s.Status().Code != codes.Ok {
					t.Errorf("successful subscription was falsely reported as an error: %v", s.Status())
				}
			}
		})
	}
}
