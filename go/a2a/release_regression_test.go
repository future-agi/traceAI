package traceaia2a_test

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	a2agrpc "github.com/a2aproject/a2a-go/v2/a2agrpc/v1"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func qualificationClient(t *testing.T, transport string, exec *agent, tp *sdktrace.TracerProvider, tc traceaia2a.TraceConfig, tracingFirst bool, policies ...a2aclient.CallInterceptor) *traceaia2a.Client {
	t.Helper()
	handler := traceaia2a.WrapHandler(a2asrv.NewHandler(exec, a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(tc)))))
	var cardValue *a2a.AgentCard
	var opts []a2aclient.FactoryOption
	tracing := traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(tc))
	interceptors := append([]a2aclient.CallInterceptor(nil), policies...)
	if tracingFirst {
		interceptors = append([]a2aclient.CallInterceptor{tracing}, interceptors...)
	} else {
		interceptors = append(interceptors, tracing)
	}
	opts = append(opts, a2aclient.WithCallInterceptors(interceptors...))
	if transport == "grpc" {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		gs := grpc.NewServer()
		a2agrpc.NewHandler(handler).RegisterWith(gs)
		go func() { _ = gs.Serve(ln) }()
		t.Cleanup(gs.Stop)
		cardValue = &a2a.AgentCard{SupportedInterfaces: []*a2a.AgentInterface{a2a.NewAgentInterface(ln.Addr().String(), a2a.TransportProtocolGRPC)}, Capabilities: a2a.AgentCapabilities{Streaming: true}}
		opts = append(opts, a2agrpc.WithGRPCTransport(grpc.WithTransportCredentials(insecure.NewCredentials())))
	} else {
		var h http.Handler = a2asrv.NewJSONRPCHandler(handler)
		if transport == "rest" {
			h = a2asrv.NewRESTHandler(handler)
		}
		server := httptest.NewServer(h)
		t.Cleanup(server.Close)
		cardValue = card(server.URL, transport == "rest", true)
	}
	client, err := traceaia2a.NewClientFromCard(context.Background(), cardValue, opts...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Destroy() })
	return client
}

type qualificationReject struct {
	a2aclient.PassthroughInterceptor
	err error
}

func (r qualificationReject) After(ctx context.Context, resp *a2aclient.Response) error { return r.err }

func TestQualificationUnaryClientRejectionEnds(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			rejection := a2a.NewError(a2a.ErrUnauthorized, "synthetic-policy-rejection")
			client := qualificationClient(t, transport, completes(), tp, traceaia2a.TraceConfig{}, true, qualificationReject{err: rejection})
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			_, err := client.SendMessage(ctx, userMessage("hello"))
			if !errors.Is(err, rejection) {
				t.Fatalf("API error changed: %v", err)
			}
			cancel()
			clients := 0
			for _, s := range rec.Ended() {
				if s.SpanKind() == trace.SpanKindClient {
					clients++
					if s.Status().Code != codes.Error {
						t.Errorf("policy rejection not ERROR: %v", s.Status())
					}
				}
			}
			if clients != 1 {
				t.Fatalf("completed rejected unary call leaked client span: started=%d ended=%d client-ended=%d", len(rec.Started()), len(rec.Ended()), clients)
			}
		})
	}
}

func TestQualificationArtifactReplacement(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			exec := &agent{execute: func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
				first := a2a.NewArtifactEvent(ec, a2a.NewTextPart("obsolete-answer"))
				replacement := a2a.NewArtifactEvent(ec, a2a.NewTextPart("correct-answer"))
				replacement.Artifact.ID = first.Artifact.ID
				if !yield(a2a.NewSubmittedTask(ec, ec.Message), nil) {
					return
				}
				if !yield(first, nil) {
					return
				}
				if !yield(replacement, nil) {
					return
				}
				yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, nil), nil)
			}}
			client := qualificationClient(t, transport, exec, tp, traceaia2a.TraceConfig{}, true)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			var taskID a2a.TaskID
			for ev, err := range client.SendStreamingMessage(ctx, userMessage("replace an artifact")) {
				if err != nil {
					t.Fatal(err)
				}
				if task, ok := ev.(*a2a.Task); ok {
					taskID = task.ID
				}
			}
			task, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: taskID})
			if err != nil {
				t.Fatal(err)
			}
			if len(task.Artifacts) != 1 || task.Artifacts[0].Parts[0].Text() != "correct-answer" {
				t.Fatalf("invalid fixture: actual stored task=%+v", task)
			}
			count := 0
			for _, s := range rec.Ended() {
				if s.Name() != "a2a.SendStreamingMessage" {
					continue
				}
				count++
				got := ""
				for _, a := range s.Attributes() {
					if a.Key == attribute.Key(traceaia2a.OutputValue) {
						got = a.Value.AsString()
					}
				}
				if got != "correct-answer" {
					t.Errorf("%s output retained superseded artifact: got %q; actual task output %q", s.SpanKind(), got, "correct-answer")
				}
			}
			if count != 2 {
				t.Fatalf("expected two stream spans, got %d", count)
			}
		})
	}
}
