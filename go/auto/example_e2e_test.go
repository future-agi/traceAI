package auto_test

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"

	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

// The shipped example, built unchanged, against a local OpenAI-compatible
// provider and a local Future AGI collector: one wire span is exported, the
// program's output is unchanged and the provider key never reaches the export.
func TestExampleExportsOneWireSpan(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "example")
	build := hostCommand("go", "build", "-o", bin, "./example")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build example %v %s", err, out)
	}
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		if r.Method != http.MethodPost || r.URL.Path != "/v1/chat/completions" {
			t.Errorf("unexpected provider request %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, responseJSON)
	}))
	defer provider.Close()
	var mu sync.Mutex
	var exported []byte
	var layers []string
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		var request collector.ExportTraceServiceRequest
		if err := proto.Unmarshal(data, &request); err != nil {
			t.Error(err)
		}
		mu.Lock()
		defer mu.Unlock()
		exported = append(exported, data...)
		for _, rs := range request.ResourceSpans {
			for _, ss := range rs.ScopeSpans {
				for _, s := range ss.Spans {
					for _, a := range s.Attributes {
						if a.Key == "traceai.capture.layer" {
							layers = append(layers, a.Value.GetStringValue())
						}
					}
				}
			}
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer sink.Close()
	cmd := hostCommand(bin)
	cmd.Dir = t.TempDir()
	cmd.Env = append(cleanEnv(), "FI_BASE_URL="+sink.URL, "FI_API_KEY=example-fi-key", "FI_SECRET_KEY=example-fi-secret",
		"OPENAI_BASE_URL="+provider.URL+"/v1", "OPENAI_API_KEY=EXAMPLE_PROVIDER_PRIVATE", "OPENAI_MODEL=test-model")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("example failed %v %s", err, stderr.String())
	}
	if stdout.String() != responseJSON+"\n" {
		t.Fatalf("example output changed: %q", stdout.String())
	}
	mu.Lock()
	defer mu.Unlock()
	if len(layers) != 1 || layers[0] != "wire" {
		t.Fatalf("want one wire span, got capture layers %v; stderr %s", layers, stderr.String())
	}
	if bytes.Contains(exported, []byte("EXAMPLE_PROVIDER_PRIVATE")) {
		t.Fatal("the provider key reached the export")
	}
}
