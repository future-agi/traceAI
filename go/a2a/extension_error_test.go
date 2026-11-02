package traceaia2a_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
)

func TestServerRequiredExtensionProtocolError(t *testing.T) {
	for _, transport := range []string{"jsonrpc", "rest", "grpc"} {
		t.Run(transport, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			client := serverClient(t, transport, completes(), tp, traceaia2a.TraceConfig{},
				a2asrv.WithCapabilityChecks(&a2a.AgentCapabilities{Extensions: []a2a.AgentExtension{{URI: "https://example.invalid/required-extension", Required: true}}}))
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_, err := client.GetTask(ctx, &a2a.GetTaskRequest{ID: "synthetic-task"})
			if !errors.Is(err, a2a.ErrExtensionSupportRequired) {
				t.Fatalf("unexpected wire response: %v", err)
			}
			waitEnded(t, rec, 2)
			if len(rec.Ended()) != 2 || len(serverSpans(rec)) != 1 {
				t.Fatalf("want one client/server pair, got %d spans", len(rec.Ended()))
			}
			for _, span := range rec.Ended() {
				if str(attrs(span), traceaia2a.ErrorType) != "EXTENSION_SUPPORT_REQUIRED" || span.Status().Code != codes.Error {
					t.Errorf("%s span must record the protocol rejection, not context cancellation: %v %v", span.SpanKind(), attrs(span), span.Status())
				}
			}
		})
	}
}

// With v2.5/v2.6 the SDK skipped After on required-extension rejections, leaving
// these completed unary calls open forever when the context was not canceled.
func TestServerRequiredExtensionEndsUnarySpan(t *testing.T) {
	for _, method := range []string{"SendMessage", "GetTask", "CancelTask"} {
		t.Run(method, func(t *testing.T) {
			tp, rec := serverRecorder(t)
			h := a2asrv.NewHandler(completes(),
				a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))),
				a2asrv.WithCapabilityChecks(&a2a.AgentCapabilities{Extensions: []a2a.AgentExtension{{URI: "https://example.invalid/required-extension", Required: true}}}))
			ctx := context.Background()
			var err error
			switch method {
			case "SendMessage":
				_, err = h.SendMessage(ctx, userMessage("start task"))
			case "GetTask":
				_, err = h.GetTask(ctx, &a2a.GetTaskRequest{ID: "synthetic-task"})
			case "CancelTask":
				_, err = h.CancelTask(ctx, &a2a.CancelTaskRequest{ID: "synthetic-task"})
			}
			if !errors.Is(err, a2a.ErrExtensionSupportRequired) {
				t.Fatalf("unexpected handler result: %v", err)
			}
			if len(rec.Started()) != 1 || len(rec.Ended()) != 1 {
				t.Fatalf("completed unary call left an open span: started=%d ended=%d", len(rec.Started()), len(rec.Ended()))
			}
			span := rec.Ended()[0]
			if span.Name() != "a2a."+method || str(attrs(span), traceaia2a.ErrorType) != "EXTENSION_SUPPORT_REQUIRED" || span.Status().Code != codes.Error {
				t.Fatalf("incorrect rejection span: %s %v %v", span.Name(), attrs(span), span.Status())
			}
		})
	}
}
