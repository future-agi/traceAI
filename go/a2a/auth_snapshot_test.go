package traceaia2a_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
)

// Unary calls return the authorization prompt while the executor is still alive.
// Capturing that snapshot must not depend on whether its state ends a stream.
func TestAuthRequiredUnarySnapshotOutput(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, hide := range []bool{false, true} {
			name := transport + "/visible"
			if hide {
				name = transport + "/hidden"
			}
			t.Run(name, func(t *testing.T) {
				release := make(chan struct{})
				defer close(release)
				const challenge = "Please authorize the synthetic calendar " + marker
				executor := &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
					if !yield(a2a.NewSubmittedTask(ec, ec.Message), nil) ||
						!yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateAuthRequired,
							a2a.NewMessageForTask(a2a.MessageRoleAgent, ec, a2a.NewTextPart(challenge))), nil) {
						return
					}
					<-release
					yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, nil), nil)
				}}
				tp, rec := serverRecorder(t)
				client := serverClient(t, transport, executor, tp, traceaia2a.TraceConfig{HideOutputs: hide})
				ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				result, err := client.SendMessage(ctx, userMessage("start protected task"))
				if err != nil {
					t.Fatal(err)
				}
				task, ok := result.(*a2a.Task)
				if !ok {
					t.Fatalf("expected task, got %T", result)
				}
				fetched, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: task.ID})
				if err != nil {
					t.Fatal(err)
				}
				for _, snapshot := range []*a2a.Task{task, fetched} {
					if snapshot.Status.State != a2a.TaskStateAuthRequired || snapshot.Status.Message == nil ||
						len(snapshot.Status.Message.Parts) != 1 || snapshot.Status.Message.Parts[0].Text() != challenge {
						t.Fatalf("framework did not return the auth challenge: %+v", snapshot)
					}
				}
				if len(rec.Ended()) != 4 || len(serverSpans(rec)) != 2 {
					t.Fatalf("want two completed client/server pairs, got %d spans", len(rec.Ended()))
				}
				want := challenge
				if hide {
					want = "__REDACTED__"
				}
				for _, span := range rec.Ended() {
					if got := str(attrs(span), traceaia2a.OutputValue); got != want {
						t.Errorf("%s %s output.value=%q, want %q", span.SpanKind(), span.Name(), got, want)
					}
					if span.Status().Code != codes.Ok || str(attrs(span), traceaia2a.A2ATaskState) != string(a2a.TaskStateAuthRequired) {
						t.Errorf("auth-required snapshot must not be an error: %v", span.Status())
					}
					if hide && strings.Contains(everything(span), marker) {
						t.Error("authorization prompt bypassed output privacy")
					}
				}
			})
		}
	}
}
