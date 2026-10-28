package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	traceaiopenai "github.com/future-agi/traceAI/go/openai"
	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
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

const completion = `{"id":"chatcmpl-ex","object":"chat.completion","created":1,"model":"gpt-4.1-mini",
"choices":[{"index":0,"message":{"role":"assistant","content":"Sunny."},"finish_reason":"stop"}],
"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}`

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

// AC-10 and AC-11: the example's exporter sends both headers, the project
// resource attributes, protobuf over HTTP without compression, to
// {FI_BASE_URL}/tracer/v1/traces, and the LLM span reaches the collector only
// once the deferred shutdown flushes the batch.
func TestExampleExportCarriesHeadersResourceAndFlushesOnShutdown(t *testing.T) {
	sink := &otlpSink{}
	collector := httptest.NewServer(sink)
	defer collector.Close()
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Api-Key") != "" || r.Header.Get("X-Secret-Key") != "" {
			t.Error("Future AGI keys were sent to the model provider")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, completion)
	}))
	defer model.Close()

	t.Setenv("FI_API_KEY", "test-api-key")
	t.Setenv("FI_SECRET_KEY", "test-secret-key")
	t.Setenv("FI_PROJECT_NAME", "go-openai-example-test")
	t.Setenv("FI_BASE_URL", collector.URL+"/")
	t.Setenv("OTEL_RESOURCE_ATTRIBUTES", "")
	t.Setenv("OTEL_SERVICE_NAME", "")

	tp, err := newTracerProvider(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	client := openai.NewClient(
		option.WithBaseURL(model.URL+"/v1/"), option.WithAPIKey("sk-test"), option.WithMaxRetries(0),
		option.WithMiddleware(traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))),
	)
	if _, err := client.Chat.Completions.New(context.Background(), openai.ChatCompletionNewParams{
		Model:    "gpt-4.1-mini",
		Messages: []openai.ChatCompletionMessageParamUnion{openai.UserMessage("Weather in Paris?")},
	}); err != nil {
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
	if res["project_name"] != "go-openai-example-test" || res["project_type"] != "observe" || res["service.name"] != "go-openai-example-test" {
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
	if len(spans) != 1 || spans[0].GetName() != "chat gpt-4.1-mini" {
		t.Fatalf("exported spans = %v", spans)
	}
	attrs := map[string]string{}
	for _, kv := range spans[0].GetAttributes() {
		attrs[kv.GetKey()] = kv.GetValue().String()
		if strings.Contains(kv.GetValue().String(), "test-api-key") || strings.Contains(kv.GetValue().String(), "sk-test") {
			t.Errorf("credential in span attribute %s", kv.GetKey())
		}
	}
	if !strings.Contains(attrs["gen_ai.span.kind"], "LLM") || !strings.Contains(attrs["gen_ai.usage.input_tokens"], "5") {
		t.Errorf("span attributes = %v", attrs)
	}
}
