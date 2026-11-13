package auto

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
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

func privacyCollector(t *testing.T) <-chan *collector.ExportTraceServiceRequest {
	t.Helper()
	received := make(chan *collector.ExportTraceServiceRequest, 32)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, err := io.ReadAll(r.Body)
		request := &collector.ExportTraceServiceRequest{}
		if err != nil || proto.Unmarshal(data, request) != nil {
			t.Error("invalid OTLP request")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received <- request
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	t.Cleanup(sink.Close)
	t.Setenv("FI_BASE_URL", sink.URL)
	return received
}

func flushedPrivacyExport(t *testing.T, received <-chan *collector.ExportTraceServiceRequest) *collector.ExportTraceServiceRequest {
	t.Helper()
	if err := ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	combined := &collector.ExportTraceServiceRequest{}
	for {
		select {
		case request := <-received:
			combined.ResourceSpans = append(combined.ResourceSpans, request.ResourceSpans...)
		default:
			if len(combined.ResourceSpans) == 0 {
				t.Fatal("collector received no OTLP export")
			}
			return combined
		}
	}
}

func TestAudioAttributeRepresentationsOTLPPrivacy(t *testing.T) {
	payload := shortWAV()
	source := `{"bytes":"` + payload + `"}`
	for _, tc := range []struct {
		name string
		attr attribute.KeyValue
	}{
		{"canonical-control", attribute.Map("input.value", attribute.Map("audio", attribute.Map("source", attribute.String("bytes", payload))))},
		{"flat-scalar", attribute.String("gen_ai.input.messages.0.audio.source.bytes", payload)},
		{"flat-slice", attribute.StringSlice("input.audio.source.bytes", []string{payload, payload})},
		{"flat-native-slice", attribute.Slice("output.audio.source.bytes", attribute.StringValue(payload))},
		{"flat-source-json", attribute.String("input.audio.source", source)},
		{"nested-json-string", attribute.Map("input.value.audio", attribute.String("source", source))},
		{"nested-json-string-array", attribute.Map("input.value", attribute.Map("audio", attribute.StringSlice("source", []string{source})))},
		{"json-in-json", attribute.String("input.value", `{"audio":{"source":`+string(mustJSON(t, source))+`}}`)},
		{"json-in-native-array", attribute.Map("input.value", attribute.Slice("audio", attribute.MapValue(attribute.String("source", source))))},
		{"mime-json-string", attribute.Map("input.value", attribute.Map("inlineData", attribute.String("mimeType", "audio/wav"), attribute.String("source", source)))},
	} {
		t.Run(tc.name, func(t *testing.T) {
			exporterRegressionSetup(t)
			attrs := []attribute.KeyValue{tc.attr, attribute.String("gen_ai.audio.mime_type", "audio/wav"), attribute.String("gen_ai.audio.transcript", "safe transcript"), attribute.String("input.audio.format", "wav")}
			p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attrs...)))
			defer p.Shutdown(context.Background())
			app := tracetest.NewSpanRecorder()
			p.RegisterSpanProcessor(app)
			received := privacyCollector(t)
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("DUMMY_FI_KEY", "DUMMY_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}})
			_, span := p.Tracer("library", trace.WithInstrumentationAttributes(attrs...)).Start(context.Background(), "audio-representation", trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: attrs}))
			span.AddEvent("media", trace.WithAttributes(attrs...))
			span.End()
			data, err := proto.Marshal(flushedPrivacyExport(t, received))
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(data), payload) || strings.Contains(string(data), payload[:32]) {
				t.Fatal("collector-received OTLP retains raw WAV")
			}
			if !strings.Contains(string(data), "safe transcript") || !strings.Contains(string(data), "audio/wav") || !strings.Contains(string(data), "wav") {
				t.Fatal("flattened audio metadata/transcript lost")
			}
			if len(app.Ended()) != 1 || spanAttr(app.Ended()[0], string(tc.attr.Key)).Emit() != tc.attr.Value.Emit() || !strings.Contains(spanAttr(app.Ended()[0], string(tc.attr.Key)).Emit(), payload) {
				t.Fatal("application-owned snapshot mutated")
			}
		})
	}
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestRequestCredentialProcessRegistryOTLPPrivacy(t *testing.T) {
	const credential = "REQUEST_CHILD_HEADER_PRIVATE_1234"
	for _, kind := range []string{"LLM", "CHAIN"} {
		for _, header := range []string{"Authorization", "X-Api-Key", "signed-url"} {
			t.Run(kind+"/"+header, func(t *testing.T) {
				p := exporterRegressionSetup(t)
				app := tracetest.NewSpanRecorder()
				p.RegisterSpanProcessor(app)
				received := privacyCollector(t)
				if _, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_KEY", "DUMMY_FI_SECRET")); err != nil {
					t.Fatal(err)
				}
				provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					_, _ = io.Copy(io.Discard, r.Body)
					got := r.Header.Get(header)
					if header == "Authorization" {
						got = strings.TrimPrefix(got, "Bearer ")
					} else if header == "signed-url" {
						got = r.URL.Query().Get("token")
					}
					if got != credential {
						t.Error("request credential changed")
					}
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusUnauthorized)
					_, _ = io.WriteString(w, `{"error":{"message":"provider rejected `+got+`"}}`)
				}))
				defer provider.Close()
				ctx, parent := p.Tracer("library").Start(context.Background(), "request-parent", trace.WithAttributes(attribute.String("gen_ai.span.kind", kind)))
				nativeCtx, native := p.Tracer("native-sdk").Start(ctx, "native-sdk")
				r, _ := http.NewRequestWithContext(nativeCtx, "POST", provider.URL+"/v1/chat/completions", strings.NewReader(round4Request))
				if header == "signed-url" {
					r.URL.RawQuery = url.Values{"token": {credential}}.Encode()
				} else if header == "Authorization" {
					r.Header.Set(header, "Bearer "+credential)
				} else {
					r.Header.Set(header, credential)
				}
				response, err := http.DefaultClient.Do(r)
				if err != nil {
					t.Fatal(err)
				}
				body, readErr := io.ReadAll(response.Body)
				closeErr := response.Body.Close()
				if readErr != nil || closeErr != nil || response.StatusCode != http.StatusUnauthorized || !strings.Contains(string(body), credential) {
					t.Fatal("provider response changed")
				}
				native.End()
				record := func(span trace.Span) {
					span.SetAttributes(attribute.String("output.value", string(body)))
					span.SetStatus(codes.Error, "provider rejected "+credential)
					span.RecordError(errors.New(string(body)))
				}
				childCtx, child := p.Tracer("response-processing").Start(ctx, "response-processing-child")
				record(child)
				record(parent)
				parent.End()
				// The process registry survives ancestor retirement, independent of
				// which span/trace later handles an echoed response.
				_, grandchild := p.Tracer("response-processing").Start(childCtx, "response-processing-grandchild")
				record(grandchild)
				child.End()
				grandchild.End()
				_, unrelated := p.Tracer("application").Start(context.Background(), "unrelated")
				unrelated.SetAttributes(attribute.String("output.value", credential))
				unrelated.End()
				request := flushedPrivacyExport(t, received)
				count := 0
				for _, rs := range request.ResourceSpans {
					for _, ss := range rs.ScopeSpans {
						for _, span := range ss.Spans {
							count++
							data, err := proto.Marshal(span)
							if err != nil {
								t.Fatal(err)
							}
							leaks := strings.Contains(string(data), credential)
							if leaks {
								t.Fatalf("span=%s credential_present=%v", span.Name, leaks)
							}
						}
					}
				}
				want := 5
				if kind == "CHAIN" {
					want++ // Framework parents still allow one wire span.
				}
				if count != want || len(app.Ended()) != want {
					t.Fatalf("lost/duplicated spans: collector=%d application=%d want=%d", count, len(app.Ended()), want)
				}
				for _, span := range app.Ended() {
					if strings.HasPrefix(span.Name(), "response-processing-") && (!strings.Contains(span.Status().Description, credential) || !strings.Contains(spanAttr(span, "output.value").AsString(), credential) || !strings.Contains(span.Events()[0].Attributes[1].Value.Emit(), credential)) {
						t.Fatal("application-owned response/error snapshot mutated")
					}
				}
			})
		}
	}
}
