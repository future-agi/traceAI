package traceaia2a_test

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	a2agrpc "github.com/a2aproject/a2a-go/v2/a2agrpc/v1"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func serverClient(t *testing.T, transport string, executor *agent, tp *sdktrace.TracerProvider, tc traceaia2a.TraceConfig, serverOpts ...a2asrv.RequestHandlerOption) *traceaia2a.Client {
	t.Helper()
	serverOpts = append([]a2asrv.RequestHandlerOption{a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(
		traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(tc)))}, serverOpts...)
	handler := traceaia2a.WrapHandler(a2asrv.NewHandler(executor, serverOpts...))
	var c *a2a.AgentCard
	opts := []a2aclient.FactoryOption{a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(
		traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(tc)))}
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
		srv := httptest.NewServer(h)
		t.Cleanup(srv.Close)
		c = card(srv.URL, transport == "rest", true)
	}
	client, err := a2aclient.NewFromCard(context.Background(), c, opts...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Destroy() })
	return traceaia2a.WrapClient(client)
}

func TestServerTraceContinuesClient(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		for _, streaming := range []bool{false, true} {
			method := "SendMessage"
			if streaming {
				method = "SendStreamingMessage"
			}
			t.Run(transport+"/"+method, func(t *testing.T) {
				rec := tracetest.NewSpanRecorder()
				tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
				t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
				exec := completes()
				inner := exec.execute
				exec.execute = func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
					_, span := tp.Tracer("test-executor").Start(ctx, "executor")
					span.End()
					inner(ctx, ec, yield)
				}
				client := serverClient(t, transport, exec, tp, traceaia2a.TraceConfig{})
				ctx, root := tp.Tracer("test").Start(context.Background(), "root")
				if streaming {
					for _, err := range client.SendStreamingMessage(ctx, userMessage("hello "+marker)) {
						if err != nil {
							t.Fatal(err)
						}
					}
				} else if _, err := client.SendMessage(ctx, userMessage("hello "+marker)); err != nil {
					t.Fatal(err)
				}
				root.End()
				spans := rec.Ended()
				if len(spans) != 4 {
					t.Fatalf("want root + client + server + executor (4 spans), got %d", len(spans))
				}
				var clientSpan, serverSpan, child sdktrace.ReadOnlySpan
				for _, s := range spans {
					switch s.SpanKind() {
					case trace.SpanKindClient:
						clientSpan = s
					case trace.SpanKindServer:
						serverSpan = s
					}
					if s.Name() == "executor" {
						child = s
					}
				}
				if clientSpan == nil || serverSpan == nil || child == nil {
					t.Fatal("missing client, server or executor span")
				}
				if clientSpan.Parent().SpanID() != root.SpanContext().SpanID() || serverSpan.Parent().SpanID() != clientSpan.SpanContext().SpanID() || child.Parent().SpanID() != serverSpan.SpanContext().SpanID() {
					t.Fatal("broken root -> client -> server -> executor parent chain")
				}
				if !serverSpan.Parent().IsRemote() || serverSpan.SpanContext().TraceID() != root.SpanContext().TraceID() {
					t.Fatal("server did not extract remote trace context")
				}
				if serverSpan.Name() != "a2a."+method || serverSpan.Status().Code != codes.Ok {
					t.Fatalf("server name/status: %s %v", serverSpan.Name(), serverSpan.Status())
				}
				a := attrs(serverSpan)
				if str(a, traceaia2a.InputValue) != "hello "+marker || str(a, traceaia2a.GenAISpanKind) != "AGENT" || str(a, traceaia2a.A2ATaskState) != string(a2a.TaskStateCompleted) {
					t.Fatalf("server attributes: %v", a)
				}
				if streaming && len(serverSpan.Events()) != 4 {
					t.Fatalf("want 4 stream events, got %d", len(serverSpan.Events()))
				}
			})
		}
	}
}
