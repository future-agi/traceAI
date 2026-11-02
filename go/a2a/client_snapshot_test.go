package traceaia2a_test

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	a2agrpc "github.com/a2aproject/a2a-go/v2/a2agrpc/v1"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"github.com/a2aproject/a2a-go/v2/a2asrv/taskstore"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// Client-only coverage against an uninstrumented official server. Existing
// interceptor-only clients must retain successful auth snapshot completion
// without adopting WrapClient; wrapped clients also observe actual clean EOF.
func TestClientAuthSnapshotEOF(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, mode := range []string{"interceptor-only", "wrapped"} {
			t.Run(transport+"/"+mode, func(t *testing.T) {
				tp, rec := serverRecorder(t)
				store := taskstore.NewInMemory(nil)
				task := &a2a.Task{ID: "persisted-auth-task", ContextID: "persisted-context", Status: a2a.TaskStatus{
					State:   a2a.TaskStateAuthRequired,
					Message: a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("authorize this task")),
				}}
				if _, err := store.Create(context.Background(), task); err != nil {
					t.Fatal(err)
				}
				handler := a2asrv.NewHandler(repliesWithMessage(), a2asrv.WithTaskStore(store))
				opts := []a2aclient.FactoryOption{a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(
					traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{})))}
				var c *a2a.AgentCard
				if transport == "grpc" {
					ln, err := net.Listen("tcp", "127.0.0.1:0")
					if err != nil {
						t.Fatal(err)
					}
					gs := grpc.NewServer()
					a2agrpc.NewHandler(handler).RegisterWith(gs)
					go func() { _ = gs.Serve(ln) }()
					t.Cleanup(gs.Stop)
					c = &a2a.AgentCard{SupportedInterfaces: []*a2a.AgentInterface{a2a.NewAgentInterface(ln.Addr().String(), a2a.TransportProtocolGRPC)}, Capabilities: a2a.AgentCapabilities{Streaming: true}}
					opts = append(opts, a2agrpc.WithGRPCTransport(grpc.WithTransportCredentials(insecure.NewCredentials())))
				} else {
					var h http.Handler = a2asrv.NewJSONRPCHandler(handler)
					if transport == "rest" {
						h = a2asrv.NewRESTHandler(handler)
					}
					server := httptest.NewServer(h)
					t.Cleanup(server.Close)
					c = card(server.URL, transport == "rest", true)
				}
				client, err := a2aclient.NewFromCard(context.Background(), c, opts...)
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = client.Destroy() })
				subscribe := client.SubscribeToTask
				if mode == "wrapped" {
					subscribe = traceaia2a.WrapClient(client).SubscribeToTask
				}
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				count := 0
				for event, err := range subscribe(ctx, &a2a.SubscribeToTaskRequest{ID: task.ID}) {
					if err != nil {
						t.Fatal(err)
					}
					if got, ok := event.(*a2a.Task); !ok || got.ID != task.ID || got.Status.State != a2a.TaskStateAuthRequired {
						t.Fatalf("unexpected snapshot: %v", event)
					}
					count++
				}
				if count != 1 || ctx.Err() != nil || len(rec.Started()) != 1 {
					t.Fatalf("not a clean single-span EOF: events=%d ctx=%v started=%d", count, ctx.Err(), len(rec.Started()))
				}
				if len(rec.Ended()) != 1 {
					t.Error("successful subscription left client span unfinished at EOF")
				}
				cancel()
				waitEnded(t, rec, 1)
				span := rec.Ended()[0]
				if span.Status().Code != codes.Ok {
					t.Errorf("successful call falsely recorded as %v after caller cancellation", span.Status())
				}
				if str(attrs(span), traceaia2a.A2ATaskState) != string(a2a.TaskStateAuthRequired) ||
					str(attrs(span), traceaia2a.OutputValue) != "authorize this task" || len(span.Events()) != 1 {
					t.Fatalf("lost snapshot state, output or event: %v", attrs(span))
				}
			})
		}
	}
}
