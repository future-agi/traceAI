package auto

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/proto"
)

func TestS6HiddenImageReferencesOTLP(t *testing.T) {
	const imageURI = "http://127.0.0.1/images/PRIVATE_IMAGE_REFERENCE.png"
	const safeURI = "http://127.0.0.1/safe-neighbor"
	const root = "gen_ai.input.messages.0.parts.0"
	for _, tc := range []struct {
		name  string
		attrs []attribute.KeyValue
	}{
		{"flat-mime", []attribute.KeyValue{attribute.String(root+".source.mimeType", "image/png"), attribute.String(root+".source.url", imageURI)}},
		{"flat-type", []attribute.KeyValue{attribute.String(root+".type", "image"), attribute.String(root+".source.url", imageURI)}},
		{"bracket-type", []attribute.KeyValue{attribute.String("gen_ai.input.messages[0].parts[0].type", "input_image"), attribute.String(root+".source.url", imageURI)}},
		{"indexed-mime", []attribute.KeyValue{attribute.String(root+".source.mimeType.0", "image/png; charset=binary"), attribute.StringSlice(root+".source.url", []string{imageURI})}},
		{"array-mime", []attribute.KeyValue{attribute.StringSlice(root+".source.mimeType", []string{"text/plain", "image/png"}), attribute.String(root+".source.url", imageURI)}},
		{"mixed-native-mime-flat-url", []attribute.KeyValue{attribute.Map(root+".source", attribute.String("mimeType", "image/png")), attribute.String(root+".source.url", imageURI)}},
		{"mixed-json-mime-flat-url", []attribute.KeyValue{attribute.String(root+".source", `{"mimeType":"image/png"}`), attribute.String(root+".source.url", imageURI)}},
		{"mixed-native-type-flat-url", []attribute.KeyValue{attribute.Map(root, attribute.String("type", "image")), attribute.String(root+".source.url", imageURI)}},
		{"mixed-json-type-flat-url", []attribute.KeyValue{attribute.String(root, `{"type":"image"}`), attribute.String(root+".source.url", imageURI)}},
		{"mixed-flat-mime-native-url", []attribute.KeyValue{attribute.String(root+".source.mimeType", "image/png"), attribute.Map(root+".source", attribute.String("url", imageURI))}},
		{"mixed-flat-mime-json-url", []attribute.KeyValue{attribute.String(root+".source.mimeType", "image/png"), attribute.String(root+".source", `{"url":"`+imageURI+`"}`)}},
		{"native", []attribute.KeyValue{attribute.Map("input.value", attribute.Slice("parts", attribute.MapValue(attribute.String("type", "image"), attribute.Map("source", attribute.String("url", imageURI)))))}},
		{"json", []attribute.KeyValue{attribute.String("input.value", `{"parts":[{"type":"image","source":{"url":"`+imageURI+`"}}]}`)}},
		{"nested-flat", []attribute.KeyValue{attribute.Map("input.value", attribute.String("parts[0].type", "image_url"), attribute.String("parts.0.source.url", imageURI), attribute.String("parts.1.source.url", safeURI))}},
	} {
		for _, hidden := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/hide=%t", tc.name, hidden), func(t *testing.T) {
				exporterRegressionSetup(t)
				received := privacyCollector(t)
				attrs := append(append([]attribute.KeyValue(nil), tc.attrs...), attribute.String("gen_ai.input.messages.0.parts.1.source.url", safeURI), attribute.String("gen_ai.input.messages.0.parts.10.source.url", safeURI))
				app := tracetest.NewSpanRecorder()
				p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attrs...)), sdktrace.WithSpanProcessor(app))
				defer p.Shutdown(context.Background())
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("S6_IMAGE_FI_KEY", "S6_IMAGE_FI_SECRET"), WithTraceConfig(TraceConfig{HideInputImages: hidden})); err != nil {
					t.Fatal(err)
				}
				parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}})
				_, span := p.Tracer("image-policy", trace.WithInstrumentationAttributes(attrs...)).Start(context.Background(), "image-policy", trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: attrs}))
				span.AddEvent("image-policy", trace.WithAttributes(attrs...))
				span.End()
				host := app.Ended()[0]
				before := fmt.Sprint(host.Attributes(), host.Resource().Attributes(), host.InstrumentationScope(), host.Events(), host.Links())
				request := flushedPrivacyExport(t, received)
				if len(request.ResourceSpans) != 1 || len(request.ResourceSpans[0].ScopeSpans) != 1 || len(request.ResourceSpans[0].ScopeSpans[0].Spans) != 1 {
					t.Fatal("image policy lost span identity")
				}
				rs := request.ResourceSpans[0]
				ss := rs.ScopeSpans[0]
				s := ss.Spans[0]
				hostID := host.SpanContext().SpanID()
				if len(s.Events) != 1 || len(s.Links) != 1 || !bytes.Equal(s.SpanId, hostID[:]) {
					t.Fatal("image policy lost span surfaces or identity")
				}
				for name, surface := range map[string]proto.Message{"resource": rs.Resource, "scope": ss.Scope, "attrs": s, "event": s.Events[0], "link": s.Links[0]} {
					data, err := proto.Marshal(surface)
					if err != nil || bytes.Contains(data, []byte(imageURI)) == hidden || !bytes.Contains(data, []byte(safeURI)) {
						t.Fatalf("%s: image hide=%t violated or neighboring index over-redacted", name, hidden)
					}
				}
				if after := fmt.Sprint(host.Attributes(), host.Resource().Attributes(), host.InstrumentationScope(), host.Events(), host.Links()); before != after || !strings.Contains(after, imageURI) {
					t.Fatal("image policy mutated the host export view")
				}
			})
		}
	}
}

func TestS6LongCaptureKeepsSpan(t *testing.T) {
	for _, side := range []string{"input", "output"} {
		for _, size := range []int{512 * 1024, 1024 * 1024, 1600000, 2 * 1024 * 1024, wire.DefaultMaxBodyBytes - 1024} {
			t.Run(fmt.Sprintf("%s/%d", side, size), func(t *testing.T) {
				ownedRegressionSetup(t)
				host := tracetest.NewSpanRecorder()
				p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(host))
				defer p.Shutdown(context.Background())
				rec := tracetest.NewSpanRecorder()
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(rec)); err != nil {
					t.Fatal(err)
				}
				s6LongExchange(t, side, size)
				if len(host.Ended()) != 1 || len(rec.Ended()) != 1 {
					t.Fatalf("under-limit capture lost spans: host=%d agent=%d", len(host.Ended()), len(rec.Ended()))
				}
				s, original := rec.Ended()[0], host.Ended()[0]
				if !s.SpanContext().Equal(original.SpanContext()) || s.StartTime() != original.StartTime() || s.EndTime() != original.EndTime() || s.Status().Code != codes.Ok {
					t.Fatal("privacy fallback changed span identity, timing or outcome")
				}
				if s.StartTime().IsZero() || s.EndTime().Before(s.StartTime()) {
					t.Fatal("invalid wire span timing")
				}
				for key, want := range map[string]int64{"gen_ai.usage.input_tokens": 7, "gen_ai.usage.output_tokens": 3, "gen_ai.usage.total_tokens": 10} {
					if spanAttr(s, key).AsInt64() != want {
						t.Fatalf("privacy fallback lost %s", key)
					}
				}
				degraded := side == "input" && size >= 1600000 || side == "output" && size >= 2*1024*1024
				if spanAttr(s, "traceai.wire.privacy_degraded").AsBool() != degraded {
					t.Fatal("privacy-budget degradation was not reported accurately")
				}
				if degraded {
					for _, kv := range s.Attributes() {
						key := string(kv.Key)
						if strings.HasPrefix(key, "input.") || strings.HasPrefix(key, "output.") || strings.HasPrefix(key, "gen_ai.input.") || strings.HasPrefix(key, "gen_ai.output.") {
							t.Fatal("unchecked content survived privacy-budget exhaustion")
						}
					}
				}
			})
		}
	}
}

func s6LongExchange(t *testing.T, side string, size int) {
	t.Helper()
	input, output := "hi", "ok"
	if side == "input" {
		input = strings.Repeat("p", size)
	} else {
		output = strings.Repeat("r", size)
	}
	requestBody := []byte(`{"model":"m","messages":[{"role":"user","content":"` + input + `"}]}`)
	responseBody := []byte(`{"choices":[{"message":{"role":"assistant","content":"` + output + `"}}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}`)
	if len(requestBody) >= wire.DefaultMaxBodyBytes || len(responseBody) >= wire.DefaultMaxBodyBytes {
		t.Fatal("test request/response must be below the default capture limit")
	}
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, err := io.ReadAll(r.Body)
		if err != nil || !bytes.Equal(data, requestBody) {
			t.Error("provider received changed request")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(responseBody)
	}))
	defer provider.Close()
	native := &http.Transport{}
	defer native.CloseIdleConnections()
	client := &http.Client{Transport: Transport(native)}
	req, err := http.NewRequest("POST", provider.URL+"/v1/chat/completions", bytes.NewReader(requestBody))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	data, readErr := io.ReadAll(resp.Body)
	closeErr := resp.Body.Close()
	if readErr != nil || closeErr != nil || resp.StatusCode != http.StatusOK || !bytes.Equal(data, responseBody) {
		t.Fatal("agent changed application response")
	}
}

func TestS6LongCaptureOTLP(t *testing.T) {
	for _, side := range []string{"input", "output"} {
		for _, size := range []int{1600000, wire.DefaultMaxBodyBytes - 1024} {
			t.Run(fmt.Sprintf("%s/%d", side, size), func(t *testing.T) {
				p := exporterRegressionSetup(t)
				host := tracetest.NewSpanRecorder()
				p.RegisterSpanProcessor(host)
				received := privacyCollector(t)
				if _, err := InitForTest(context.Background(), WithCredentials("S6_LONG_FI_KEY", "S6_LONG_FI_SECRET")); err != nil {
					t.Fatal(err)
				}
				s6LongExchange(t, side, size)
				_, control := p.Tracer("healthy-exporter").Start(context.Background(), "control")
				control.End()
				request := flushedPrivacyExport(t, received)
				wires, controls := 0, 0
				for _, rs := range request.ResourceSpans {
					for _, ss := range rs.ScopeSpans {
						for _, span := range ss.Spans {
							if span.Name == "control" {
								controls++
							} else {
								wires++
							}
						}
					}
				}
				if len(host.Ended()) != 2 || wires != 1 || controls != 1 {
					t.Fatalf("healthy exporter lost wire capture: host=%d wire=%d control=%d", len(host.Ended()), wires, controls)
				}
			})
		}
	}
}
