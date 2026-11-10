package traceaia2a_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
)

func TestArtifactUpdatesMatchPersistedTask(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, hidden := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/hidden=%v", transport, hidden), func(t *testing.T) {
				tp, rec := serverRecorder(t)
				exec := &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
					if !yield(a2a.NewSubmittedTask(ec, ec.Message), nil) {
						return
					}
					for _, change := range []struct {
						id, text    string
						appendParts bool
					}{
						{"a", "obsolete", false},
						{"b", "second", false},
						{"a", "correct", false},
						{"a", "tail", true},
						{"b", "replaced second", false},
					} {
						event := a2a.NewArtifactEvent(ec, a2a.NewTextPart(change.text))
						event.Artifact.ID = a2a.ArtifactID(change.id)
						event.Append = change.appendParts
						if !yield(event, nil) {
							return
						}
					}
					yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, nil), nil)
				}}
				client := serverClient(t, transport, exec, tp, traceaia2a.TraceConfig{HideOutputs: hidden})
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				var taskID a2a.TaskID
				var received []string
				for event, err := range client.SendStreamingMessage(ctx, userMessage("update artifacts")) {
					if err != nil {
						t.Fatal(err)
					}
					switch e := event.(type) {
					case *a2a.Task:
						taskID = e.ID
					case *a2a.TaskArtifactUpdateEvent:
						received = append(received, e.Artifact.Parts[0].Text())
					}
				}
				if strings.Join(received, ",") != "obsolete,second,correct,tail,replaced second" {
					t.Fatalf("SDK events changed: %v", received)
				}
				task, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: taskID})
				if err != nil {
					t.Fatal(err)
				}
				var persisted []string
				for _, artifact := range task.Artifacts {
					for _, part := range artifact.Parts {
						persisted = append(persisted, part.Text())
					}
				}
				want := "correct\ntail\nreplaced second"
				if strings.Join(persisted, "\n") != want {
					t.Fatalf("invalid fixture: persisted=%v", persisted)
				}
				if hidden {
					want = traceaia2a.RedactedValue
				}
				count := 0
				for _, span := range rec.Ended() {
					if span.Name() != "a2a.SendStreamingMessage" {
						continue
					}
					count++
					if span.Status().Code != codes.Ok || str(attrs(span), traceaia2a.OutputValue) != want {
						t.Errorf("%s final output=%q want=%q", span.SpanKind(), str(attrs(span), traceaia2a.OutputValue), want)
					}
					if len(span.Events()) != 7 {
						t.Errorf("lost stream events: %d", len(span.Events()))
					}
				}
				if count != 2 {
					t.Fatalf("want client/server parity, got %d spans", count)
				}
			})
		}
	}
}
