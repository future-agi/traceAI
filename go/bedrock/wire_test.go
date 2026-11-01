package traceaibedrock

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

// The executable is also the complete customer docs snippet. Only endpoint and
// credentials change in this test; the real SDK serializes/signs/decodes requests
// and the standard exporter sends protobuf OTLP to a loopback receiver.
func TestCustomerExampleOTLP(t *testing.T) {
	temp := t.TempDir()
	bin := filepath.Join(temp, "example")
	build := exec.Command("go", "build", "-o", bin, "./example")
	if b, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %s: %v", b, err)
	}
	awsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256") {
			t.Error("unsigned request")
		}
		if strings.HasSuffix(r.URL.Path, "converse-stream") {
			w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
			// Model latency before headers also avoids the native loopback request-write race.
			time.Sleep(50 * time.Millisecond)
			converseFrames(w)
		} else {
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, syncBody)
		}
	}))
	defer awsServer.Close()
	var mu sync.Mutex
	var spans []*tracepb.Span
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/tracer/v1/traces" || r.Method != "POST" {
			t.Error("not traces-only endpoint")
		}
		if r.Header.Get("X-Api-Key") != "synthetic-fi-api" || r.Header.Get("X-Secret-Key") != "synthetic-fi-secret" {
			t.Error("FI auth header mapping")
		}
		if r.Header.Get("Content-Encoding") != "" {
			t.Error("compressed request")
		}
		b, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		var req collector.ExportTraceServiceRequest
		if err := proto.Unmarshal(b, &req); err != nil {
			t.Error(err)
			return
		}
		for _, rs := range req.ResourceSpans {
			attrs := map[string]string{}
			for _, a := range rs.Resource.Attributes {
				attrs[a.Key] = a.Value.GetStringValue()
			}
			if attrs["project_name"] != "bedrock-go" || attrs["project_type"] != "observe" {
				t.Error("resource attributes missing")
			}
			for _, scope := range rs.ScopeSpans {
				if scope.Scope.Name != ScopeName || scope.Scope.Version != Version {
					t.Error("scope mismatch")
				}
				mu.Lock()
				spans = append(spans, scope.Spans...)
				mu.Unlock()
			}
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
		_, _ = w.Write([]byte{})
	}))
	defer receiver.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin)
	// No host credential/config discovery, proxy credentials or OTLP env leakage.
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + temp, "AWS_CONFIG_FILE=" + filepath.Join(temp, "config"), "AWS_SHARED_CREDENTIALS_FILE=" + filepath.Join(temp, "credentials"), "AWS_EC2_METADATA_DISABLED=true", "AWS_ACCESS_KEY_ID=synthetic-access", "AWS_SECRET_ACCESS_KEY=synthetic-secret", "AWS_REGION=us-east-1", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME=" + awsServer.URL, "BEDROCK_MODEL_ID=" + model, "FI_API_KEY=synthetic-fi-api", "FI_SECRET_KEY=synthetic-fi-secret", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=" + receiver.URL + "/tracer/v1/traces"}
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("example: %s: %v", b, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(spans) != 2 {
		t.Fatalf("want two complete semantic traces; got %d", len(spans))
	}
	for _, s := range spans {
		if s.EndTimeUnixNano <= s.StartTimeUnixNano {
			t.Error("invalid lifetime")
		}
		values := map[string]string{}
		cacheWriteFound := false
		firstTokenMillis := float64(0)
		for _, a := range s.Attributes {
			values[a.Key] = a.Value.GetStringValue()
			if a.Key == "gen_ai.server.time_to_first_token" {
				firstTokenMillis = a.Value.GetDoubleValue()
			}
			if a.Key == "gen_ai.latency.time_to_first_token" {
				t.Error("unrecognized timing key on OTLP wire")
			}
			// This is the cache-write key accepted by Future AGI ingestion,
			// not the provider's cache_creation_input_tokens payload field.
			if a.Key == "gen_ai.usage.cache_write_tokens" {
				cacheWriteFound = a.Value.GetIntValue() == 4
			}
			if a.Key == "gen_ai.usage.cache_creation_tokens" {
				t.Error("unrecognized cache-write attribute on the OTLP wire")
			}
		}
		if s.Name == "bedrock.ConverseStream" && (firstTokenMillis < 40 || firstTokenMillis > float64(s.EndTimeUnixNano-s.StartTimeUnixNano)/1e6) {
			t.Errorf("OTLP first-token value %g must be milliseconds", firstTokenMillis)
		}
		if !cacheWriteFound {
			t.Error("canonical cache-write token count missing from OTLP")
		}
		if values["gen_ai.span.kind"] != "LLM" || values["session.id"] != "weather-session" || values["gen_ai.request.model"] != model {
			t.Error("semantic attributes missing")
		}
	}
}
