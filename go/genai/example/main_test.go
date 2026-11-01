package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/genai"
	"google.golang.org/protobuf/proto"
)

// otlpSink is a fake Future AGI collector that records every export request.
type otlpSink struct {
	mu   sync.Mutex
	reqs []export
}

type export struct {
	path, contentType, contentEncoding, apiKey, secretKey string
	body                                                  *collectortrace.ExportTraceServiceRequest
}

func (s *otlpSink) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	raw, _ := io.ReadAll(r.Body)
	msg := &collectortrace.ExportTraceServiceRequest{}
	if err := proto.Unmarshal(raw, msg); err != nil {
		http.Error(w, "not an OTLP protobuf export", http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	s.reqs = append(s.reqs, export{
		path: r.URL.Path, contentType: r.Header.Get("Content-Type"), contentEncoding: r.Header.Get("Content-Encoding"),
		apiKey: r.Header.Get("X-Api-Key"), secretKey: r.Header.Get("X-Secret-Key"), body: msg,
	})
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/x-protobuf")
	w.WriteHeader(http.StatusOK)
}

func (s *otlpSink) all() []export {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]export(nil), s.reqs...)
}

const textResponse = `{"candidates":[{"content":{"parts":[{"text":"Sunny."}],"role":"model"},"finishReason":"STOP","index":0}],
"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":2,"totalTokenCount":7},"modelVersion":"gemini-3.8-flash","responseId":"r1"}`

const callResponse = `{"candidates":[{"content":{"parts":[{"functionCall":{"name":"get_weather","args":{"city":"Paris"},"id":"call_1"},
"thoughtSignature":"c2ln"}],"role":"model"},"finishReason":"STOP","index":0}],
"usageMetadata":{"promptTokenCount":40,"candidatesTokenCount":10,"totalTokenCount":50},"modelVersion":"gemini-3.8-flash","responseId":"r0"}`

func TestNewTracerProviderRequiresKeysAndProject(t *testing.T) {
	for _, missing := range []string{"FI_API_KEY", "FI_SECRET_KEY", "FI_PROJECT_NAME"} {
		t.Setenv("FI_API_KEY", "k")
		t.Setenv("FI_SECRET_KEY", "s")
		t.Setenv("FI_PROJECT_NAME", "p")
		t.Setenv(missing, "")
		if _, err := newTracerProvider(context.Background()); err == nil {
			t.Errorf("no error with %s unset", missing)
		}
	}
}

// AC-11 and AC-12: the example's exporter sends both headers, the project
// resource attributes, protobuf over HTTP without compression, to
// {FI_BASE_URL}/tracer/v1/traces, and the LLM span reaches the collector only
// once the deferred shutdown flushes the batch.
func TestExampleExportCarriesHeadersResourceAndFlushesOnShutdown(t *testing.T) {
	sink := &otlpSink{}
	collector := httptest.NewServer(sink)
	defer collector.Close()
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Api-Key") != "" || r.Header.Get("X-Secret-Key") != "" {
			t.Error("Future AGI keys were sent to Gemini")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, textResponse)
	}))
	defer model.Close()

	t.Setenv("FI_API_KEY", "test-api-key")
	t.Setenv("FI_SECRET_KEY", "test-secret-key")
	t.Setenv("FI_PROJECT_NAME", "go-genai-example-test")
	t.Setenv("FI_BASE_URL", collector.URL+"/")
	t.Setenv("OTEL_RESOURCE_ATTRIBUTES", "")
	t.Setenv("OTEL_SERVICE_NAME", "")

	tp, err := newTracerProvider(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	client, err := genai.NewClient(context.Background(), &genai.ClientConfig{
		APIKey:      "AIza-test-gemini-key",
		Backend:     genai.BackendGeminiAPI,
		HTTPClient:  traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
		HTTPOptions: genai.HTTPOptions{BaseURL: model.URL + "/"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Models.GenerateContent(context.Background(), "gemini-3.8-flash", genai.Text("Weather in Paris?"), nil); err != nil {
		t.Fatal(err)
	}

	time.Sleep(200 * time.Millisecond)
	if n := len(sink.all()); n != 0 {
		t.Fatalf("batch exported %d requests before shutdown; the flush check would be meaningless", n)
	}
	shutdown(tp)

	reqs := sink.all()
	if len(reqs) != 1 {
		t.Fatalf("collector saw %d export requests after shutdown, want 1", len(reqs))
	}
	r := reqs[0]
	if r.path != "/tracer/v1/traces" {
		t.Errorf("path = %q", r.path)
	}
	if r.contentType != "application/x-protobuf" || r.contentEncoding != "" {
		t.Errorf("content type %q, encoding %q; the collector takes uncompressed protobuf", r.contentType, r.contentEncoding)
	}
	if r.apiKey != "test-api-key" || r.secretKey != "test-secret-key" {
		t.Error("X-Api-Key / X-Secret-Key missing or wrong")
	}
	rs := r.body.GetResourceSpans()
	if len(rs) != 1 {
		t.Fatalf("resource spans = %d", len(rs))
	}
	res := map[string]string{}
	for _, kv := range rs[0].GetResource().GetAttributes() {
		res[kv.GetKey()] = kv.GetValue().GetStringValue()
	}
	if res["project_name"] != "go-genai-example-test" || res["project_type"] != "observe" || res["service.name"] != "go-genai-example-test" {
		t.Errorf("resource attributes = %v", res)
	}
	for k, v := range res {
		if strings.HasPrefix(k, "host.") || strings.HasPrefix(k, "process.") || strings.HasPrefix(k, "os.") {
			t.Errorf("resource must not carry host/process/os detail, got %s", k)
		}
		if strings.Contains(v, "test-api-key") || strings.Contains(v, "test-secret-key") {
			t.Errorf("credential in resource attribute %s", k)
		}
	}
	spans := rs[0].GetScopeSpans()[0].GetSpans()
	if len(spans) != 1 || spans[0].GetName() != "generate_content gemini-3.8-flash" {
		t.Fatalf("exported spans = %v", spans)
	}
	attrs := map[string]string{}
	for _, kv := range spans[0].GetAttributes() {
		attrs[kv.GetKey()] = kv.GetValue().String()
		if strings.Contains(kv.GetValue().String(), "test-api-key") || strings.Contains(kv.GetValue().String(), "AIza-test") {
			t.Errorf("credential in span attribute %s", kv.GetKey())
		}
	}
	if !strings.Contains(attrs["gen_ai.span.kind"], "LLM") || !strings.Contains(attrs["gen_ai.usage.input_tokens"], "5") {
		t.Errorf("span attributes = %v", attrs)
	}
}

// The example's tool loop: LLM span (function call) -> tool span -> LLM span,
// and the function response goes back with the call id and the model turn
// (thought signature included) unchanged.
func TestToolLoopSpansAndHistory(t *testing.T) {
	var mu sync.Mutex
	var bodies []string
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, string(b))
		n := len(bodies)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		if n == 1 {
			_, _ = io.WriteString(w, callResponse)
			return
		}
		_, _ = io.WriteString(w, textResponse)
	}))
	defer model.Close()

	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	client, err := genai.NewClient(context.Background(), &genai.ClientConfig{
		APIKey:      "AIza-test-gemini-key",
		Backend:     genai.BackendGeminiAPI,
		HTTPClient:  traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
		HTTPOptions: genai.HTTPOptions{BaseURL: model.URL + "/"},
	})
	if err != nil {
		t.Fatal(err)
	}
	answer, err := askWithTool(context.Background(), client, tp.Tracer("t"), traceaigenai.TraceConfig{},
		"gemini-3.8-flash", "Weather in Paris?", "", false)
	if err != nil || answer != "Sunny." {
		t.Fatalf("answer %q err %v", answer, err)
	}
	var names []string
	for _, s := range rec.Ended() {
		names = append(names, s.Name())
	}
	if strings.Join(names, ",") != "generate_content gemini-3.8-flash,execute_tool get_weather,generate_content gemini-3.8-flash" {
		t.Fatalf("spans = %v", names)
	}
	var second struct {
		Contents []struct {
			Role  string           `json:"role"`
			Parts []map[string]any `json:"parts"`
		} `json:"contents"`
	}
	if err := json.Unmarshal([]byte(bodies[1]), &second); err != nil || len(second.Contents) != 3 {
		t.Fatalf("second request = %s", bodies[1])
	}
	if second.Contents[1].Parts[0]["thoughtSignature"] != "c2ln" {
		t.Error("the model turn must go back with its thought signature")
	}
	fr, _ := second.Contents[2].Parts[0]["functionResponse"].(map[string]any)
	if fr["id"] != "call_1" || fr["name"] != "get_weather" {
		t.Errorf("function response = %v", second.Contents[2].Parts[0])
	}
}
