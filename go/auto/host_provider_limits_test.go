package auto

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

type s6HostProcessor struct {
	*tracetest.SpanRecorder
	flushed, stopped atomic.Int32
	addOverflow      bool
}

func (p *s6HostProcessor) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	p.SpanRecorder.OnStart(ctx, span)
	if p.addOverflow {
		for i := 0; i < 200; i++ {
			span.SetAttributes(attribute.Int(fmt.Sprintf("host.extra.%d", i), i))
		}
	}
}
func (p *s6HostProcessor) Shutdown(context.Context) error   { p.stopped.Add(1); return nil }
func (p *s6HostProcessor) ForceFlush(context.Context) error { p.flushed.Add(1); return nil }

func TestS6NeutralProviderActivation(t *testing.T) {
	for _, mode := range []string{"explicit", "global", "processor"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			t.Setenv("FI_API_KEY", "")
			t.Setenv("FI_SECRET_KEY", "")
			// Neutral activation must not even construct an FI destination.
			t.Setenv("FI_BASE_URL", "://unused-FI-endpoint")
			host := &s6HostProcessor{SpanRecorder: tracetest.NewSpanRecorder()}
			p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(host))
			defer p.Shutdown(context.Background())
			opts := []Option{}
			switch mode {
			case "explicit":
				opts = append(opts, WithTracerProvider(p))
			case "global":
				otel.SetTracerProvider(p)
			case "processor":
				opts = append(opts, WithTracerProvider(p), WithSpanProcessor(tracetest.NewSpanRecorder()))
			}
			global := otel.GetTracerProvider()
			stop, err := InitForTest(context.Background(), opts...)
			if err != nil || agent.current == nil || agent.current.provider != p || agent.current.owned || otel.GetTracerProvider() != global {
				t.Fatalf("neutral init changed provider ownership: %v", err)
			}
			s6LongExchange(t, "input", 2)
			if len(host.Ended()) != 1 || spanAttr(host.Ended()[0], "traceai.capture.layer").AsString() != "wire" {
				t.Fatal("application provider received no wire span")
			}
			if err := ForceFlush(context.Background()); err != nil || mode != "processor" && host.flushed.Load() != 1 {
				t.Fatalf("host flush failed: %v", err)
			}
			if err := stop(context.Background()); err != nil || host.stopped.Load() != 0 {
				t.Fatalf("agent shut down application processors: %v", err)
			}
			_, control := p.Tracer("host").Start(context.Background(), "after-agent-shutdown")
			control.End()
			if len(host.Ended()) != 2 {
				t.Fatal("agent shutdown damaged host provider")
			}
		})
	}
}

func s6ProtoAttrs(span *tracepb.Span) map[string]any {
	values := map[string]any{}
	for _, kv := range span.Attributes {
		values[kv.Key] = kv.Value
	}
	return values
}

func TestS6SmallHostAttributeLimitsOTLP(t *testing.T) {
	for _, limit := range []int{16, 32, 128} {
		t.Run(fmt.Sprint(limit), func(t *testing.T) {
			exporterRegressionSetup(t)
			received := privacyCollector(t)
			limits := sdktrace.NewSpanLimits()
			limits.AttributeCountLimit = limit
			host := &s6HostProcessor{SpanRecorder: tracetest.NewSpanRecorder(), addOverflow: true}
			p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(limits), sdktrace.WithSpanProcessor(host))
			defer p.Shutdown(context.Background())
			if providerAttributeLimit(p) != limit {
				t.Fatal("pinned SDK limit adapter is stale")
			}
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("S6_LIMIT_FI_KEY", "S6_LIMIT_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			messages := make([]map[string]string, 80)
			for i := range messages {
				messages[i] = map[string]string{"role": "user", "content": fmt.Sprintf("message %d", i)}
			}
			body, _ := json.Marshal(map[string]any{"model": "m", "messages": messages})
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				data, _ := io.ReadAll(r.Body)
				if !bytes.Equal(data, body) {
					t.Error("changed request")
				}
				w.Header().Set("Content-Type", "application/json")
				_, _ = io.WriteString(w, `{"id":"c1","model":"m","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}`)
			}))
			defer provider.Close()
			ctx := ContextWithUserID(ContextWithSessionID(context.Background(), "session-context"), "user-context")
			req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+"/v1/chat/completions", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_, readErr := io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if readErr != nil {
				t.Fatal(readErr)
			}
			request := flushedPrivacyExport(t, received)
			span := request.ResourceSpans[0].ScopeSpans[0].Spans[0]
			attrs := s6ProtoAttrs(span)
			for _, key := range []string{"user.id", "session.id", "traceai.capture.layer", "traceai.wire.attributes_truncated", "traceai.capture.dropped_content_count", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens"} {
				if attrs[key] == nil {
					t.Errorf("host limit %d lost protected %s", limit, key)
				}
			}
			original := host.Ended()[0]
			if len(span.Attributes) > limit || span.DroppedAttributesCount != uint32(original.DroppedAttributes()) || original.DroppedAttributes() == 0 {
				t.Fatal("provider limit or host SDK dropped counts changed")
			}
			if spanAttr(original, "traceai.wire.attributes_truncated").AsBool() != true {
				t.Fatal("marker was added only after host eviction")
			}
		})
	}
}

func TestS6AttributeProjectionContracts(t *testing.T) {
	values := []attribute.KeyValue{attribute.String("gen_ai.span.kind", "LLM"), attribute.String("traceai.capture.layer", "wire"), attribute.String("user.id", "u"), attribute.String("session.id", "s"), attribute.Int64("gen_ai.usage.total_tokens", 9), attribute.Int64("traceai.capture.dropped_content_count", 2), attribute.String("output.value", "ok"), attribute.String("output.mime_type", "text/plain"), attribute.String("input.value", "old + new"), attribute.String("input.mime_type", "text/plain")}
	for i := 0; i < 12; i++ {
		values = append(values, attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.role", i), "user"), attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.content", i), "message"))
	}
	for _, limit := range []int{-1, 0, 1, 5, 8, 13, 16, 32, 128} {
		t.Run(fmt.Sprint(limit), func(t *testing.T) {
			got := budgetAttributes(values, limit)
			reversed := append([]attribute.KeyValue(nil), values...)
			for i, j := 0, len(reversed)-1; i < j; i, j = i+1, j-1 {
				reversed[i], reversed[j] = reversed[j], reversed[i]
			}
			if !reflect.DeepEqual(got, budgetAttributes(reversed, limit)) || limit >= 0 && len(got) > limit {
				t.Fatal("nondeterministic or over-budget projection")
			}
			selected := map[string]attribute.Value{}
			for _, kv := range got {
				selected[string(kv.Key)] = kv.Value
			}
			keptContent := 0
			for key := range selected {
				if contentGroup(key, "").content {
					keptContent++
				}
			}
			if count, ok := selected["traceai.capture.dropped_content_count"]; ok && count.AsInt64() != 2+int64(28-keptContent) {
				t.Fatal("dropped-content count did not accumulate exact omitted keys")
			}
			missing := false
			for i := 11; i >= 0; i-- {
				_, role := selected[fmt.Sprintf("gen_ai.input.messages.%d.message.role", i)]
				_, text := selected[fmt.Sprintf("gen_ai.input.messages.%d.message.content", i)]
				if role != text || missing && role {
					t.Fatal("partial message or noncontiguous input suffix")
				}
				missing = missing || !role
			}
			if missing && selected["input.value"].Type() != attribute.EMPTY {
				t.Fatal("aggregate input restored dropped conversation")
			}
			if selected["output.value"].Type() != selected["output.mime_type"].Type() {
				t.Fatal("dangling output MIME companion")
			}
		})
	}
}

func TestS6UsageFrameBoundaryOTLP(t *testing.T) {
	const usage = `{"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}`
	const gemini = `{"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":2,"totalTokenCount":6}}`
	for _, tc := range []struct {
		name, path, ct, body string
		total                int64
	}{
		{"sse-incomplete-delimiter", "/v1/chat/completions", "text/event-stream", "data: " + usage + "\n", -1},
		{"sse-complete-prefix", "/v1/chat/completions", "text/event-stream", "data: " + usage + "\n\ndata: {\"usage\":", 6},
		{"sse-crlf", "/v1/chat/completions", "text/event-stream", "data: " + usage + "\r\n\r\n", 6},
		{"json-complete-array-members", "/v1beta/models/m:streamGenerateContent", "application/json", "[{}," + gemini, 6},
		{"json-partial-member", "/v1beta/models/m:streamGenerateContent", "application/json", `[{"usageMetadata":{"promptTokenCount":4`, -1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			exporterRegressionSetup(t)
			received := privacyCollector(t)
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", tc.ct)
				w.Header().Set("Content-Length", fmt.Sprint(len(tc.body)+20))
				_, _ = io.WriteString(w, tc.body) // Deliberate real HTTP unexpected EOF.
			}))
			defer provider.Close()
			native := &http.Transport{}
			defer native.CloseIdleConnections()
			run := func(transport http.RoundTripper) (string, string) {
				req, _ := http.NewRequest("POST", provider.URL+tc.path, strings.NewReader(`{"model":"m","messages":[],"contents":[{"role":"user","parts":[{"text":"hi"}]}]}`))
				req.Header.Set("Content-Type", "application/json")
				resp, err := (&http.Client{Transport: transport}).Do(req)
				if err != nil {
					t.Fatal(err)
				}
				data, readErr := io.ReadAll(resp.Body)
				_ = resp.Body.Close()
				if readErr == nil {
					t.Fatal("HTTP fixture did not fail")
				}
				return string(data), fmt.Sprintf("%T:%v", readErr, readErr)
			}
			baseline, baselineErr := run(native)
			if _, err := InitForTest(context.Background(), WithCredentials("S6_FRAME_FI_KEY", "S6_FRAME_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			actual, actualErr := run(Transport(native))
			if actual != baseline || actualErr != baselineErr {
				t.Fatal("application response bytes/errors changed")
			}
			request := flushedPrivacyExport(t, received)
			span := request.ResourceSpans[0].ScopeSpans[0].Spans[0]
			found := false
			for _, kv := range span.Attributes {
				if kv.Key == "gen_ai.usage.total_tokens" {
					found = true
					if kv.Value.GetIntValue() != tc.total {
						t.Fatalf("total=%d want=%d", kv.Value.GetIntValue(), tc.total)
					}
				}
			}
			if found != (tc.total >= 0) || span.Status.Code != tracepb.Status_STATUS_CODE_ERROR {
				t.Fatal("observed-frame usage/outcome violated")
			}
		})
	}
}
