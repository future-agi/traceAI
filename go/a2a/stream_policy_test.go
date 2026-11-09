package traceaia2a_test

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"github.com/a2aproject/a2a-go/v2/a2asrv/taskstore"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Exercise real SDK response policies over each wire transport, including its
// unary stream fallback and the persisted subscription snapshot/EOF path.
func TestStreamPolicyAcceptedOutcome(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, mode := range []string{"message", "fallback", "snapshot", "events"} {
			for _, first := range []bool{false, true} {
				for _, reject := range []bool{false, true} {
					for _, hidden := range []bool{false, true} {
						t.Run(fmt.Sprintf("%s/%s/first=%v/reject=%v/hidden=%v", transport, mode, first, reject, hidden), func(t *testing.T) {
							testStreamPolicyAcceptedOutcome(t, transport, mode, first, reject, hidden)
						})
					}
				}
			}
		}
	}
}

func testStreamPolicyAcceptedOutcome(t *testing.T, transport, mode string, first, reject, hidden bool) {
	t.Helper()
	tp, rec := serverRecorder(t)
	method := "SendStreamingMessage"
	exec := repliesWithMessage()
	if mode == "snapshot" {
		method = "SubscribeToTask"
	} else if mode == "events" {
		exec = completes()
	}
	rejection := a2a.NewError(a2a.ErrUnauthorized, "private-stream-rejection")
	var accepted []any
	var order []string
	policy := unaryPolicy{method: method, after: func(_ context.Context, resp *a2aclient.Response) error {
		if resp.Err != nil || resp.Payload == nil {
			t.Errorf("fixture returned an error before the policy: %+v", resp)
		}
		terminal := mode != "events"
		if status, ok := resp.Payload.(*a2a.TaskStatusUpdateEvent); ok {
			terminal = status.Status.State.Terminal()
		}
		order = append(order, "policy")
		if reject && terminal {
			return rejection
		}
		// Both replacement and in-place rewrites must be observed after policies.
		switch event := resp.Payload.(type) {
		case *a2a.Message:
			resp.Payload = a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("accepted message"))
		case *a2a.Task:
			if mode == "snapshot" {
				// A nonterminal accepted snapshot must wait for actual clean EOF.
				event.Status.State = a2a.TaskStateAuthRequired
				event.Status.Message = a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("accepted snapshot"))
			}
		case *a2a.TaskArtifactUpdateEvent:
			event.Artifact.Parts = a2a.ContentParts{a2a.NewTextPart("accepted artifact")}
		case *a2a.TaskStatusUpdateEvent:
			if terminal {
				copy := *event
				copy.Status.Message = a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("accepted final"))
				resp.Payload = &copy
			}
		}
		accepted = append(accepted, resp.Payload)
		return nil
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tc := traceaia2a.TraceConfig{HideInputs: hidden, HideOutputs: hidden}
	var client *traceaia2a.Client
	if mode == "snapshot" {
		// Seed a persisted task without a live executor, guaranteeing clean EOF.
		store := taskstore.NewInMemory(nil)
		_, err := store.Create(ctx, &a2a.Task{ID: "policy-snapshot", Status: a2a.TaskStatus{
			State: a2a.TaskStateInputRequired, Message: a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("obsolete snapshot")),
		}})
		if err != nil {
			t.Fatal(err)
		}
		handler := a2asrv.NewHandler(exec, a2asrv.WithTaskStore(store))
		client = qualificationClientWithHandler(t, transport, handler, tp, tc, first, true, policy)
	} else {
		client = qualificationClientWithStreaming(t, transport, exec, tp, tc, first, mode != "fallback", policy)
	}
	stream := client.SendStreamingMessage(ctx, userMessage("source input"))
	if mode == "snapshot" {
		stream = client.SubscribeToTask(ctx, &a2a.SubscribeToTaskRequest{ID: "policy-snapshot"})
	}
	yielded := 0
	var returned error
	for event, err := range stream {
		order = append(order, "yield")
		if err != nil {
			if returned != nil || event != nil {
				t.Fatal("error event changed")
			}
			returned = err
			continue
		}
		if yielded >= len(accepted) || event != accepted[yielded] {
			t.Fatal("wrapper changed event identity or policy ordering")
		}
		yielded++
		// Capture must happen before yielding to the caller, not during unwinding.
		switch event := event.(type) {
		case *a2a.Message:
			event.Parts = a2a.ContentParts{a2a.NewTextPart("caller mutation")}
		case *a2a.Task:
			if mode == "snapshot" {
				event.Status.Message.Parts = a2a.ContentParts{a2a.NewTextPart("caller mutation")}
			}
		case *a2a.TaskArtifactUpdateEvent:
			event.Artifact.Parts = a2a.ContentParts{a2a.NewTextPart("caller mutation")}
		case *a2a.TaskStatusUpdateEvent:
			if event.Status.Message != nil {
				event.Status.Message.Parts = a2a.ContentParts{a2a.NewTextPart("caller mutation")}
			}
		}
	}
	if (reject && returned != rejection) || (!reject && returned != nil) {
		t.Fatalf("policy error identity changed: %v", returned)
	}
	if yielded != len(accepted) || len(order) == 0 || len(order)%2 != 0 {
		t.Fatalf("lost events or callbacks: yielded=%d accepted=%d order=%v", yielded, len(accepted), order)
	}
	for index := 0; index < len(order); index += 2 {
		if order[index] != "policy" || order[index+1] != "yield" {
			t.Fatalf("SDK event ordering changed: %v", order)
		}
	}
	if ctx.Err() != nil {
		t.Fatalf("stream did not finish independently of caller cancellation: %v", ctx.Err())
	}
	// Completion is synchronous with iterator return, not caller cancellation.
	count := 0
	for _, span := range rec.Ended() {
		if span.SpanKind() != trace.SpanKindClient || span.Name() != "a2a."+method {
			continue
		}
		count++
		a := attrs(span)
		wantOutput := "accepted message"
		wantTypes := []string{"message"}
		if mode == "snapshot" {
			wantOutput, wantTypes = "accepted snapshot", []string{"task"}
		} else if mode == "events" {
			wantOutput, wantTypes = "accepted artifact\naccepted final", []string{"task", "status-update", "artifact-update", "status-update"}
		} else if mode == "fallback" {
			wantTypes = nil
		}
		if reject {
			wantOutput, wantTypes = "", nil
			if mode == "events" {
				wantOutput, wantTypes = "accepted artifact", []string{"task", "status-update", "artifact-update"}
			}
			if span.Status().Code != codes.Error || str(a, traceaia2a.ErrorType) != "UNAUTHORIZED" {
				t.Errorf("rejected stream not ERROR: %v %v", span.Status(), a)
			}
			if hidden {
				if has(a, traceaia2a.ErrorMessage) || span.Status().Description != "" {
					t.Error("private rejection details escaped")
				}
			} else if str(a, traceaia2a.ErrorMessage) != "private-stream-rejection" {
				t.Error("public rejection details missing")
			}
		} else if span.Status().Code != codes.Ok {
			t.Errorf("accepted stream not OK: %v", span.Status())
		}
		if mode == "snapshot" && !reject && str(a, traceaia2a.A2ATaskState) != string(a2a.TaskStateAuthRequired) {
			t.Error("rewritten snapshot state was not recorded")
		}
		if hidden && wantOutput != "" {
			wantOutput = traceaia2a.RedactedValue
		}
		if str(a, traceaia2a.OutputValue) != wantOutput || (wantOutput == "" && has(a, traceaia2a.OutputValue)) {
			t.Errorf("wrong accepted output: got %q want %q", str(a, traceaia2a.OutputValue), wantOutput)
		}
		var eventTypes []string
		for _, event := range span.Events() {
			for _, attr := range event.Attributes {
				if string(attr.Key) == traceaia2a.A2AEventType {
					eventTypes = append(eventTypes, attr.Value.AsString())
				}
			}
		}
		if !slices.Equal(eventTypes, wantTypes) || len(span.Events()) != len(wantTypes) {
			t.Errorf("wrong accepted event ordering: got %v want %v", eventTypes, wantTypes)
		}
		if mode != "fallback" && a[traceaia2a.A2AStreamEventCount].AsInt64() != int64(len(wantTypes)) {
			t.Error("event count includes rejected or duplicate events")
		}
		if strings.Contains(everything(span), "caller mutation") {
			t.Error("recording was deferred until after yielding to the caller")
		}
	}
	if count != 1 {
		t.Fatalf("want exactly one completed stream span, got %d", count)
	}
	cancel()
}
