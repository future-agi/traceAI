package traceaibedrock

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream"
	"github.com/aws/aws-sdk-go-v2/aws/retry"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/document"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"github.com/aws/smithy-go"
	"github.com/aws/smithy-go/middleware"
	"github.com/aws/smithy-go/tracing/smithyoteltracing"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdk "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const model = "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.anthropic.claude-test-v1:0"
const syncBody = `{"output":{"message":{"role":"assistant","content":[{"text":"hello"},{"toolUse":{"toolUseId":"call-1","name":"weather","input":{"city":"Paris"}}}]}},"stopReason":"tool_use","usage":{"inputTokens":3,"outputTokens":2,"totalTokens":5,"cacheReadInputTokens":0,"cacheWriteInputTokens":4},"metrics":{"latencyMs":1}}`

func provider(t *testing.T) (*sdk.TracerProvider, *tracetest.InMemoryExporter) {
	t.Helper()
	e := tracetest.NewInMemoryExporter()
	p := sdk.NewTracerProvider(sdk.WithSyncer(e))
	t.Cleanup(func() { _ = p.Shutdown(context.Background()) })
	return p, e
}
func attrs(s tracetest.SpanStub) map[string]attribute.Value {
	m := map[string]attribute.Value{}
	for _, a := range s.Attributes {
		m[string(a.Key)] = a.Value
	}
	return m
}
func llms(e *tracetest.InMemoryExporter) []tracetest.SpanStub {
	var out []tracetest.SpanStub
	for _, s := range e.GetSpans() {
		if attrs(s)["gen_ai.span.kind"].AsString() == "LLM" {
			out = append(out, s)
		}
	}
	return out
}
func rawClient(t *testing.T, h http.HandlerFunc, options ...func(*bedrockruntime.Options)) *bedrockruntime.Client {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Bedrock parses the request before streaming its response. Finish reading
		// HTTP/1 request bytes before Flush, rather than exercising half-duplex
		// server connection cleanup while the SDK is still writing the request.
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		_ = r.Body.Close()
		r.Body = io.NopCloser(bytes.NewReader(body))
		h(w, r)
	}))
	t.Cleanup(server.Close)
	options = append(options, func(o *bedrockruntime.Options) { o.HTTPClient = requestWriteBarrier{o.HTTPClient} })
	return bedrockruntime.New(bedrockruntime.Options{Region: "us-east-1", BaseEndpoint: aws.String(server.URL), Credentials: credentials.NewStaticCredentialsProvider("synthetic-access", "synthetic-secret", ""), Retryer: retry.NewStandard(func(o *retry.StandardOptions) {
		o.MaxAttempts = 2
		o.Backoff = retry.BackoffDelayerFunc(func(int, error) (time.Duration, error) { return 0, nil })
	})}, options...)
}

// Loopback responses can arrive before net/http finishes its final request-body
// read. Smithy's ClientHandler closes that body as soon as Do returns, causing
// a native write-side EOF and socket close unrelated to event-stream decoding.
// Synchronize fixture writes instead of adding sleeps or accepting lost events.
type requestWriteBarrier struct{ bedrockruntime.HTTPClient }

func (c requestWriteBarrier) Do(r *http.Request) (*http.Response, error) {
	written := make(chan struct{})
	var once sync.Once
	ctx := httptrace.WithClientTrace(r.Context(), &httptrace.ClientTrace{WroteRequest: func(httptrace.WroteRequestInfo) { once.Do(func() { close(written) }) }})
	response, err := c.HTTPClient.Do(r.WithContext(ctx))
	if err == nil {
		select {
		case <-written:
		case <-ctx.Done():
		}
	}
	return response, err
}

func input() *bedrockruntime.ConverseInput {
	return &bedrockruntime.ConverseInput{ModelId: aws.String(model), Messages: []types.Message{{Role: types.ConversationRoleUser, Content: []types.ContentBlock{&types.ContentBlockMemberText{Value: "question"}, &types.ContentBlockMemberToolResult{Value: types.ToolResultBlock{ToolUseId: aws.String("call-old"), Content: []types.ToolResultContentBlock{&types.ToolResultContentBlockMemberText{Value: "tool-result"}}}}}}}, System: []types.SystemContentBlock{&types.SystemContentBlockMemberText{Value: "system prompt"}}, InferenceConfig: &types.InferenceConfiguration{MaxTokens: aws.Int32(100), Temperature: aws.Float32(0)}, ToolConfig: &types.ToolConfiguration{Tools: []types.Tool{&types.ToolMemberToolSpec{Value: types.ToolSpecification{Name: aws.String("weather"), InputSchema: &types.ToolInputSchemaMemberJson{Value: document.NewLazyDocument(map[string]any{"type": "object"})}}}}}}
}
func TestConverseNativeAndSemantic(t *testing.T) {
	p, e := provider(t)
	var hooks atomic.Int32
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256") {
			t.Error("AWS signing missing")
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, syncBody)
	}, func(o *bedrockruntime.Options) {
		o.TracerProvider = smithyoteltracing.Adapt(p)
		o.APIOptions = append(o.APIOptions, func(s *middleware.Stack) error { hooks.Add(1); return nil })
	})
	// Native spans are real, but contain no LLM kind or messages.
	if _, err := raw.Converse(context.Background(), input()); err != nil {
		t.Fatal(err)
	}
	if len(e.GetSpans()) == 0 || len(llms(e)) != 0 {
		t.Fatal("native baseline does not match")
	}
	e.Reset()
	c := NewClient(raw, WithTracerProvider(p))
	ctx, parent := p.Tracer("app").Start(ContextWithSessionID(context.Background(), "session-a"), "parent")
	out, err := c.Converse(ctx, input())
	parent.End()
	if err != nil || out.Usage == nil {
		t.Fatal(err)
	}
	spans := llms(e)
	if len(spans) != 1 {
		t.Fatalf("LLM spans: %d", len(spans))
	}
	a := attrs(spans[0])
	if a["gen_ai.request.model"].AsString() != model || a["session.id"].AsString() != "session-a" {
		t.Fatal(a)
	}
	for k, v := range map[string]int64{"input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 4} {
		if x := a["gen_ai.usage."+k]; x.Type() != attribute.INT64 || x.AsInt64() != v {
			t.Fatalf("%s: %v", k, x)
		}
	}
	for _, v := range []string{"question", "system prompt", "tool-result"} {
		if !strings.Contains(a["input.value"].AsString(), v) {
			t.Fatal(v)
		}
	}
	if !strings.Contains(a["output.value"].AsString(), "weather") || hooks.Load() != 2 {
		t.Fatal(a)
	}
	var definition struct {
		InputSchema struct {
			Type string `json:"type"`
		} `json:"input_schema"`
	}
	if err := json.Unmarshal([]byte(a["gen_ai.tool.definitions.0.tool.json_schema"].AsString()), &definition); err != nil || definition.InputSchema.Type != "object" {
		t.Fatal("tool schema must stay structured", err)
	}
	if spans[0].Parent.SpanID() != parent.SpanContext().SpanID() {
		t.Fatal("parent lost")
	}
}
func frame(w io.Writer, typ, payload string) error {
	var h eventstream.Headers
	h.Set(":message-type", eventstream.StringValue("event"))
	h.Set(":event-type", eventstream.StringValue(typ))
	h.Set(":content-type", eventstream.StringValue("application/json"))
	return eventstream.NewEncoder().Encode(w, eventstream.Message{Headers: h, Payload: []byte(payload)})
}
func converseFrames(w io.Writer) {
	for _, ev := range [][2]string{{"messageStart", `{"role":"assistant"}`}, {"contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"hel"}}`}, {"contentBlockStart", `{"contentBlockIndex":1,"start":{"toolUse":{"toolUseId":"c1","name":"weather"}}}`}, {"contentBlockDelta", `{"contentBlockIndex":1,"delta":{"toolUse":{"input":"{\"city\":"}}}`}, {"contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"lo"}}`}, {"contentBlockDelta", `{"contentBlockIndex":1,"delta":{"toolUse":{"input":"\"Paris\"}"}}}`}, {"messageStop", `{"stopReason":"tool_use"}`}, {"metadata", `{"usage":{"inputTokens":3,"outputTokens":2,"totalTokens":5,"cacheReadInputTokens":0,"cacheWriteInputTokens":4},"metrics":{"latencyMs":2}}`}} {
		_ = frame(w, ev[0], ev[1])
	}
}
func TestConverseStreamLifetimeAndIdentity(t *testing.T) {
	p, e := provider(t)
	release := make(chan struct{})
	defer close(release)
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
		_ = frame(w, "messageStart", `{"role":"assistant"}`)
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		converseFrames(w)
	})
	c := NewClient(raw, WithTracerProvider(p))
	ctx, cancel := context.WithTimeout(context.Background(), time.Second*5)
	defer cancel()
	out, err := c.ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages})
	if err != nil {
		t.Fatal(err)
	}
	if len(llms(e)) != 0 {
		t.Fatal("span ended at headers")
	}
	first := <-out.GetStream().Events()
	if _, ok := first.(*types.ConverseStreamOutputMemberMessageStart); !ok {
		t.Fatalf("first %T", first)
	}
	// SDK stream's public Reader has not been replaced.
	if _, ok := out.ConverseStreamOutput.GetStream().Reader.(*stream[types.ConverseStreamOutput]); ok {
		t.Fatal("mutated SDK Reader")
	}
	release <- struct{}{}
	var text string
	count := 1
	for ev := range out.GetStream().Events() {
		count++
		if ev, ok := ev.(*types.ConverseStreamOutputMemberContentBlockDelta); ok {
			if d, ok := ev.Value.Delta.(*types.ContentBlockDeltaMemberText); ok {
				text += d.Value
			}
		}
	}
	if err := out.GetStream().Err(); err != nil {
		t.Fatalf("stream error after %d events (%q), context=%v: %v", count, text, ctx.Err(), err)
	}
	_ = out.GetStream().Close()
	if text != "hello" || count != 9 {
		t.Fatalf("%q %d", text, count)
	}
	spans := llms(e)
	if len(spans) != 1 {
		t.Fatalf("spans %d", len(spans))
	}
	a := attrs(spans[0])
	if !strings.Contains(a["output.value"].AsString(), "Paris") || a["gen_ai.server.time_to_first_token"].AsFloat64() <= 0 {
		t.Fatal(a)
	}
}
func TestStreamFailuresAndClose(t *testing.T) {
	for _, mode := range []string{"empty", "malformed", "service", "close-unread", "cancel-unread", "deadline", "concurrent-close"} {
		t.Run(mode, func(t *testing.T) {
			p, e := provider(t)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				switch mode {
				case "empty":
					return
				case "malformed":
					_, _ = w.Write([]byte("not an event secret-canary"))
					return
				case "service":
					var h eventstream.Headers
					h.Set(":message-type", eventstream.StringValue("exception"))
					h.Set(":exception-type", eventstream.StringValue("modelStreamErrorException"))
					_ = eventstream.NewEncoder().Encode(w, eventstream.Message{Headers: h, Payload: []byte(`{"message":"secret-canary"}`)})
					return
				default:
					_ = frame(w, "contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"hello"}}`)
					w.(http.Flusher).Flush()
					<-r.Context().Done()
				}
			})
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			if mode == "deadline" {
				var stop context.CancelFunc
				ctx, stop = context.WithTimeout(ctx, 100*time.Millisecond)
				defer stop()
			}
			out, err := NewClient(raw, WithTracerProvider(p)).ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages})
			if err != nil {
				t.Fatal(err)
			}
			s := out.GetStream()
			switch mode {
			case "close-unread":
				_ = s.Close()
			case "concurrent-close":
				var wg sync.WaitGroup
				for i := 0; i < 8; i++ {
					wg.Go(func() { _ = s.Close() })
				}
				wg.Wait()
			case "cancel-unread":
				cancel()
				for range s.Events() {
				}
			default:
				for range s.Events() {
				}
			}
			if mode == "service" || mode == "malformed" {
				if s.Err() == nil {
					t.Fatal("lost SDK stream error")
				}
			}
			if mode == "cancel-unread" && !errors.Is(s.Err(), context.Canceled) {
				t.Errorf("lost cancellation error: %v", s.Err())
			}
			if mode == "deadline" && !errors.Is(s.Err(), context.DeadlineExceeded) {
				t.Errorf("lost deadline error: %v", s.Err())
			}
			_ = s.Close()
			spans := llms(e)
			if len(spans) != 1 {
				t.Fatalf("end count %d", len(spans))
			}
			if mode == "service" || mode == "malformed" || mode == "deadline" || mode == "cancel-unread" {
				if spans[0].Status.Code != codes.Error {
					t.Fatal("missing error status")
				}
			}
			b, _ := json.Marshal(spans)
			if strings.Contains(string(b), "secret-canary") {
				t.Fatal("error details leaked")
			}
		})
	}
}
func TestRetriesAndPreDeserializeErrors(t *testing.T) {
	for _, exhaust := range []bool{false, true} {
		t.Run(map[bool]string{false: "retry-success", true: "exhausted"}[exhaust], func(t *testing.T) {
			p, e := provider(t)
			var attempts atomic.Int32
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				n := attempts.Add(1)
				w.Header().Set("Content-Type", "application/json")
				if exhaust || n == 1 {
					w.Header().Set("X-Amzn-Errortype", "ThrottlingException")
					w.WriteHeader(429)
					io.WriteString(w, `{"message":"secret-canary"}`)
					return
				}
				io.WriteString(w, syncBody)
			})
			_, err := NewClient(raw, WithTracerProvider(p)).Converse(context.Background(), input())
			if (err != nil) != exhaust {
				t.Fatal(err)
			}
			if exhaust {
				var api smithy.APIError
				if !errors.As(err, &api) || api.ErrorCode() != "ThrottlingException" {
					t.Fatal(err)
				}
			}
			if len(llms(e)) != 1 || attempts.Load() != 2 {
				t.Fatalf("spans/attempts %d/%d", len(llms(e)), attempts.Load())
			}
			b, _ := json.Marshal(llms(e))
			if bytes.Contains(b, []byte("secret-canary")) {
				t.Fatal("error leaked")
			}
		})
	}
	p, e := provider(t)
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) { t.Error("invalid input reached wire") })
	_, err := NewClient(raw, WithTracerProvider(p)).Converse(context.Background(), &bedrockruntime.ConverseInput{})
	if err == nil || len(llms(e)) != 1 || llms(e)[0].Status.Code != codes.Error {
		t.Fatal("validation error not captured")
	}
}
func TestPrivacyAndBounds(t *testing.T) {
	for _, flag := range []string{"INPUTS", "INPUT_MESSAGES", "INPUT_TEXT", "OUTPUTS", "OUTPUT_MESSAGES", "OUTPUT_TEXT", "LLM_INVOCATION_PARAMETERS", "INPUT_IMAGES", "ERROR_DETAILS"} {
		t.Run(flag, func(t *testing.T) {
			t.Setenv("FI_HIDE_"+flag, " TrUe ")
			p, e := provider(t)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				io.WriteString(w, strings.ReplaceAll(syncBody, "hello", "output-canary"))
			})
			in := input()
			in.System = []types.SystemContentBlock{&types.SystemContentBlockMemberText{Value: "input-canary"}}
			in.Messages[0].Content = []types.ContentBlock{&types.ContentBlockMemberText{Value: "input-canary"}, &types.ContentBlockMemberToolUse{Value: types.ToolUseBlock{ToolUseId: aws.String("input-canary"), Name: aws.String("input-canary"), Input: document.NewLazyDocument(map[string]string{"value": "input-canary"})}}, &types.ContentBlockMemberImage{Value: types.ImageBlock{Format: types.ImageFormatPng, Source: &types.ImageSourceMemberBytes{Value: []byte("binary-canary")}}}}
			in.ToolConfig = nil
			_, err := NewClient(raw, WithTracerProvider(p)).Converse(context.Background(), in)
			if err != nil {
				t.Fatal(err)
			}
			b, _ := json.Marshal(llms(e))
			text := string(b)
			if flag == "INPUTS" || flag == "INPUT_MESSAGES" || flag == "INPUT_TEXT" {
				if strings.Contains(text, "input-canary") {
					t.Fatal("input leaked")
				}
			}
			if flag == "OUTPUTS" || flag == "OUTPUT_MESSAGES" || flag == "OUTPUT_TEXT" {
				if strings.Contains(text, "output-canary") {
					t.Fatal("output leaked")
				}
			}
			if strings.Contains(text, "binary-canary") || strings.Contains(text, base64.StdEncoding.EncodeToString([]byte("binary-canary"))) {
				t.Fatal("media leaked")
			}
			if flag == "LLM_INVOCATION_PARAMETERS" {
				if _, ok := attrs(llms(e)[0])["gen_ai.request.max_tokens"]; ok {
					t.Fatal("parameters leaked")
				}
			}
		})
	}
	p, e := provider(t)
	_, r := NewClient(nil, WithTracerProvider(p)).start(context.Background(), "test", nil)
	for i := 0; i < 10000; i++ {
		r.delta(int32(i%100), "text", strings.Repeat("a", 1000))
	}
	if r.used > maxCapture || len(r.blocks) > maxBlocks {
		t.Fatal("unbounded capture")
	}
	r.finish(nil, false)
	if !attrs(llms(e)[0])["traceai.capture.truncated"].AsBool() {
		t.Fatal("truncation missing")
	}
}
func TestProvidersSuppressionAndIsolation(t *testing.T) {
	p, e := provider(t)
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(p)
	defer otel.SetTracerProvider(old)
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, syncBody)
	})
	c := NewClient(raw)
	for _, id := range []string{"a", "b", ""} {
		_, err := c.Converse(ContextWithUserID(ContextWithSessionID(context.Background(), id), id), input())
		if err != nil {
			t.Fatal(err)
		}
	}
	for i, s := range llms(e) {
		want := []string{"a", "b", ""}[i]
		if attrs(s)["session.id"].AsString() != want {
			t.Fatal("session leaked")
		}
	}
	_, err := c.Converse(ContextWithSuppression(context.Background()), input())
	if err != nil {
		t.Fatal(err)
	}
	dropped := sdk.NewTracerProvider(sdk.WithSampler(sdk.NeverSample()))
	defer dropped.Shutdown(context.Background())
	_, err = NewClient(raw, WithTracerProvider(dropped)).Converse(context.Background(), input())
	if err != nil || len(llms(e)) != 3 {
		t.Fatal("suppression failed")
	}
}

// Ensure no accidental interface drift in the stream decorator.
var _ bedrockruntime.ConverseStreamOutputReader = (*stream[types.ConverseStreamOutput])(nil)
var _ trace.TracerProvider = (*sdk.TracerProvider)(nil)
