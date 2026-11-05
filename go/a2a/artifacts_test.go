package traceaia2a

import (
	"context"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func artifact(id, text string) *a2a.Artifact {
	return &a2a.Artifact{ID: a2a.ArtifactID(id), Parts: a2a.ContentParts{a2a.NewTextPart(text)}}
}

func TestArtifactStateUpdates(t *testing.T) {
	var state artifactState
	first := artifact("a", "obsolete")
	state.update(first, false)
	state.update(artifact("b", "second"), false)
	replacement := artifact("a", "correct")
	state.update(replacement, false)
	state.update(artifact("a", "tail"), true)
	if got := state.String(); got != "correct\ntail\nsecond" {
		t.Fatalf("replacement/append order: %q", got)
	}
	if first.Parts[0].Text() != "obsolete" || replacement.Parts[0].Text() != "correct" || len(replacement.Parts) != 1 {
		t.Fatal("SDK artifacts were mutated")
	}
	// Mutating an SDK object later cannot mutate the recorded state either.
	replacement.Parts[0] = a2a.NewTextPart("changed later")
	if state.String() != "correct\ntail\nsecond" {
		t.Fatal("retained mutable SDK state")
	}
	state.update(&a2a.Artifact{ID: "a", Parts: a2a.ContentParts{a2a.NewFileURLPart("https://example.test/?token=private", "text/plain")}}, false)
	if state.String() != "second" {
		t.Fatalf("non-text replacement retained obsolete text: %q", state.String())
	}
	state.update(artifact("b", ""), false)
	if state.String() != "" {
		t.Fatal("empty replacement retained old text")
	}
	state.update(artifact("new", "append creates an artifact"), true)
	if state.String() != "append creates an artifact" {
		t.Fatal("first append was lost")
	}
}

func TestArtifactStateBoundsAndRecovery(t *testing.T) {
	var state artifactState
	long := strings.Repeat("é", maxContentLen)
	state.update(artifact("a", long), false)
	state.update(artifact("b", "later artifact"), false)
	state.update(artifact("a", "ignored tail"), true)
	if got := truncate(state.String()); len(got) > maxContentLen || !utf8.ValidString(got) || !strings.HasSuffix(got, truncatedSuffix) {
		t.Fatal("invalid bounded text")
	}
	for _, entry := range state.entries {
		if len(entry.text) > maxContentLen {
			t.Fatal("unbounded artifact")
		}
	}
	// The later artifact must still be available after replacing the large one.
	state.update(artifact("a", "short"), false)
	if state.String() != "short\nlater artifact" {
		t.Fatal("replacement failed to recover later artifact")
	}
	state.Reset()
	for i := range maxTrackedArtifacts + 10 {
		state.update(artifact(fmt.Sprint(i), "x"), false)
	}
	if len(state.index) != maxTrackedArtifacts || len(state.entries) != maxTrackedArtifacts || !strings.HasSuffix(state.String(), truncatedSuffix) {
		t.Fatal("artifact count is unbounded or omission unmarked")
	}
	state.update(artifact("0", "replacement at capacity"), false)
	if !strings.HasPrefix(state.String(), "replacement at capacity") {
		t.Fatal("capacity blocked an existing ID's replacement")
	}
	state.Reset()
	if state.String() != "" || state.omitted || len(state.index) != 0 {
		t.Fatal("snapshot reset retained old state")
	}
	state.update(artifact(strings.Repeat("huge ID", maxContentLen), "safe"), false)
	if state.String() != "safe" || len(state.index) != 1 {
		t.Fatal("large ID broke bounded lookup")
	}
}

func TestArtifactSnapshotResetsStream(t *testing.T) {
	for _, hidden := range []bool{false, true} {
		t.Run(fmt.Sprint(hidden), func(t *testing.T) {
			rec := tracetest.NewSpanRecorder()
			tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
			t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
			tracing := NewInterceptor(WithTracerProvider(tp), WithTraceConfig(TraceConfig{HideOutputs: hidden}))
			ctx, _, _ := tracing.Before(context.Background(), &a2aclient.Request{Method: "SendStreamingMessage"})
			old := &a2a.TaskArtifactUpdateEvent{Artifact: artifact("old", "stale output")}
			_ = tracing.After(ctx, &a2aclient.Response{Method: "SendStreamingMessage", Payload: old})
			snapshot := &a2a.Task{ID: "task", Artifacts: []*a2a.Artifact{artifact("a", "snapshot")}, Status: a2a.TaskStatus{State: a2a.TaskStateWorking}}
			_ = tracing.After(ctx, &a2aclient.Response{Method: "SendStreamingMessage", Payload: snapshot})
			_ = tracing.After(ctx, &a2aclient.Response{Method: "SendStreamingMessage", Payload: &a2a.TaskArtifactUpdateEvent{Artifact: artifact("a", "tail"), Append: true}})
			_ = tracing.After(ctx, &a2aclient.Response{Method: "SendStreamingMessage", Payload: &a2a.TaskStatusUpdateEvent{Status: a2a.TaskStatus{State: a2a.TaskStateCompleted}}})
			want := "snapshot\ntail"
			if hidden {
				want = RedactedValue
			}
			spans := rec.Ended()
			if len(spans) != 1 {
				t.Fatalf("want one ended span, got %d", len(spans))
			}
			got := ""
			for _, kv := range spans[0].Attributes() {
				if kv.Key == attribute.Key(OutputValue) {
					got = kv.Value.AsString()
				}
			}
			if got != want {
				t.Fatalf("snapshot output %q, want %q", got, want)
			}
			if !reflect.DeepEqual(snapshot.Artifacts, []*a2a.Artifact{artifact("a", "snapshot")}) {
				t.Fatal("snapshot was mutated")
			}
		})
	}
}
