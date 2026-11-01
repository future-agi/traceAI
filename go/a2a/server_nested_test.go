package traceaia2a_test

import (
	"context"
	"errors"
	"iter"
	"strings"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// The SDK reuses and mutates CallContext for nested in-process calls. Completion
// must follow each call's context-local state, including calls we do not trace.
func TestServerNestedInProcessCalls(t *testing.T) {
	for _, transport := range []string{"direct", "jsonrpc", "rest", "grpc"} {
		for _, method := range []string{"SendMessage", "SendStreamingMessage"} {
			for _, nested := range []string{"traced", "uninstrumented", "untraced-error", "untraced-success", "same-method"} {
				for _, privacy := range []string{"visible", "redacted"} {
					t.Run(transport+"/"+method+"/"+nested+"/"+privacy, func(t *testing.T) {
						tp, rec := serverRecorder(t)
						tc := traceaia2a.TraceConfig{HideInputs: privacy == "redacted", HideOutputs: privacy == "redacted"}
						interceptor := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(tc))
						var opts []a2asrv.RequestHandlerOption
						if nested != "uninstrumented" {
							opts = append(opts, a2asrv.WithCallInterceptors(interceptor))
						}
						if nested == "untraced-success" {
							opts = append(opts, a2asrv.WithExtendedAgentCard(&a2a.AgentCard{Name: "nested " + marker}))
						}
						inner := a2asrv.NewHandler(repliesWithMessage(), opts...)
						exec := repliesWithMessage()
						reply := exec.execute
						exec.execute = func(ctx context.Context, ec *a2asrv.ExecutorContext, yield func(a2a.Event, error) bool) {
							var err, wantErr error
							switch nested {
							case "untraced-error":
								_, err = inner.GetExtendedAgentCard(ctx, &a2a.GetExtendedAgentCardRequest{})
								wantErr = a2a.ErrExtendedCardNotConfigured
							case "untraced-success":
								_, err = inner.GetExtendedAgentCard(ctx, &a2a.GetExtendedAgentCardRequest{})
							case "same-method":
								_, err = inner.SendMessage(ctx, userMessage("nested "+marker))
							default:
								_, err = inner.GetTask(ctx, &a2a.GetTaskRequest{ID: "synthetic-missing"})
								wantErr = a2a.ErrTaskNotFound
							}
							if !errors.Is(err, wantErr) {
								t.Errorf("nested call error = %v, want %v", err, wantErr)
							}
							if !trace.SpanFromContext(ctx).IsRecording() {
								t.Error("nested call ended the outer server span before its response")
							}
							reply(ctx, ec, yield)
						}

						var send func(context.Context, *a2a.SendMessageRequest) (a2a.SendMessageResult, error)
						var stream func(context.Context, *a2a.SendMessageRequest) iter.Seq2[a2a.Event, error]
						wantSpans := 1
						if transport == "direct" {
							handler := a2asrv.NewHandler(exec, a2asrv.WithCallInterceptors(interceptor))
							send, stream = handler.SendMessage, handler.SendStreamingMessage
						} else {
							client := serverClient(t, transport, exec, tp, tc)
							send, stream = client.SendMessage, client.SendStreamingMessage
							wantSpans++
						}
						if nested == "traced" || nested == "same-method" {
							wantSpans++
						}
						// No cancellation: a completed direct call must end its own span.
						ctx := context.Background()
						req := userMessage("outer " + marker)
						if method == "SendStreamingMessage" {
							events := 0
							for event, err := range stream(ctx, req) {
								if err != nil {
									t.Fatal(err)
								}
								if _, ok := event.(*a2a.Message); !ok {
									t.Fatalf("unexpected event %T", event)
								}
								events++
							}
							if events != 1 {
								t.Fatalf("want one message event, got %d", events)
							}
						} else {
							result, err := send(ctx, req)
							if err != nil {
								t.Fatal(err)
							}
							if _, ok := result.(*a2a.Message); !ok {
								t.Fatalf("unexpected result %T", result)
							}
						}
						if transport != "direct" {
							waitEnded(t, rec, wantSpans)
						}
						if len(rec.Started()) != wantSpans || len(rec.Ended()) != wantSpans {
							t.Fatalf("want %d finished spans: started=%d ended=%d", wantSpans, len(rec.Started()), len(rec.Ended()))
						}
						var outer, child sdktrace.ReadOnlySpan
						for _, span := range serverSpans(rec) {
							// Nested spans finish before their parent.
							child, outer = outer, span
						}
						if outer == nil || outer.Name() != "a2a."+method || outer.Status().Code != codes.Ok {
							t.Fatalf("successful outer call has incorrect span: %v", outer)
						}
						if child != nil {
							if child.Parent().SpanID() != outer.SpanContext().SpanID() || child.SpanContext().TraceID() != outer.SpanContext().TraceID() || child.Parent().IsRemote() {
								t.Error("nested server span lost its local parent")
							}
							if nested == "traced" && (child.Name() != "a2a.GetTask" || child.Status().Code != codes.Error || str(attrs(child), traceaia2a.ErrorType) != "TASK_NOT_FOUND") {
								t.Error("nested error was not recorded on its own span")
							}
						}
						input, output := "outer "+marker, "hello outer "+marker
						if privacy == "redacted" {
							input, output = "__REDACTED__", "__REDACTED__"
							for _, span := range rec.Ended() {
								if strings.Contains(everything(span), marker) {
									t.Error("nested tracing leaked redacted content")
								}
							}
						}
						a := attrs(outer)
						if str(a, traceaia2a.InputValue) != input || str(a, traceaia2a.OutputValue) != output || has(a, traceaia2a.ErrorType) {
							t.Errorf("outer response was lost or replaced by nested response: %v", a)
						}
					})
				}
			}
		}
	}
}
