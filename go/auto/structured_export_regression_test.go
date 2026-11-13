package auto

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

func round5StructuredAttributes() []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.Map("metadata",
			attribute.String("api_key", "MAP_CREDENTIAL_PRIVATE"),
			attribute.Map("nested", attribute.String("echo", "FI_KEY_PRIVATE FI_SECRET_PRIVATE PROVIDER_HEADER_PRIVATE"), attribute.String("data", strings.Repeat("YWJj", 100))),
			attribute.Slice("values", attribute.StringValue("https://example.com?sig=SLICE_SIGNED_PRIVATE"), attribute.MapValue(attribute.String("client-secret", "NESTED_SECRET_PRIVATE")), attribute.Int64Value(7), attribute.BoolValue(true)),
			attribute.Slice("images", attribute.StringValue(strings.Repeat("QUJD", 100))),
			attribute.ByteSlice("binary.echo", []byte("FI_SECRET_PRIVATE")),
			attribute.Map("password", attribute.String("value", "STRUCTURED_PASSWORD_PRIVATE")),
			attribute.String("visible", "ordinary metadata")),
		attribute.Slice("mixed", attribute.MapValue(attribute.String("token", "SLICE_TOKEN_PRIVATE")), attribute.SliceValue(attribute.StringValue("FI_KEY_PRIVATE")), attribute.StringValue("data:image/png;base64,QUJDQUJD")),
		attribute.Map("input.value", attribute.String("text", "INPUT_PRIVATE")),
		attribute.Slice("output.value", attribute.StringValue("OUTPUT_PRIVATE")),
	}
}

type round5InjectProcessor struct{ *tracetest.SpanRecorder }

func (p *round5InjectProcessor) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	p.SpanRecorder.OnStart(ctx, span)
	span.SetAttributes(round5StructuredAttributes()...)
	span.AddEvent("exception", trace.WithAttributes(
		attribute.String("exception.type", "ProviderError"),
		attribute.Map("exception.details", attribute.String("text", "EVENT_INPUT_PRIVATE")),
		attribute.Slice("exception.payload", attribute.MapValue(attribute.String("text", "EVENT_OUTPUT_PRIVATE"))),
		attribute.Map("exception.credentials", attribute.String("api_key", "EVENT_CREDENTIAL_PRIVATE")),
	))
	span.AddLink(trace.Link{SpanContext: trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}}), Attributes: round5StructuredAttributes()})
}

func TestStructuredOTLPExportPrivacy(t *testing.T) {
	for _, layer := range []string{"library", "wire"} {
		for _, mode := range []string{"scrub", "hide-inputs", "hide-outputs"} {
			t.Run(layer+"/"+mode, func(t *testing.T) {
				ownedRegressionSetup(t)
				app := tracetest.NewSpanRecorder()
				p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(round5StructuredAttributes()...)), sdktrace.WithSpanProcessor(&round5InjectProcessor{app}))
				defer p.Shutdown(context.Background())
				received := make(chan *collector.ExportTraceServiceRequest, 1)
				sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					data, err := io.ReadAll(r.Body)
					if err != nil {
						t.Error(err)
					}
					x := &collector.ExportTraceServiceRequest{}
					if err := proto.Unmarshal(data, x); err != nil {
						t.Error(err)
					}
					received <- x
					w.Header().Set("Content-Type", "application/x-protobuf")
				}))
				defer sink.Close()
				t.Setenv("FI_BASE_URL", sink.URL)
				cfg := TraceConfig{HideInputs: mode == "hide-inputs", HideOutputs: mode == "hide-outputs"}
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("FI_KEY_PRIVATE", "FI_SECRET_PRIVATE"), WithTraceConfig(cfg)); err != nil {
					t.Fatal(err)
				}
				if layer == "library" {
					ctx, span := p.Tracer("library", trace.WithInstrumentationAttributes(round5StructuredAttributes()...)).Start(context.Background(), "library", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM")))
					r, _ := http.NewRequestWithContext(ctx, "POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
					r.Header.Set("Authorization", "Bearer PROVIDER_HEADER_PRIVATE")
					resp, err := Transport(round5SuccessTransport()).RoundTrip(r)
					if err != nil {
						t.Fatal(err)
					}
					_, _ = io.Copy(io.Discard, resp.Body)
					_ = resp.Body.Close()
					span.End()
				} else {
					r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
					r.Header.Set("Authorization", "Bearer PROVIDER_HEADER_PRIVATE")
					resp, err := Transport(round5SuccessTransport()).RoundTrip(r)
					if err != nil {
						t.Fatal(err)
					}
					_, _ = io.Copy(io.Discard, resp.Body)
					_ = resp.Body.Close()
				}
				if err := ForceFlush(context.Background()); err != nil {
					t.Fatal(err)
				}
				var request *collector.ExportTraceServiceRequest
				select {
				case request = <-received:
				default:
					t.Fatal("no OTLP export after flush")
				}
				data, _ := proto.Marshal(request)
				for _, secret := range []string{"MAP_CREDENTIAL_PRIVATE", "FI_KEY_PRIVATE", "FI_SECRET_PRIVATE", "PROVIDER_HEADER_PRIVATE", "SLICE_SIGNED_PRIVATE", "NESTED_SECRET_PRIVATE", "STRUCTURED_PASSWORD_PRIVATE", "SLICE_TOKEN_PRIVATE", "EVENT_CREDENTIAL_PRIVATE", "YWJj", "QUJD"} {
					if strings.Contains(string(data), secret) {
						t.Fatalf("actual OTLP leaked %s", secret)
					}
				}
				if hidesContent(cfg) {
					for _, secret := range []string{"EVENT_INPUT_PRIVATE", "EVENT_OUTPUT_PRIVATE"} {
						if strings.Contains(string(data), secret) {
							t.Fatalf("structured exception leaked %s", secret)
						}
					}
				}
				if cfg.HideInputs && strings.Contains(string(data), "INPUT_PRIVATE") || cfg.HideOutputs && strings.Contains(string(data), "OUTPUT_PRIVATE") {
					t.Fatal("structured content bypassed hiding")
				}
				if !strings.Contains(string(data), "ordinary metadata") || !strings.Contains(string(data), "ProviderError") {
					t.Fatal("safe structured metadata/exception type lost")
				}
				ss := request.ResourceSpans[0].ScopeSpans[0]
				if len(ss.Spans) != 1 || len(ss.Spans[0].Links) != 1 {
					t.Fatal("library/wire deduplication or structured links changed")
				}
				foundMap := false
				for _, kv := range ss.Spans[0].Attributes {
					if kv.Key == "metadata" {
						foundMap = kv.Value.GetKvlistValue() != nil
					}
				}
				if !foundMap {
					t.Fatal("structured attribute type was flattened or dropped")
				}
				if len(app.Ended()) != 1 || !strings.Contains(attribute.Map("all", app.Ended()[0].Attributes()...).String(), "MAP_CREDENTIAL_PRIVATE") || !strings.Contains(app.Ended()[0].Events()[0].Attributes[1].String(), "EVENT_INPUT_PRIVATE") {
					t.Fatal("application-owned snapshot changed")
				}
			})
		}
	}
}

func round5SuccessTransport() http.RoundTripper {
	return round4TransportFunc(func(r *http.Request) (*http.Response, error) {
		if r.Body != nil {
			_, _ = io.Copy(io.Discard, r.Body)
			_ = r.Body.Close()
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(round4Response)), ContentLength: -1}, nil
	})
}

func TestErrorMessageContentHiding(t *testing.T) {
	for _, tc := range []struct {
		name string
		cfg  TraceConfig
	}{
		{"inputs", TraceConfig{HideInputs: true}}, {"outputs", TraceConfig{HideOutputs: true}},
		{"input-text", TraceConfig{HideInputText: true}}, {"output-text", TraceConfig{HideOutputText: true}},
		{"input-messages", TraceConfig{HideInputMessages: true}}, {"output-messages", TraceConfig{HideOutputMessages: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ownedRegressionSetup(t)
			app, rec := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
			p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(app))
			defer p.Shutdown(context.Background())
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(rec), WithTraceConfig(tc.cfg)); err != nil {
				t.Fatal(err)
			}
			_, span := p.Tracer("library").Start(context.Background(), "library", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM"), attribute.String("error.message", "provider rejected PRIVATE_PROMPT")))
			span.SetStatus(codes.Error, "provider rejected PRIVATE_PROMPT")
			span.End()
			if len(rec.Ended()) != 1 || rec.Ended()[0].Status().Description != "" {
				t.Fatal("missing span or hidden status leaked")
			}
			found := false
			for _, kv := range rec.Ended()[0].Attributes() {
				if kv.Key == "error.message" {
					found = kv.Value.AsString() == RedactedValue
				}
			}
			if !found || !strings.Contains(attribute.Map("app", app.Ended()[0].Attributes()...).String(), "PRIVATE_PROMPT") || !strings.Contains(app.Ended()[0].Status().Description, "PRIVATE_PROMPT") {
				t.Fatal("error.message leaked or application snapshot changed")
			}
		})
	}
}

func TestPanicUploadInstrumentationCleanup(t *testing.T) {
	for _, mode := range []string{"transport", "get-body", "reader"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
				t.Fatal(err)
			}
			token := &struct{ label string }{"original application panic"}
			var observers []*requestObserver
			base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
				observers = append(observers, r.Body.(*requestBody).observer)
				if mode == "reader" {
					_, _ = r.Body.Read(make([]byte, 1))
				} else {
					b, _ := r.GetBody()
					observers = append(observers, b.(*requestBody).observer)
				}
				panic(token)
			})
			tr := Transport(base).(*wireTransport)
			for i := 0; i < 3; i++ {
				r, _ := http.NewRequest("POST", "https://api.openai.com/v1/audio/transcriptions", strings.NewReader("multipart upload"))
				r.Header.Set("Content-Type", "multipart/form-data; boundary=b")
				if mode == "get-body" {
					r.GetBody = func() (io.ReadCloser, error) { panic(token) }
				}
				if mode == "reader" {
					r.Body = &round5PanicUploadBody{token: token}
				}
				var got any
				func() {
					defer func() { got = recover() }()
					_, _ = tr.RoundTrip(r)
				}()
				if got != token {
					t.Fatalf("original panic changed: %v", got)
				}
				tr.mu.Lock()
				tracked := len(tr.requests)
				tr.mu.Unlock()
				if tracked != 0 {
					t.Fatal("recovered application panic retained cancellation mapping")
				}
				for _, o := range observers {
					select {
					case <-o.done:
					default:
						t.Fatal("multipart parser still live after panic unwinding")
					}
					o.mu.Lock()
					released := o.released && o.capture.data == nil && o.fields == nil && o.files == nil
					o.mu.Unlock()
					if !released {
						t.Fatal("panic retained capture")
					}
				}
				if mode == "reader" && r.Body.(*round5PanicUploadBody).closed {
					t.Fatal("instrumentation closed application's body")
				}
			}
		})
	}
}

type round5PanicUploadBody struct {
	token  any
	closed bool
}

func (b *round5PanicUploadBody) Read([]byte) (int, error) { panic(b.token) }
func (b *round5PanicUploadBody) Close() error             { b.closed = true; return nil }

func TestUnusedGetBodyReplayPreservesOriginalCapture(t *testing.T) {
	for _, multipart := range []bool{false, true} {
		for _, order := range []string{"before-original", "after-original"} {
			t.Run(map[bool]string{false: "json", true: "multipart"}[multipart]+"/"+order, func(t *testing.T) {
				ownedRegressionSetup(t)
				rec := tracetest.NewSpanRecorder()
				if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
					t.Fatal(err)
				}
				payload, ct, path := round4Request, "application/json", "/v1/chat/completions"
				if multipart {
					payload = "--b\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\nAUDIO\r\n--b--\r\n"
					ct, path = "multipart/form-data; boundary=b", "/v1/audio/transcriptions"
				}
				base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
					unused := func() {
						b, err := r.GetBody()
						if err != nil {
							t.Fatal(err)
						}
						if err := b.Close(); err != nil {
							t.Fatal(err)
						}
					}
					if order == "before-original" {
						unused()
					}
					got, err := io.ReadAll(r.Body)
					if err != nil || string(got) != payload {
						t.Fatal("application upload changed")
					}
					_ = r.Body.Close()
					if order == "after-original" {
						unused()
					}
					return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"text":"hello","choices":[{"message":{"role":"assistant","content":"hello"}}]}`))}, nil
				})
				r, _ := http.NewRequest("POST", "https://proxy.example"+path, strings.NewReader(payload))
				r.Header.Set("Content-Type", ct)
				resp, err := Transport(base).RoundTrip(r)
				if err != nil {
					t.Fatal(err)
				}
				got, err := io.ReadAll(resp.Body)
				if err != nil || !strings.Contains(string(got), "hello") {
					t.Fatal("application response changed")
				}
				_ = resp.Body.Close()
				if len(rec.Ended()) != 1 {
					t.Fatalf("valid proxy capture dropped: spans=%d", len(rec.Ended()))
				}
				attrs := map[string]attribute.Value{}
				for _, kv := range rec.Ended()[0].Attributes() {
					attrs[string(kv.Key)] = kv.Value
				}
				if _, unavailable := attrs["traceai.wire.body_unavailable"]; unavailable {
					t.Fatal("unused replay replaced authoritative original")
				}
				if multipart && attrs["traceai.audio.input_bytes"].AsInt64() != 5 {
					t.Fatal("original multipart capture lost")
				}
				if !multipart && attrs["gen_ai.input.messages.0.message.content"].AsString() != "hi" {
					t.Fatal("original JSON capture lost")
				}
			})
		}
	}
}

func TestApplicationServiceResourcePreserved(t *testing.T) {
	for _, mode := range []string{"inferred", "environment", "option", "missing-resource"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			t.Setenv("OTEL_SERVICE_NAME", "")
			app, rec := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
			res := resource.NewSchemaless(attribute.String("service.name", "checkout-api"))
			if mode == "missing-resource" {
				res = resource.Empty()
			}
			p := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSpanProcessor(app))
			defer p.Shutdown(context.Background())
			opts := []Option{WithTracerProvider(p), WithSpanProcessor(rec)}
			// A6 (spec 2026-10-30, Go fix 2026-10-31): an application-supplied
			// service.name is kept exactly in the FI view; neither the agent's
			// environment nor its options replace a host resource identity.
			want := "checkout-api"
			if mode == "environment" {
				t.Setenv("OTEL_SERVICE_NAME", "explicit-agent")
			}
			if mode == "option" {
				c, err := readConfig()
				if err != nil {
					t.Fatal(err)
				}
				c.ServiceName = "option-agent"
				opts = append(opts, WithConfig(c))
			}
			if _, err := InitForTest(context.Background(), opts...); err != nil {
				t.Fatal(err)
			}
			if mode == "missing-resource" {
				want = agent.current.config.ServiceName
			}
			_, span := p.Tracer("application").Start(context.Background(), "application")
			span.End()
			if len(rec.Ended()) != 1 || len(app.Ended()) != 1 {
				t.Fatal("span delivery changed")
			}
			got, _ := rec.Ended()[0].Resource().Set().Value("service.name")
			if got.AsString() != want {
				t.Fatalf("agent service.name=%q want %q", got.AsString(), want)
			}
			original, ok := app.Ended()[0].Resource().Set().Value("service.name")
			if mode != "missing-resource" && original.AsString() != "checkout-api" || mode == "missing-resource" && ok {
				t.Fatal("application resource mutated")
			}
		})
	}
}

func TestOwnedServiceFallbackReconfiguration(t *testing.T) {
	ownedRegressionSetup(t)
	t.Setenv("OTEL_SERVICE_NAME", "previous-agent-service")
	rec := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	p := agent.current.provider
	cached := p.Tracer("cached")
	t.Setenv("OTEL_SERVICE_NAME", "")
	second := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(second)); err != nil {
		t.Fatal(err)
	}
	_, span := cached.Start(context.Background(), "after-reconfigure")
	span.End()
	if len(rec.Ended()) != 1 {
		t.Fatal("cached tracer lost delivery")
	}
	got, _ := rec.Ended()[0].Resource().Set().Value("service.name")
	if got.AsString() != agent.current.config.ServiceName || got.AsString() != "previous-agent-service" {
		t.Fatalf("agent-owned resource retained stale service name: %q", got.AsString())
	}
}

func TestCollectorRawQueryPreserved(t *testing.T) {
	for _, query := range []string{"tenant=checkout", "tenant=a%2Fb&tenant=second&flag&plus=a+b&encoded=%26%3D", ""} {
		t.Run(query, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			received := make(chan string, 1)
			sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				received <- r.RequestURI
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "application/x-protobuf")
			}))
			defer sink.Close()
			t.Setenv("FI_BASE_URL", sink.URL+"/base/?"+query+"#ignored")
			if _, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_KEY", "DUMMY_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			exportRegressionSpan(t, p)
			select {
			case got := <-received:
				if want := "/base/tracer/v1/traces?" + query; got != want {
					t.Fatalf("collector URI=%q want %q", got, want)
				}
			default:
				t.Fatal("collector received no request after flush")
			}
		})
	}
}

func TestMalformedLiteralStringConfigReportsFailure(t *testing.T) {
	t.Setenv("FI_API_KEY", "")
	if err := os.Unsetenv("FI_API_KEY"); err != nil {
		t.Fatal(err)
	}
	for _, active := range []bool{false, true} {
		t.Run(map[bool]string{false: "fresh", true: "replacement"}[active], func(t *testing.T) {
			ownedRegressionSetup(t)
			var previous *agentState
			if active {
				if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
					t.Fatal(err)
				}
				previous = agent.current
			}
			path := filepath.Join(t.TempDir(), "bad.toml")
			t.Setenv("FI_CONFIG_FILE", path)
			for _, value := range []string{"'", "'unterminated", "\""} {
				if err := os.WriteFile(path, []byte("api_key = "+value+"\n"), 0600); err != nil {
					t.Fatal(err)
				}
				_, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder()))
				if active {
					if err != nil || agent.current != previous || Report().Integrations[0].Status != "instrumented" {
						t.Fatal("second init changed active agent")
					}
					continue
				}
				if err == nil {
					t.Fatalf("malformed literal %q accepted", value)
				}
				r := Report()
				if _, err := json.Marshal(r); err != nil {
					t.Fatal(err)
				}
				if len(r.Integrations) == 0 || r.Integrations[0].Status != "failed" || r.Config.ConfigFile != path {
					t.Fatalf("malformed config reported healthy/stale: %+v", r)
				}
				if agent.current != previous || previous != nil && !previous.active.Load() {
					t.Fatal("failed config disturbed active instrumentation")
				}
			}
		})
	}
	for _, value := range []string{"''", "'literal#value'"} {
		path := filepath.Join(t.TempDir(), "valid.toml")
		if err := os.WriteFile(path, []byte("api_key = "+value+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
		values, err := readFlatConfig(path, nil)
		if err != nil || values["API_KEY"] != strings.Trim(value, "'") {
			t.Fatalf("valid literal string rejected: %q %v", value, err)
		}
	}
}
