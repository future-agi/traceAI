package traceaia2a_test

import (
	"context"
	"net"
	"sync"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	a2agrpc "github.com/a2aproject/a2a-go/v2/a2agrpc/v1"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	traceaia2a "github.com/future-agi/traceAI/go/a2a"
)

// On gRPC a2a-go sends service parameters as call metadata instead of HTTP
// headers; the interceptor is the same.
func TestGRPCTransportCarriesTraceparentAsMetadata(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	exec := completes()
	inner := exec.execute
	exec.execute = func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
		if v, ok := ec.ServiceParams.Get("traceparent"); ok {
			mu.Lock()
			seen = append(seen, v...)
			mu.Unlock()
		}
		if _, ok := ec.ServiceParams.Get("x-api-key"); ok {
			t.Error("gRPC metadata carries x-api-key")
		}
		inner(ctx, ec, yield)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	gs := grpc.NewServer()
	a2agrpc.NewHandler(a2asrv.NewHandler(exec)).RegisterWith(gs)
	go func() { _ = gs.Serve(ln) }()
	t.Cleanup(gs.Stop)

	c := &a2a.AgentCard{
		Name:                "gRPC Agent",
		SupportedInterfaces: []*a2a.AgentInterface{a2a.NewAgentInterface(ln.Addr().String(), a2a.TransportProtocolGRPC)},
		Capabilities:        a2a.AgentCapabilities{Streaming: true},
	}
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	client, err := a2aclient.NewFromCard(context.Background(), c,
		a2agrpc.WithGRPCTransport(grpc.WithTransportCredentials(insecure.NewCredentials())),
		a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(
			traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{}))))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Destroy() })

	if _, err := client.SendMessage(context.Background(), userMessage("hi over grpc")); err != nil {
		t.Fatal(err)
	}
	for _, err := range client.SendStreamingMessage(context.Background(), userMessage("stream over grpc")) {
		if err != nil {
			t.Fatal(err)
		}
	}

	spans := rec.Ended()
	if len(spans) != 2 {
		t.Fatalf("want 2 spans, got %d", len(spans))
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 {
		t.Fatalf("the agent saw %d traceparent values, want 2", len(seen))
	}
	for i, s := range spans {
		want := "00-" + s.SpanContext().TraceID().String() + "-" + s.SpanContext().SpanID().String() + "-01"
		if seen[i] != want {
			t.Errorf("call %d: agent saw traceparent %q, want %q", i, seen[i], want)
		}
		a := attrs(s)
		if str(a, traceaia2a.A2ATaskState) != "TASK_STATE_COMPLETED" || str(a, traceaia2a.A2AAgentURL) != ln.Addr().String() || s.Status().Code != codes.Ok {
			t.Errorf("span %s: status %+v attrs %v", s.Name(), s.Status(), a)
		}
	}
}
