package traceaigenai_test

import (
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/genai"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// The stock Go HTTP exporter sends protobuf. This test-only client uses the
// same stock OTLP span transformation with JSON encoding to check wire parity;
// it is not a customer exporter or a claim of JSON support in otlptracehttp.
type embeddingJSONClient struct {
	endpoint   string
	compressed bool
}

func (c embeddingJSONClient) Start(context.Context) error { return nil }
func (c embeddingJSONClient) Stop(context.Context) error  { return nil }
func (c embeddingJSONClient) UploadTraces(ctx context.Context, spans []*tracepb.ResourceSpans) error {
	body, err := protojson.Marshal(&collectortrace.ExportTraceServiceRequest{ResourceSpans: spans})
	if err != nil {
		return err
	}
	if c.compressed {
		var b bytes.Buffer
		w := gzip.NewWriter(&b)
		if _, err = w.Write(body); err != nil {
			return err
		}
		if err = w.Close(); err != nil {
			return err
		}
		body = b.Bytes()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Api-Key", "synthetic-fi-key")
	req.Header.Set("X-Secret-Key", "synthetic-fi-secret")
	if c.compressed {
		req.Header.Set("Content-Encoding", "gzip")
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("collector status %d", res.StatusCode)
	}
	return nil
}

func TestEmbeddingOTLPWire(t *testing.T) {
	for _, jsonWire := range []bool{false, true} {
		for _, compressed := range []bool{false, true} {
			for name, tc := range map[string]traceaigenai.TraceConfig{"capture": {}, "inputs": {HideInputs: true}, "text": {HideInputText: true}, "parameters": {HideLLMInvocationParameters: true}} {
				t.Run(fmt.Sprintf("json=%t/gzip=%t/%s", jsonWire, compressed, name), func(t *testing.T) {
					clearHideEnv(t)
					ctx := context.Background()
					received := make(chan *collectortrace.ExportTraceServiceRequest, 1)
					collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Method != http.MethodPost || r.URL.Path != "/tracer/v1/traces" || r.Header.Get("X-Api-Key") != "synthetic-fi-key" || r.Header.Get("X-Secret-Key") != "synthetic-fi-secret" {
							t.Error("wrong export endpoint or authentication")
						}
						if (r.Header.Get("Content-Encoding") == "gzip") != compressed {
							t.Error("wrong compression")
						}
						var reader io.Reader = r.Body
						if compressed {
							gz, err := gzip.NewReader(reader)
							if err != nil {
								t.Error(err)
								w.WriteHeader(400)
								return
							}
							defer gz.Close()
							reader = gz
						}
						raw, err := io.ReadAll(reader)
						if err != nil {
							t.Error(err)
							w.WriteHeader(400)
							return
						}
						msg := &collectortrace.ExportTraceServiceRequest{}
						contentType := "application/x-protobuf"
						if jsonWire {
							contentType = "application/json"
							err = protojson.Unmarshal(raw, msg)
						} else {
							err = proto.Unmarshal(raw, msg)
						}
						if r.Header.Get("Content-Type") != contentType || err != nil {
							t.Errorf("invalid %s export: %v", contentType, err)
							w.WriteHeader(400)
							return
						}
						received <- msg
						w.Header().Set("Content-Type", contentType)
						if jsonWire {
							_, _ = io.WriteString(w, "{}")
						}
					}))
					defer collector.Close()
					var exporter *otlptrace.Exporter
					var err error
					if jsonWire {
						exporter, err = otlptrace.New(ctx, embeddingJSONClient{collector.URL + "/tracer/v1/traces", compressed})
					} else {
						compression := otlptracehttp.NoCompression
						if compressed {
							compression = otlptracehttp.GzipCompression
						}
						exporter, err = otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(collector.URL+"/tracer/v1/traces"), otlptracehttp.WithCompression(compression), otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": "synthetic-fi-key", "X-Secret-Key": "synthetic-fi-secret"}))
					}
					if err != nil {
						t.Fatal(err)
					}
					tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(resource.NewSchemaless(attribute.String("project_name", "embedding-wire"), attribute.String("project_type", "observe"))))
					defer tp.Shutdown(ctx)
					model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Header.Get("X-Api-Key") != "" || r.Header.Get("X-Secret-Key") != "" {
							t.Error("collector credentials sent to Gemini")
						}
						w.Header().Set("Content-Type", "application/json")
						_, _ = io.WriteString(w, `{"embeddings":[{"values":[0.125,0.875]}]}`)
					}))
					defer model.Close()
					client, err := genai.NewClient(ctx, &genai.ClientConfig{APIKey: testGeminiKey, Backend: genai.BackendGeminiAPI, HTTPClient: traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp), traceaigenai.WithTraceConfig(tc)), HTTPOptions: genai.HTTPOptions{BaseURL: model.URL + "/"}})
					if err != nil {
						t.Fatal(err)
					}
					_, err = client.Models.EmbedContent(ctx, "gemini-embedding-001", genai.Text("private-wire-input"), &genai.EmbedContentConfig{Title: "private-wire-title", TaskType: "RETRIEVAL_DOCUMENT", OutputDimensionality: genai.Ptr[int32](2)})
					if err != nil {
						t.Fatal(err)
					}
					if err = tp.Shutdown(ctx); err != nil {
						t.Fatal(err)
					}
					var msg *collectortrace.ExportTraceServiceRequest
					select {
					case msg = <-received:
					default:
						t.Fatal("no OTLP export")
					}
					if len(msg.ResourceSpans) != 1 || len(msg.ResourceSpans[0].ScopeSpans) != 1 || len(msg.ResourceSpans[0].ScopeSpans[0].Spans) != 1 {
						t.Fatalf("wrong export shape: %v", msg)
					}
					rs := msg.ResourceSpans[0]
					scope := rs.ScopeSpans[0]
					s := scope.Spans[0]
					if s.Name != "embeddings gemini-embedding-001" || s.Kind != tracepb.Span_SPAN_KIND_CLIENT || scope.Scope.Name != traceaigenai.ScopeName {
						t.Error("wrong span or scope")
					}
					a := map[string]string{}
					for _, kv := range s.Attributes {
						a[kv.Key] = kv.Value.GetStringValue()
					}
					if a["gen_ai.span.kind"] != "EMBEDDING" || a["gen_ai.operation.name"] != "embeddings" {
						t.Error("embedding semantics missing")
					}
					project := ""
					for _, kv := range rs.Resource.Attributes {
						if kv.Key == "project_name" {
							project = kv.Value.GetStringValue()
						}
					}
					if project != "embedding-wire" {
						t.Error("project resource missing")
					}
					encoded, _ := protojson.Marshal(msg)
					all := string(encoded)
					for _, forbidden := range []string{testGeminiKey, "synthetic-fi-key", "synthetic-fi-secret", "private-wire-title", "0.125"} {
						if strings.Contains(all, forbidden) {
							t.Errorf("wire leaked %s", forbidden)
						}
					}
					if tc.HideInputs || tc.HideInputText {
						if strings.Contains(all, "private-wire-input") {
							t.Error("hidden input on wire")
						}
					} else if !strings.Contains(all, "private-wire-input") {
						t.Error("captured input absent")
					}
					if tc.HideLLMInvocationParameters && strings.Contains(all, "gen_ai.request.parameters") {
						t.Error("hidden parameters on wire")
					}
				})
			}
		}
	}
}
