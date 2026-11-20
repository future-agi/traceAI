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

// Headers from the standard OTEL_EXPORTER_OTLP_*HEADERS variables belong to
// whatever backend the application configured them for. The Future AGI
// exporter must send its own credentials and nothing taken from them. Every
// value is synthetic and the only server is on 127.0.0.1.
func TestOTLPAmbientHeadersNotForwardedToFutureAGI(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "ambientheaders")
	build := hostCommand("go", "build", "-o", bin, "./testdata/ambientheaders")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build app %v %s", err, out)
	}
	ambient := map[string]string{
		"OTEL_EXPORTER_OTLP_HEADERS":        "Authorization=Bearer%20SYNTHETIC-OTHER-BACKEND,X-Synthetic-Marker-A=marker-a",
		"OTEL_EXPORTER_OTLP_TRACES_HEADERS": "Authorization=Bearer%20SYNTHETIC-OTHER-BACKEND,X-Synthetic-Marker-B=marker-b",
	}
	for name, spec := range ambient {
		for _, mode := range []string{"init", "zero"} {
			t.Run(name+"/"+mode, func(t *testing.T) {
				provider := localServer(t, responseJSON, "application/json", 200)
				var mu sync.Mutex
				var seen []http.Header
				spans := 0
				sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					data, _ := io.ReadAll(r.Body)
					var request collector.ExportTraceServiceRequest
					_ = proto.Unmarshal(data, &request)
					mu.Lock()
					seen = append(seen, r.Header.Clone())
					for _, rs := range request.ResourceSpans {
						for _, ss := range rs.ScopeSpans {
							spans += len(ss.Spans)
						}
					}
					mu.Unlock()
					w.Header().Set("Content-Type", "application/x-protobuf")
				}))
				defer sink.Close()
				cmd := hostCommand(bin, provider.URL, mode)
				cmd.Dir = t.TempDir()
				cmd.Env = append(cleanEnv(), "FI_BASE_URL="+sink.URL, "FI_API_KEY=synthetic-fi-key", "FI_SECRET_KEY=synthetic-fi-secret", name+"="+spec)
				var stderr bytes.Buffer
				cmd.Stderr = &stderr
				if err := cmd.Run(); err != nil {
					t.Fatalf("app failed %v %s", err, stderr.String())
				}
				mu.Lock()
				defer mu.Unlock()
				if spans == 0 || len(seen) == 0 {
					t.Fatal("no export reached the local collector")
				}
				for _, h := range seen {
					if h.Get("X-Api-Key") != "synthetic-fi-key" || h.Get("X-Secret-Key") != "synthetic-fi-secret" {
						t.Fatal("Future AGI credentials missing from export")
					}
					if h.Get("Authorization") != "" || h.Get("X-Synthetic-Marker-A") != "" || h.Get("X-Synthetic-Marker-B") != "" {
						t.Fatal("ambient OTLP headers reach the FI exporter")
					}
				}
			})
		}
	}
}
