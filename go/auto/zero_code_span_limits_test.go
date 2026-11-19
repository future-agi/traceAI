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

// Zero-code path with the application's own SDK provider, whose attribute
// count, value-length, per-event and per-link limits come from the standard
// OTEL_* variables. The agent activates on the first AI call and attaches to
// that provider. An evicted or truncated MIME/type hint never lets its payload
// reach OTLP, on span attributes, events and links; application bytes are
// unchanged.
func TestZeroCodeSpanLimitsE2E(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "spanlimits")
	build := hostCommand("go", "build", "-o", bin, "./testdata/spanlimits")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build zero-code app %v %s", err, out)
	}
	for mode, env := range map[string][]string{
		"count":   {"OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT=32", "OTEL_EVENT_ATTRIBUTE_COUNT_LIMIT=4", "OTEL_LINK_ATTRIBUTE_COUNT_LIMIT=4"},
		"length":  {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=16"},
		"length8": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=8"},
		"query45": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=45"},
		"query50": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=50"},
		"query57": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=57"},
		"query80": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=80"},
		"query34": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=34"},
		"query28": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=28"},
		"query29": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=29"},
		"query30": {"OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=30"},
	} {
		t.Run(mode, func(t *testing.T) {
			provider := localServer(t, responseJSON, "application/json", 200)
			baseline := hostCommand(bin, provider.URL, mode)
			baseline.Dir = t.TempDir()
			baseline.Env = append(cleanEnv(), "OTEL_SDK_DISABLED=true")
			want, err := baseline.Output()
			if err != nil {
				t.Fatalf("disabled baseline failed: %v", err)
			}
			var mu sync.Mutex
			var exported []byte
			spans := 0
			sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				data, _ := io.ReadAll(r.Body)
				var request collector.ExportTraceServiceRequest
				if err := proto.Unmarshal(data, &request); err != nil {
					t.Error(err)
				}
				mu.Lock()
				exported = append(exported, data...)
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
			cmd.Env = append(append(cleanEnv(), "FI_BASE_URL="+sink.URL, "FI_API_KEY=rc2-fi-key", "FI_SECRET_KEY=rc2-fi-secret"), env...)
			var stdout, stderr bytes.Buffer
			cmd.Stdout, cmd.Stderr = &stdout, &stderr
			if err := cmd.Run(); err != nil {
				t.Fatalf("zero-code app failed %v %s", err, stderr.String())
			}
			if stdout.String() != responseJSON || !bytes.Equal(stdout.Bytes(), want) {
				t.Fatal("application bytes changed")
			}
			mu.Lock()
			defer mu.Unlock()
			if spans != 3 {
				t.Fatalf("want wire, link-target and application spans, got %d", spans)
			}
			for _, raw := range []string{"AQIDBAUGBwgJCgsMDQ4PEBESExQ=", "AQIDBA=="} {
				if bytes.Contains(exported, []byte(raw)) {
					t.Fatalf("raw media %q reached the zero-code OTLP export", raw)
				}
			}
			// rc2c: CR/LF-wrapped ftyp media cut before its signature.
			if bytes.Contains(exported, []byte("AAAA\r\n")) {
				t.Fatal("raw wrapped media prefix reached the zero-code OTLP export")
			}
			for _, raw := range []string{"AQIDBA", "AAAA", "https://example.invalid/?v=A"} {
				if bytes.Contains(exported, []byte(raw)) {
					t.Fatalf("raw query media %q reached the zero-code OTLP export", raw)
				}
			}
			// Short encoded audio "AQI" as an exact OTLP string value (field 1, length 3).
			if bytes.Contains(exported, []byte("\x0a\x03AQI")) {
				t.Fatal("raw short media reached the zero-code OTLP export")
			}
			survivor := "message 3"
			if mode == "length8" {
				survivor = "note ok"
			}
			if !bytes.Contains(exported, []byte(survivor)) {
				t.Fatal("ordinary text lost")
			}
		})
	}
}
