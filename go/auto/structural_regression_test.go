package auto

import (
	"context"
	"errors"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	anthropic "github.com/anthropics/anthropic-sdk-go"
	anthropicoption "github.com/anthropics/anthropic-sdk-go/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

func TestInitOncePerProcess(t *testing.T) {
	ownedRegressionSetup(t)
	first, second := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	stop, err := InitForTest(context.Background(), WithSpanProcessor(first), WithProject("first", "observe"))
	if err != nil {
		t.Fatal(err)
	}
	state, transport := agent.current, http.DefaultTransport
	for i := 0; i < 3; i++ {
		_, err := Init(context.Background(), WithSpanProcessor(second), WithWireCapture(false), func(*settings) { t.Fatal("second Init evaluated user options") })
		if err != nil || agent.current != state || http.DefaultTransport != transport || Report().Config.ProjectName != "first" {
			t.Fatal("second Init rewired process")
		}
	}
	_, span := state.provider.Tracer("cached").Start(context.Background(), "once")
	span.End()
	if len(first.Ended()) != 1 || len(second.Ended()) != 0 {
		t.Fatal("second Init changed processor")
	}
	if err := stop(context.Background()); err != nil {
		t.Fatal(err)
	}
	stopAgain, err := Init(context.Background(), WithSpanProcessor(second))
	if err != nil || agent.current != nil || agent.first != state {
		t.Fatal("Init restarted a stopped process")
	}
	if err := stopAgain(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func spanAttr(span sdktrace.ReadOnlySpan, key string) attribute.Value {
	for _, kv := range span.Attributes() {
		if string(kv.Key) == key {
			return kv.Value
		}
	}
	return attribute.Value{}
}
func TestDedupThroughNativeAncestry(t *testing.T) {
	for _, kind := range []string{"LLM", "EMBEDDING", "RERANKER", "GUARDRAIL", "CHAIN", "AGENT", "TOOL"} {
		t.Run(kind, func(t *testing.T) {
			ownedRegressionSetup(t)
			rec := tracetest.NewSpanRecorder()
			if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
				t.Fatal(err)
			}
			p := agent.current.provider
			ctx, model := p.Tracer("library").Start(context.Background(), "library")
			// Some middleware sets its kind after Start; native descendants must
			// inherit that coverage rather than inspecting only the immediate parent.
			model.SetAttributes(attribute.String("gen_ai.span.kind", kind))
			ctx, native := p.Tracer("native-sdk").Start(ctx, "native-sdk")
			ctx, nested := p.Tracer("native-http").Start(ctx, "native-http")
			r, _ := http.NewRequestWithContext(ctx, "POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
			r.Header.Set("Authorization", "Bearer REQUEST_HEADER_PRIVATE")
			resp, err := Transport(round5SuccessTransport()).RoundTrip(r)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			nested.End()
			native.End()
			model.SetAttributes(attribute.String("output.value", "REQUEST_HEADER_PRIVATE"))
			model.End()
			wireCount := 0
			for _, span := range rec.Ended() {
				if spanAttr(span, "traceai.capture.layer").AsString() == "wire" {
					wireCount++
				}
				if span.Name() == "library" && strings.Contains(spanAttr(span, "output.value").AsString(), "REQUEST_HEADER_PRIVATE") {
					t.Fatal("ancestor credential echo escaped privacy boundary")
				}
			}
			want := 0
			if kind == "CHAIN" || kind == "AGENT" || kind == "TOOL" {
				want = 1
			}
			if wireCount != want {
				t.Fatalf("wire spans=%d want=%d", wireCount, want)
			}
		})
	}
}

func TestStructuredMediaAndOverlappingSecretsOTLP(t *testing.T) {
	p := exporterRegressionSetup(t)
	app := tracetest.NewSpanRecorder()
	p.RegisterSpanProcessor(app)
	received := make(chan *collector.ExportTraceServiceRequest, 1)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		request := &collector.ExportTraceServiceRequest{}
		if err := proto.Unmarshal(data, request); err != nil {
			t.Error(err)
		}
		received <- request
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer sink.Close()
	t.Setenv("FI_BASE_URL", sink.URL)
	const api = "VERIFY_API_PREFIX"
	const secret = "VERIFY_API_PREFIX_SECRET_SUFFIX_123456789"
	if _, err := InitForTest(context.Background(), WithCredentials(api, secret), WithTraceConfig(TraceConfig{HideInputImages: true})); err != nil {
		t.Fatal(err)
	}
	state, err := trace.ParseTraceState("vendor=" + secret)
	if err != nil {
		t.Fatal(err)
	}
	parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: trace.FlagsSampled, TraceState: state, Remote: true})
	media := []attribute.KeyValue{
		attribute.Map("input.value", attribute.Map("inlineData", attribute.String("mimeType", "image/png"), attribute.String("data", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"))),
		attribute.Map("output.media", attribute.Map("inlineData", attribute.String("mimeType", "audio/wav"), attribute.String("data", "UklGRgAAAABXQVZF"))),
		attribute.String("output.value", secret),
		attribute.String("metadata.uri-json", `{"fileData":{"fileUri":"FILE_URI_PRIVATE"}}`),
		attribute.Map("metadata.uri", attribute.Map("fileData", attribute.String("fileUri", "STRUCTURED_FILE_URI_PRIVATE"))),
		attribute.StringSlice("audio_base64", []string{"SHORT_RAW_AUDIO_PRIVATE"}),
		attribute.Float64("not-finite", math.NaN()),
		attribute.Float64Slice("nonfinite-array", []float64{1, math.Inf(1), math.Inf(-1)}),
	}
	_, span := p.Tracer("scope-"+secret, trace.WithInstrumentationAttributes(media...)).Start(trace.ContextWithRemoteSpanContext(context.Background(), parent), "all-surfaces", trace.WithAttributes(media...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: media}))
	span.SetStatus(codes.Error, "provider rejected "+secret)
	span.AddEvent("exception", trace.WithAttributes(attribute.String("exception.message", secret)))
	span.End()
	if err := ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	request := <-received
	data, err := proto.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{api, "SECRET_SUFFIX", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", "UklGRgAAAABXQVZF", "FILE_URI_PRIVATE", "SHORT_RAW_AUDIO_PRIVATE"} {
		if strings.Contains(string(data), forbidden) {
			t.Fatalf("OTLP leaked %s", forbidden)
		}
	}
	s := request.ResourceSpans[0].ScopeSpans[0].Spans[0]
	if s.TraceState != "vendor=<redacted>" || strings.Contains(s.Links[0].TraceState, secret) {
		t.Fatalf("tracestate was not scrubbed: span=%q link=%q", s.TraceState, s.Links[0].TraceState)
	}
	// OTel 1.47's OTLP encoder omits link tracestate. The shared sanitizer's
	// custom-processor view must still scrub it, and the unencoded parent.
	view := exportSpan(app.Ended()[0], agent.current.config, agent.current.res)
	if view.Parent().TraceState().String() != "vendor=<redacted>" || view.Links()[0].SpanContext.TraceState().String() != "vendor=<redacted>" {
		t.Fatal("parent/link tracestate bypassed immutable export view")
	}
	found := false
	for _, kv := range s.Attributes {
		if kv.Key == "not-finite" {
			found = kv.Value.GetStringValue() == "NaN"
		}
		if kv.Key == "nonfinite-array" {
			values := kv.Value.GetArrayValue().Values
			if len(values) != 3 || values[0].GetDoubleValue() != 1 || values[1].GetStringValue() != "Infinity" || values[2].GetStringValue() != "-Infinity" {
				t.Fatal("nonfinite array lost values")
			}
		}
	}
	if !found || len(app.Ended()) != 1 || app.Ended()[0].Status().Description != "provider rejected "+secret || app.Ended()[0].SpanContext().TraceState().String() != "vendor="+secret {
		t.Fatal("span dropped or application snapshot mutated")
	}
}

func TestPrivacyBudgetFailsClosed(t *testing.T) {
	ownedRegressionSetup(t)
	app, rec := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attribute.String("secret", "RESOURCE_PRIVATE"))), sdktrace.WithSpanProcessor(app))
	defer p.Shutdown(context.Background())
	if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	v := attribute.StringValue("DEEPLY_PRIVATE")
	for i := 0; i < 40; i++ {
		v = attribute.SliceValue(v)
	}
	_, span := p.Tracer("SCOPE_PRIVATE").Start(context.Background(), "NAME_PRIVATE", trace.WithAttributes(attribute.KeyValue{Key: "input.value", Value: v}))
	span.SetStatus(codes.Error, "STATUS_PRIVATE")
	span.AddEvent("EVENT_PRIVATE")
	span.End()
	if len(rec.Ended()) != 1 {
		t.Fatal("privacy failure dropped identity/timing")
	}
	got, raw := rec.Ended()[0], app.Ended()[0]
	if len(got.Attributes()) != 0 || len(got.Events()) != 0 || len(got.Resource().Attributes()) != 0 || got.InstrumentationScope().Name != "" || got.Status().Description != "" {
		t.Fatal("sanitizer failure forwarded unchecked content")
	}
	if got.StartTime() != raw.StartTime() || got.EndTime() != raw.EndTime() || got.SpanContext().SpanID() != raw.SpanContext().SpanID() || got.Status().Code != codes.Error {
		t.Fatal("privacy fallback lost identity/timing/status code")
	}
}

func TestHideAttributeFamilies(t *testing.T) {
	for _, tc := range []struct {
		cfg  TraceConfig
		keys []string
	}{
		{TraceConfig{HideInputs: true}, []string{"input.custom", "gen_ai.input.unknown", "gen_ai.system_instructions", "gen_ai.tool.custom"}},
		{TraceConfig{HideOutputs: true}, []string{"output.custom", "gen_ai.output.unknown", "gen_ai.tool.call.result.custom", "gen_ai.tool.custom"}},
		{TraceConfig{HideInputText: true}, []string{"input.custom", "gen_ai.input.unknown"}},
		{TraceConfig{HideOutputText: true}, []string{"output.custom", "gen_ai.output.unknown"}},
		{TraceConfig{HideInputImages: true}, []string{"gen_ai.input.messages.0.image_url", "gen_ai.input.file_uri", "input.image", "gen_ai.input.fileData.fileUri"}},
		{TraceConfig{HideLLMInvocationParameters: true}, []string{"gen_ai.request.parameters"}},
	} {
		for _, key := range tc.keys {
			got, keep := tc.cfg.Mask(attribute.Map(key, attribute.String("value", "PRIVATE")))
			if keep && strings.Contains(got.Value.Emit(), "PRIVATE") {
				t.Fatalf("hide flag leaked %s", key)
			}
		}
	}
}

type parityBody struct {
	reader                   *strings.Reader
	trailer                  http.Header
	reads, closes            int
	closeErr                 error
	lastRead, closeAt, eofAt time.Time
}

func (b *parityBody) Read(p []byte) (int, error) {
	b.reads++
	n, err := b.reader.Read(p)
	if n > 0 {
		b.lastRead = time.Now()
	}
	if err == io.EOF {
		b.eofAt = time.Now()
		b.trailer.Set("X-Final", "complete")
	}
	return n, err
}
func (b *parityBody) Close() error { b.closes++; b.closeAt = time.Now(); return b.closeErr }

func TestRoundTripperResponseParity(t *testing.T) {
	for _, mode := range []string{"read", "never-read", "close-twice"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
				t.Fatal(err)
			}
			type outcome struct {
				data, trailer     string
				reads, closes     int
				readErr, closeErr error
			}
			failure := errors.New("original Close error")
			run := func(wrapped bool) outcome {
				trailer := http.Header{"X-Final": nil}
				body := &parityBody{reader: strings.NewReader(round4Response), trailer: trailer, closeErr: failure}
				original := &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: body, Trailer: trailer, ContentLength: -1}
				base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
					_, _ = io.Copy(io.Discard, r.Body)
					_ = r.Body.Close()
					original.Request = r
					return original, nil
				})
				var transport http.RoundTripper = base
				if wrapped {
					transport = Transport(base)
				}
				r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
				resp, err := transport.RoundTrip(r)
				if err != nil || resp != original || resp.Request != r || body.reads != 0 || body.closes != 0 {
					t.Fatal("RoundTrip changed response/request identity or consumed unread body")
				}
				var data []byte
				var readErr error
				if mode == "read" {
					data, readErr = io.ReadAll(resp.Body)
				}
				closeErr := resp.Body.Close()
				if mode == "close-twice" && resp.Body.Close() != failure {
					t.Fatal("second Close error changed")
				}
				return outcome{string(data), resp.Trailer.Get("X-Final"), body.reads, body.closes, readErr, closeErr}
			}
			baseline, actual := run(false), run(true)
			if !reflect.DeepEqual(actual, baseline) {
				t.Fatalf("response parity changed: actual=%+v baseline=%+v", actual, baseline)
			}
		})
	}
}

func TestResponseRequestReuseAndZeroLengthClose(t *testing.T) {
	ownedRegressionSetup(t)
	rec := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, round4Response)
	}))
	defer server.Close()
	base := unwrapTransport(http.DefaultTransport).(*wireTransport).base.(*http.Transport).Clone()
	defer base.CloseIdleConnections()
	client := &http.Client{Transport: Transport(base)}
	r, _ := http.NewRequest("POST", server.URL+"/v1/chat/completions", strings.NewReader(round4Request))
	for i := 0; i < 2; i++ {
		resp, err := client.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
		if suppressed(resp.Request.Context()) {
			t.Fatal("nested capture marker escaped its lifetime")
		}
		r = resp.Request.Clone(resp.Request.Context())
		r.Body, err = resp.Request.GetBody()
		if err != nil {
			t.Fatal(err)
		}
	}
	_ = r.Body.Close()
	if len(rec.Ended()) != 2 {
		t.Fatalf("request reuse lost spans: %d", len(rec.Ended()))
	}
	tr := Transport(round4TransportFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 204, Header: http.Header{}, Body: io.NopCloser(strings.NewReader("")), ContentLength: 0}, nil
	}))
	r, _ = http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
	resp, err := tr.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if len(rec.Ended()) != 3 || spanAttr(rec.Ended()[2], "traceai.wire.incomplete").AsBool() {
		t.Fatal("known empty body marked incomplete")
	}
}

type timingProcessor struct {
	*tracetest.SpanRecorder
	entered time.Time
}

func (p *timingProcessor) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	p.entered = time.Now()
	p.SpanRecorder.OnStart(ctx, span)
}
func TestTransportBoundaryTimestamps(t *testing.T) {
	for _, mode := range []string{"eof", "length", "close", "error"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			processor := &timingProcessor{SpanRecorder: tracetest.NewSpanRecorder()}
			if _, err := InitForTest(context.Background(), WithSpanProcessor(processor)); err != nil {
				t.Fatal(err)
			}
			body := &parityBody{reader: strings.NewReader(round4Response), trailer: http.Header{}}
			var transportEntered, transportReturned time.Time
			failure := errors.New("transport failure")
			tr := Transport(round4TransportFunc(func(r *http.Request) (*http.Response, error) {
				transportEntered = time.Now()
				_, _ = io.Copy(io.Discard, r.Body)
				transportReturned = time.Now()
				if mode == "error" {
					return nil, failure
				}
				length := int64(-1)
				if mode == "length" {
					length = int64(len(round4Response))
				}
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: body, ContentLength: length}, nil
			}))
			r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
			entry := time.Now()
			resp, err := tr.RoundTrip(r)
			if mode == "error" {
				if err != failure {
					t.Fatal("transport error identity changed")
				}
			} else {
				if err != nil {
					t.Fatal(err)
				}
				if mode == "length" {
					_, err = io.ReadFull(resp.Body, make([]byte, len(round4Response)))
				} else if mode != "close" {
					_, err = io.Copy(io.Discard, resp.Body)
				}
				if err != nil {
					t.Fatal(err)
				}
				_ = resp.Body.Close()
			}
			if len(processor.Ended()) != 1 {
				t.Fatal("missing timing span")
			}
			s := processor.Ended()[0]
			if s.StartTime().Before(entry) || s.StartTime().After(transportEntered) || s.EndTime().After(processor.entered) || s.EndTime().Before(transportReturned) {
				t.Fatal("parsing or processor work inflated transport duration")
			}
			if mode == "close" && s.EndTime().After(body.closeAt) {
				t.Fatal("underlying Close work inflated consumer-abort duration")
			}
			if mode == "eof" && s.EndTime().After(body.eofAt) {
				t.Fatal("later empty EOF read inflated transport duration")
			}
			if mode == "eof" || mode == "length" {
				if s.EndTime().Before(body.lastRead) {
					t.Fatal("span ended before last observed byte")
				}
			}
		})
	}
}

func TestRealAnthropicDedupThroughNativeSpan(t *testing.T) {
	ownedRegressionSetup(t)
	p := sdktrace.NewTracerProvider()
	defer p.Shutdown(context.Background())
	otel.SetTracerProvider(p)
	rec := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"id":"msg_1","type":"message","role":"assistant","model":"test","content":[{"type":"text","text":"hello"}],"stop_reason":"end_turn","usage":{"input_tokens":2,"output_tokens":1}}`)
	}))
	defer server.Close()
	ctx, model := p.Tracer("traceai-middleware").Start(context.Background(), "model", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM")))
	client := anthropic.NewClient(anthropicoption.WithAPIKey("ANTHROPIC_PROVIDER_PRIVATE"), anthropicoption.WithBaseURL(server.URL), anthropicoption.WithMaxRetries(0))
	message, err := client.Messages.New(ctx, anthropic.MessageNewParams{Model: "test", MaxTokens: 16, Messages: []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("hi"))}})
	if err != nil || message.ID != "msg_1" {
		t.Fatalf("SDK return value changed: %v", err)
	}
	model.SetAttributes(attribute.String("output.value", "ANTHROPIC_PROVIDER_PRIVATE"))
	model.End()
	for _, span := range rec.Ended() {
		if spanAttr(span, "traceai.capture.layer").AsString() == "wire" {
			t.Fatal("real Anthropic native span broke middleware deduplication")
		}
		if strings.Contains(spanAttr(span, "output.value").AsString(), "ANTHROPIC_PROVIDER_PRIVATE") {
			t.Fatal("real SDK credential escaped ancestor privacy")
		}
	}
}

type upgradedBody struct {
	strings.Reader
	writes int
}

func (b *upgradedBody) Write(p []byte) (int, error) { b.writes++; return len(p), nil }
func (b *upgradedBody) Close() error                { return nil }
func TestUpgradeBodyPassedThrough(t *testing.T) {
	ownedRegressionSetup(t)
	rec := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	body := &upgradedBody{}
	base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 101, Body: body, Header: http.Header{}, Request: r}, nil
	})
	r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
	resp, err := Transport(base).RoundTrip(r)
	if err != nil || resp.Body != body || resp.Request != r {
		t.Fatal("upgrade identity changed")
	}
	if _, err := resp.Body.(io.Writer).Write([]byte("payload")); err != nil || body.writes != 1 {
		t.Fatal("bidirectional body lost Writer")
	}
	_ = resp.Body.Close()
	if len(rec.Ended()) != 0 {
		t.Fatal("unsupported upgrade captured as an AI HTTP response")
	}
}

func TestResponseTrailersBaselineHTTP1AndHTTP2(t *testing.T) {
	for _, h2 := range []bool{false, true} {
		t.Run(map[bool]string{false: "http1", true: "http2"}[h2], func(t *testing.T) {
			ownedRegressionSetup(t)
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
				t.Fatal(err)
			}
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "application/json")
				w.Header().Add("Trailer", "X-Final")
				_, _ = io.WriteString(w, round4Response)
				w.Header().Set("X-Final", "complete")
			}))
			server.EnableHTTP2 = h2
			server.StartTLS()
			defer server.Close()
			base := server.Client().Transport
			defer base.(*http.Transport).CloseIdleConnections()
			for _, wrapped := range []bool{false, true} {
				transport := base
				if wrapped {
					transport = Transport(base)
				}
				r, _ := http.NewRequest("POST", server.URL+"/v1/chat/completions", strings.NewReader(round4Request))
				resp, err := (&http.Client{Transport: transport}).Do(r)
				if err != nil {
					t.Fatal(err)
				}
				data, readErr := io.ReadAll(resp.Body)
				closeErr := resp.Body.Close()
				wantProtocol := 1
				if h2 {
					wantProtocol = 2
				}
				if string(data) != round4Response || resp.Trailer.Get("X-Final") != "complete" || resp.ProtoMajor != wantProtocol || readErr != nil || closeErr != nil {
					t.Fatalf("wrapped=%v response bytes/trailers/protocol/errors changed", wrapped)
				}
			}
		})
	}
}
