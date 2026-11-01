package traceailambda

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-lambda-go/lambdacontext"
	"go.opentelemetry.io/contrib/instrumentation/github.com/aws/aws-lambda-go/otellambda"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const httpPayload = `{"version":"2.0","requestContext":{"http":{"method":"POST"}},"headers":{"traceparent":"00-0123456789abcdef0123456789abcdef-0123456789abcdef-01","authorization":"synthetic-secret","baggage":"customer=synthetic-secret","tracestate":"vendor=synthetic-secret"},"body":"synthetic-secret"}`

type handlerFunc func(context.Context, []byte) ([]byte, error)

func (h handlerFunc) Invoke(ctx context.Context, payload []byte) ([]byte, error) {
	return h(ctx, payload)
}

func invocationContext() context.Context {
	return lambdacontext.NewContext(context.Background(), &lambdacontext.LambdaContext{
		AwsRequestID: "synthetic-request", InvokedFunctionArn: "arn:aws:lambda:us-east-1:123456789012:function:example",
	})
}

func recorderProvider(t *testing.T) (*sdktrace.TracerProvider, *tracetest.SpanRecorder) {
	t.Helper()
	recorder := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return tp, recorder
}

func attributeValue(s sdktrace.ReadOnlySpan, name string) attribute.Value {
	for _, attr := range s.Attributes() {
		if string(attr.Key) == name {
			return attr.Value
		}
	}
	return attribute.Value{}
}

func TestWarmReuseAndNativeSpanOwnership(t *testing.T) {
	tp, recorder := recorderProvider(t)
	provider := &flushProvider{TracerProvider: tp}
	calls := 0
	h := WrapHandler(handlerFunc(func(ctx context.Context, payload []byte) ([]byte, error) {
		calls++
		if string(payload) != httpPayload {
			t.Fatal("application event changed")
		}
		_, child := tp.Tracer("app").Start(ctx, "lookup_order")
		child.End()
		return []byte(`{"result":"synthetic-secret"}`), nil
	}), provider)
	for i := range 2 {
		output, err := h.Invoke(invocationContext(), []byte(httpPayload))
		if err != nil || string(output) != `{"result":"synthetic-secret"}` {
			t.Fatalf("changed application result: %s %v", output, err)
		}
		spans := recorder.Ended()
		if len(spans) != 2*(i+1) {
			t.Fatalf("duplicate or missing spans: %d", len(spans))
		}
		child, root := spans[i*2], spans[i*2+1]
		if root.Name() != "lambda.handler" || root.SpanKind() != trace.SpanKindServer || root.InstrumentationScope().Name != otellambda.ScopeName {
			t.Fatalf("not the maintained native span: %s %v %s", root.Name(), root.SpanKind(), root.InstrumentationScope().Name)
		}
		if child.Parent().SpanID() != root.SpanContext().SpanID() || root.Parent().SpanID().String() != "0123456789abcdef" || !root.Parent().IsRemote() {
			t.Fatal("invocation / child parent linkage lost")
		}
		if root.SpanContext().TraceState().Len() != 0 {
			t.Fatal("tracestate was propagated")
		}
		if attributeValue(root, "faas.coldstart").AsBool() != (i == 0) {
			t.Fatal("warm/cold marker wrong")
		}
		if attributeValue(root, "faas.trigger").AsString() != "http" || attributeValue(root, "fi.span.kind").AsString() != "CHAIN" || attributeValue(root, "faas.invocation_id").AsString() != "synthetic-request" {
			t.Fatal("invocation metadata lost")
		}
		if root.Status().Code != codes.Unset {
			t.Fatal("successful invocation marked failed")
		}
		if strings.Contains(fmt.Sprint(root.Attributes(), root.Events()), "synthetic-secret") {
			t.Fatal("captured application content or headers")
		}
	}
	if calls != 2 || provider.calls != 2 {
		t.Fatalf("handler=%d flush=%d", calls, provider.calls)
	}
}

func TestFailuresPreserveApplicationSemantics(t *testing.T) {
	for _, mode := range []string{"error", "panic", "panic-nil"} {
		t.Run(mode, func(t *testing.T) {
			tp, recorder := recorderProvider(t)
			provider := &flushProvider{TracerProvider: tp}
			failure := errors.New("synthetic-secret")
			h := WrapHandler(handlerFunc(func(context.Context, []byte) ([]byte, error) {
				if mode == "panic" {
					panic(failure)
				}
				if mode == "panic-nil" {
					panic(nil)
				}
				return nil, failure
			}), provider)
			var recovered any
			func() {
				defer func() { recovered = recover() }()
				_, err := h.Invoke(invocationContext(), []byte(httpPayload))
				if mode != "error" {
					t.Error("panic swallowed")
				}
				if err != failure {
					t.Fatal("error identity changed")
				}
			}()
			if mode == "panic" && recovered != failure {
				t.Fatal("panic identity changed")
			}
			if mode == "panic-nil" && recovered == nil {
				t.Fatal("nil panic swallowed")
			}
			spans := recorder.Ended()
			if len(spans) != 1 || provider.calls != 1 {
				t.Fatalf("span/flush not completed: %d/%d", len(spans), provider.calls)
			}
			root := spans[0]
			if root.Status().Code != codes.Error || attributeValue(root, "error.type").AsString() == "" {
				t.Fatal("failed invocation lacks error status/type")
			}
			if strings.Contains(fmt.Sprint(root.Status(), root.Attributes(), root.Events()), "synthetic-secret") || len(root.Events()) != 0 {
				t.Fatal("captured error content or exception event")
			}
		})
	}
}

func TestParentExtractionBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name, payload string
		remote        bool
	}{
		{"http-v2", httpPayload, true},
		{"case-insensitive", strings.Replace(httpPayload, "traceparent", "Traceparent", 1), true},
		{"invalid-parent", strings.Replace(httpPayload, "0123456789abcdef0123456789abcdef", "00000000000000000000000000000000", 1), false},
		{"rest-v1", strings.Replace(httpPayload, `"2.0"`, `"1.0"`, 1), false},
		{"direct", `{}`, false},
		{"sqs", `{"Records":[{"messageAttributes":{"traceparent":{"stringValue":"00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}}}]}`, false},
		{"xray-only", `{"version":"2.0","headers":{"x-amzn-trace-id":"Root=1-abcdef01-abcdef0123456789abcdef01"}}`, false},
		{"malformed-event", `{`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tp, recorder := recorderProvider(t)
			h := WrapHandler(lambda.NewHandler(func(context.Context) error { return nil }), tp)
			if _, err := h.Invoke(invocationContext(), []byte(tc.payload)); err != nil {
				t.Fatal(err)
			}
			root := recorder.Ended()[0]
			if root.Parent().IsRemote() != tc.remote {
				t.Fatalf("unexpected parent: %v", root.Parent())
			}
			if !tc.remote && root.Parent().IsValid() {
				t.Fatal("unsupported event continued a trace")
			}
		})
	}
}

type flushProvider struct {
	*sdktrace.TracerProvider
	calls int
	probe func(context.Context) error
}

func (p *flushProvider) ForceFlush(ctx context.Context) error {
	p.calls++
	if p.probe != nil {
		return p.probe(ctx)
	}
	return p.TracerProvider.ForceFlush(ctx)
}

func TestFlushDetachesCancellationAndHonorsDeadline(t *testing.T) {
	for _, tc := range []struct {
		name      string
		remaining time.Duration
		cancel    bool
		wantFlush bool
	}{
		{"cancelled-no-deadline", 0, true, true},
		{"cancelled-live-deadline", time.Second, true, true},
		{"live-deadline", time.Second, false, true},
		{"long-deadline", time.Minute, false, true},
		{"inside-reserve", 100 * time.Millisecond, false, false},
		{"expired", -time.Second, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tp, recorder := recorderProvider(t)
			ctx := invocationContext()
			if tc.remaining != 0 {
				var cancel context.CancelFunc
				ctx, cancel = context.WithDeadline(ctx, time.Now().Add(tc.remaining))
				defer cancel()
			}
			if tc.cancel {
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}
			provider := &flushProvider{TracerProvider: tp, probe: func(flushCtx context.Context) error {
				if flushCtx.Err() != nil {
					t.Errorf("flush inherits cancellation: %v", flushCtx.Err())
				}
				deadline, ok := flushCtx.Deadline()
				if !ok || time.Until(deadline) > FlushTimeout {
					t.Error("flush unbounded")
				}
				if original, ok := ctx.Deadline(); ok && deadline.After(original.Add(-FlushReserve)) {
					t.Error("flush consumed runtime reserve")
				}
				if len(recorder.Ended()) != 1 {
					t.Error("flushed before invocation span ended")
				}
				return nil
			}}
			h := WrapHandler(lambda.NewHandler(func(context.Context) error { return nil }), provider)
			if _, err := h.Invoke(ctx, []byte(`{}`)); err != nil {
				t.Fatal(err)
			}
			if (provider.calls == 1) != tc.wantFlush {
				t.Fatalf("unexpected flush count: %d", provider.calls)
			}
		})
	}
}

func TestFlushFailureIsSanitizedAndDoesNotFailInvocation(t *testing.T) {
	original := otel.GetErrorHandler()
	t.Cleanup(func() { otel.SetErrorHandler(original) })
	var reported []string
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) { reported = append(reported, err.Error()) }))
	tp, _ := recorderProvider(t)
	provider := &flushProvider{TracerProvider: tp, probe: func(context.Context) error { return errors.New("synthetic-secret at https://private.invalid") }}
	h := WrapHandler(lambda.NewHandler(func() (string, error) { return "ok", nil }), provider)
	output, err := h.Invoke(invocationContext(), []byte(`{}`))
	if err != nil || string(output) != `"ok"` {
		t.Fatalf("export failure changed result: %s %v", output, err)
	}
	if len(reported) != 1 || strings.Contains(reported[0], "synthetic-secret") || strings.Contains(reported[0], "private.invalid") {
		t.Fatalf("unsafe diagnostic: %v", reported)
	}
}

func TestConcurrentColdStart(t *testing.T) {
	tp, recorder := recorderProvider(t)
	h := WrapHandler(lambda.NewHandler(func() error { return nil }), tp)
	var wg sync.WaitGroup
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := h.Invoke(invocationContext(), []byte(`{}`)); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	cold := 0
	for _, span := range recorder.Ended() {
		if attributeValue(span, "faas.coldstart").AsBool() {
			cold++
		}
	}
	if len(recorder.Ended()) != 12 || cold != 1 {
		t.Fatalf("spans=%d cold=%d", len(recorder.Ended()), cold)
	}
}

func TestNilProviderDisablesInstrumentation(t *testing.T) {
	h := lambda.NewHandler(func() error { return nil })
	if WrapHandler(h, nil) != h {
		t.Fatal("disabled wrapper replaced handler")
	}
}
