package traceaia2a

import (
	"context"
	"errors"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestUnaryCompletionOwnsOnlyItsCall(t *testing.T) {
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	tracing := NewInterceptor(WithTracerProvider(tp), WithTraceConfig(TraceConfig{}))
	message := a2a.NewMessage(a2a.MessageRoleAgent, a2a.NewTextPart("accepted"))
	result, err := observeUnary(nil, "SendMessage", func(ctx context.Context) (*a2a.Message, error) {
		ctx, _, _ = tracing.Before(ctx, &a2aclient.Request{Method: "SendMessage"})
		outer := ctx.Value(stateKey{}).(*callState)
		if !outer.unaryCompletion {
			t.Fatal("outer call did not claim completion")
		}
		// An unwrapped nested call must not steal its parent's completion slot.
		inner, _, _ := tracing.Before(ctx, &a2aclient.Request{Method: "SendMessage"})
		if inner.Value(stateKey{}).(*callState).unaryCompletion {
			t.Fatal("nested call stole completion")
		}
		_ = tracing.After(inner, &a2aclient.Response{Method: "SendMessage", Payload: message})
		// A wrapped nested call owns a fresh slot and completes independently.
		_, err := observeUnary(ctx, "SendMessage", func(ctx context.Context) (*a2a.Message, error) {
			ctx, _, _ = tracing.Before(ctx, &a2aclient.Request{Method: "SendMessage"})
			if ctx.Value(stateKey{}).(*callState) == outer {
				t.Fatal("nested call reused outer state")
			}
			_ = tracing.After(ctx, &a2aclient.Response{Method: "SendMessage", Payload: message})
			return message, nil
		})
		if err != nil {
			t.Fatal(err)
		}
		_ = tracing.After(ctx, &a2aclient.Response{Method: "SendMessage", Payload: message})
		if len(rec.Ended()) != 2 || outer.ended {
			t.Fatal("outer completion was not deferred")
		}
		return message, nil
	})
	if err != nil || result != message || len(rec.Started()) != 3 || len(rec.Ended()) != 3 {
		t.Fatal("lost result or completion")
	}
}

func TestUnaryCompletionWithoutInterceptor(t *testing.T) {
	rejection := errors.New("synthetic policy rejection")
	result, err := observeUnary(context.Background(), "GetTask", func(context.Context) (*a2a.Task, error) { return nil, rejection })
	if result != nil || err != rejection {
		t.Fatal("untraced API outcome changed")
	}
}
