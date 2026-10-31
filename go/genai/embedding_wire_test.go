package traceaigenai_test

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
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
	testEmbeddingOTLPWire(t, 1, false)
}

func TestEmbeddingFullBatchOTLPWire(t *testing.T) {
	t.Run("success", func(t *testing.T) { testEmbeddingOTLPWire(t, 100, false) })
	t.Run("error", func(t *testing.T) { testEmbeddingOTLPWire(t, 100, true) })
}

func testEmbeddingOTLPWire(t *testing.T, batchSize int, apiError bool) {
	t.Helper()
	// Exercise the documented provider's default limits, not a raised test limit.
	t.Setenv("OTEL_ATTRIBUTE_COUNT_LIMIT", "")
	t.Setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "")
	for _, jsonWire := range []bool{false, true} {
		for _, compressed := range []bool{false, true} {
			for name, tc := range map[string]traceaigenai.TraceConfig{"capture": {}, "inputs": {HideInputs: true}, "messages": {HideInputMessages: true}, "text": {HideInputText: true}, "parameters": {HideLLMInvocationParameters: true}} {
				t.Run(fmt.Sprintf("json=%t/gzip=%t/%s", jsonWire, compressed, name), func(t *testing.T) {
					clearHideEnv(t)
					ctx := traceaigenai.ContextWithSessionID(context.Background(), "batch-session")
					ctx = traceaigenai.ContextWithUserID(ctx, "batch-user")
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
						if r.URL.Path != "/v1beta/models/gemini-embedding-001:batchEmbedContents" || r.Header.Get("x-goog-api-key") != testGeminiKey {
							t.Error("wrong SDK endpoint or credentials")
						}
						var request struct {
							Requests []json.RawMessage `json:"requests"`
						}
						if err := json.NewDecoder(r.Body).Decode(&request); err != nil || len(request.Requests) != batchSize {
							t.Errorf("SDK batch changed: count=%d err=%v", len(request.Requests), err)
						}
						w.Header().Set("Content-Type", "application/json")
						if apiError {
							w.WriteHeader(http.StatusBadRequest)
							_, _ = io.WriteString(w, `{"error":{"code":400,"status":"INVALID_ARGUMENT","message":"invalid embedding input"}}`)
							return
						}
						embeddings := make([]map[string]any, batchSize)
						for i := range embeddings {
							embeddings[i] = map[string]any{"values": []float64{0.125, 0.875}}
						}
						if err := json.NewEncoder(w).Encode(map[string]any{"embeddings": embeddings}); err != nil {
							t.Error(err)
						}
					}))
					defer model.Close()
					client, err := genai.NewClient(ctx, &genai.ClientConfig{APIKey: testGeminiKey, Backend: genai.BackendGeminiAPI, HTTPClient: traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp), traceaigenai.WithTraceConfig(tc)), HTTPOptions: genai.HTTPOptions{BaseURL: model.URL + "/"}})
					if err != nil {
						t.Fatal(err)
					}
					inputs := make([]*genai.Content, batchSize)
					for i := range inputs {
						inputs[i] = genai.NewContentFromText(fmt.Sprintf("private-wire-input-%03d", i), genai.RoleUser)
					}
					response, err := client.Models.EmbedContent(ctx, "gemini-embedding-001", inputs, &genai.EmbedContentConfig{Title: "private-wire-title", TaskType: "RETRIEVAL_DOCUMENT", OutputDimensionality: genai.Ptr[int32](2)})
					if apiError {
						if err == nil {
							t.Fatal("SDK error lost")
						}
					} else {
						if err != nil {
							t.Fatal(err)
						}
						if len(response.Embeddings) != batchSize {
							t.Fatal("SDK response count changed")
						}
						for _, embedding := range response.Embeddings {
							if len(embedding.Values) != 2 || embedding.Values[0] != 0.125 || embedding.Values[1] != 0.875 {
								t.Fatal("SDK vector changed")
							}
						}
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
						if a[kv.Key] == "" {
							a[kv.Key] = fmt.Sprint(kv.Value.GetIntValue())
						}
					}
					if s.DroppedAttributesCount != 0 {
						t.Errorf("default provider dropped %d attributes", s.DroppedAttributesCount)
					}
					want := map[string]string{
						"gen_ai.span.kind": "EMBEDDING", "gen_ai.operation.name": "embeddings",
						"gen_ai.provider.name": "google", "gen_ai.request.model": "gemini-embedding-001",
						"embedding.model_name": "gemini-embedding-001", "session.id": "batch-session", "user.id": "batch-user",
					}
					if apiError {
						want["http.response.status_code"] = "400"
						want["error.type"] = "INVALID_ARGUMENT"
						if !tc.HideInputs {
							want["error.message"] = "invalid embedding input"
						}
						if s.Status.GetCode() != tracepb.Status_STATUS_CODE_ERROR || a["embedding.count"] != "" {
							t.Error("wrong error semantics")
						}
					} else {
						want["http.response.status_code"] = "200"
						want["embedding.count"] = fmt.Sprint(batchSize)
						want["gen_ai.embeddings.dimension.count"] = "2"
					}
					for key, value := range want {
						if a[key] != value {
							t.Errorf("%s=%q want=%q", key, a[key], value)
						}
					}
					if tc.HideInputs {
						if a["input.value"] != traceaigenai.RedactedValue || a["error.message"] != "" {
							t.Error("input or error privacy changed")
						}
					} else {
						var messages []struct{ Role, Content string }
						if err := json.Unmarshal([]byte(a["input.value"]), &messages); err != nil || len(messages) != batchSize {
							t.Errorf("aggregate input lost: count=%d err=%v", len(messages), err)
						}
						for i, message := range messages {
							text := fmt.Sprintf("private-wire-input-%03d", i)
							if tc.HideInputText {
								text = traceaigenai.RedactedValue
							}
							if message.Role != "user" || message.Content != text {
								t.Errorf("aggregate document %d changed", i)
							}
						}
					}
					if !tc.HideLLMInvocationParameters {
						var params struct{ Requests []map[string]any }
						if err := json.Unmarshal([]byte(a["gen_ai.request.parameters"]), &params); err != nil || len(params.Requests) != batchSize {
							t.Errorf("batch parameters lost: count=%d err=%v", len(params.Requests), err)
						}
					}
					if tc.HideInputs || tc.HideInputMessages {
						for key := range a {
							if strings.HasPrefix(key, "gen_ai.input.messages.") {
								t.Error("hidden flattened input on wire")
							}
						}
					} else if batchSize > 32 {
						if a["gen_ai.input.messages.31.message.role"] != "user" || a["gen_ai.input.messages.32.message.role"] != "" {
							t.Error("flattened document preview is not bounded to 32 messages")
						}
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
