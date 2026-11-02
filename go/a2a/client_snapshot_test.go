package traceaia2a_test

import (
	"context"
	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"github.com/a2aproject/a2a-go/v2/a2asrv/taskstore"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"net/http/httptest"
	"testing"
	"time"
)

// Client-only coverage against an uninstrumented official server.
func TestClientAuthSnapshotEOF(t *testing.T) {
	store := taskstore.NewInMemory(nil)
	task := &a2a.Task{ID: "persisted-auth-task", ContextID: "persisted-context", Status: a2a.TaskStatus{State: a2a.TaskStateAuthRequired}}
	if _, err := store.Create(context.Background(), task); err != nil {
		t.Fatal(err)
	}
	handler := a2asrv.NewHandler(repliesWithMessage(), a2asrv.WithTaskStore(store))
	server := httptest.NewServer(a2asrv.NewJSONRPCHandler(handler))
	defer server.Close()
	client, rec, _ := newClient(t, card(server.URL, false, true))
	defer client.Destroy()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	count := 0
	for _, err := range traceaia2a.WrapClient(client).SubscribeToTask(ctx, &a2a.SubscribeToTaskRequest{ID: task.ID}) {
		if err != nil {
			t.Fatal(err)
		}
		count++
	}
	if count != 1 || ctx.Err() != nil {
		t.Fatalf("not a clean EOF: events=%d ctx=%v", count, ctx.Err())
	}
	t.Logf("clean subscription EOF: started=%d ended=%d", len(rec.Started()), len(rec.Ended()))
	if len(rec.Ended()) != 1 {
		t.Fatalf("successful subscription left client span unfinished")
	}
	if rec.Ended()[0].Status().Code != codes.Ok {
		t.Fatalf("incorrect completion: %v", rec.Ended()[0].Status())
	}
}
