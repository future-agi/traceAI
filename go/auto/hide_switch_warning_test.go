package auto_test

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

// A hide switch with a value the agent does not recognise hides that content
// and says so. The configuration is read at package initialization and again
// at activation, so a real process is the proof that the warning is printed
// once. It names the variable and never prints the value.
func TestUnrecognisedHideSwitchE2E(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "plain")
	build := hostCommand("go", "build", "-o", bin, "./testdata/blank")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build plain app %v %s", err, out)
	}
	provider := localServer(t, responseJSON, "application/json", 200)
	var mu sync.Mutex
	inputs := []string{}
	outputs := []string{}
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		data, _ := io.ReadAll(r.Body)
		var request collector.ExportTraceServiceRequest
		if err := proto.Unmarshal(data, &request); err != nil {
			t.Error(err)
		}
		for _, rs := range request.ResourceSpans {
			for _, ss := range rs.ScopeSpans {
				for _, s := range ss.Spans {
					for _, a := range s.Attributes {
						switch a.Key {
						case "input.value":
							inputs = append(inputs, a.Value.GetStringValue())
						case "output.value":
							outputs = append(outputs, a.Value.GetStringValue())
						}
					}
				}
			}
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(200)
	}))
	defer sink.Close()
	cmd := hostCommand(bin, provider.URL)
	cmd.Dir = t.TempDir()
	cmd.Env = append(cleanEnv(), "FI_BASE_URL="+sink.URL, "FI_PROJECT_NAME=blank-test", "FI_API_KEY=test-fi-key", "FI_SECRET_KEY=test-fi-secret", "FI_HIDE_INPUTS=maybe")
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("plain app crashed %v %s", err, stderr.String())
	}
	if stdout.String() != responseJSON+responseJSON {
		t.Fatal("application bytes changed")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(inputs) != 2 || inputs[0] != auto.RedactedValue || inputs[1] != auto.RedactedValue {
		t.Fatalf("an unrecognised FI_HIDE_INPUTS must hide the input of both calls, got %q", inputs)
	}
	if len(outputs) != 2 || outputs[0] != "Hello" || outputs[1] != "Hello" {
		t.Fatalf("FI_HIDE_INPUTS must leave the output of both calls alone, got %q", outputs)
	}
	warnings := 0
	for _, line := range strings.Split(stderr.String(), "\n") {
		if strings.Contains(line, "FI_HIDE_INPUTS") {
			warnings++
		}
	}
	if warnings != 1 {
		t.Fatalf("want exactly one warning that names FI_HIDE_INPUTS, got %d: %q", warnings, stderr.String())
	}
	if strings.Contains(stderr.String(), "maybe") {
		t.Fatalf("the warning must not print the value: %q", stderr.String())
	}
}
