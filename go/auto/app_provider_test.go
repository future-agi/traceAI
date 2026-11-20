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
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	"google.golang.org/protobuf/proto"
)

type exportedSpan struct {
	name                    string
	traceID, spanID, parent []byte
	attrs                   map[string]string
	events                  []exportedEvent
	links                   []exportedLink
}

type exportedEvent struct {
	name  string
	time  uint64
	attrs map[string]string
}

type exportedLink struct {
	spanID []byte
	attrs  map[string]string
}

// spanSink is a local OTLP/HTTP collector that records every span it receives.
type spanSink struct {
	*httptest.Server
	mu    sync.Mutex
	spans []exportedSpan
	raw   []byte
}

func newSpanSink(t *testing.T) *spanSink {
	t.Helper()
	s := &spanSink{}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		var request collector.ExportTraceServiceRequest
		if err := proto.Unmarshal(data, &request); err != nil {
			t.Error(err)
		}
		s.mu.Lock()
		s.raw = append(s.raw, data...)
		for _, rs := range request.ResourceSpans {
			for _, ss := range rs.ScopeSpans {
				for _, span := range ss.Spans {
					attrs := map[string]string{}
					for _, a := range span.Attributes {
						attrs[a.Key] = a.Value.GetStringValue()
					}
					flat := func(kvs []*commonpb.KeyValue) map[string]string {
						out := map[string]string{}
						for _, a := range kvs {
							out[a.Key] = a.Value.GetStringValue()
						}
						return out
					}
					exported := exportedSpan{name: span.Name, traceID: span.TraceId, spanID: span.SpanId, parent: span.ParentSpanId, attrs: attrs}
					for _, e := range span.Events {
						exported.events = append(exported.events, exportedEvent{name: e.Name, time: e.TimeUnixNano, attrs: flat(e.Attributes)})
					}
					for _, l := range span.Links {
						exported.links = append(exported.links, exportedLink{spanID: l.SpanId, attrs: flat(l.Attributes)})
					}
					s.spans = append(s.spans, exported)
				}
			}
		}
		s.mu.Unlock()
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *spanSink) snapshot() ([]exportedSpan, []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]exportedSpan(nil), s.spans...), append([]byte(nil), s.raw...)
}

func spanNames(spans []exportedSpan) []string {
	names := []string{}
	for _, span := range spans {
		names = append(names, span.name)
	}
	return names
}

// An application that installs its own OpenTelemetry provider in main keeps
// its own spans, including spans from a tracer it cached at package level.
// The agent never replaces the global provider. Future AGI receives only the
// wire span, in the application's trace, and the program's output is unchanged.
func TestApplicationOwnedProviderE2E(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "appprovider")
	build := hostCommand("go", "build", "-o", bin, "./testdata/appprovider")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build application %v %s", err, out)
	}
	for _, mode := range []string{"global", "explicit"} {
		t.Run(mode, func(t *testing.T) {
			provider := localServer(t, responseJSON, "application/json", 200)
			app, fi := newSpanSink(t), newSpanSink(t)
			cmd := hostCommand(bin, provider.URL, app.URL, mode)
			cmd.Dir = t.TempDir()
			cmd.Env = append(cleanEnv(), "FI_BASE_URL="+fi.URL, "FI_API_KEY=test-fi-key", "FI_SECRET_KEY=test-fi-secret", "FI_PROJECT_NAME=app-provider-test")
			var stdout, stderr bytes.Buffer
			cmd.Stdout, cmd.Stderr = &stdout, &stderr
			if err := cmd.Run(); err != nil {
				t.Fatalf("application failed %v %s", err, stderr.String())
			}
			if stdout.String() != responseJSON {
				t.Fatalf("application bytes changed: %q", stdout.String())
			}
			// One startup hint, and no other agent line: an explicit Init before
			// the first AI call is the activation, so its options are not ignored.
			if strings.Count(stderr.String(), auto.ShutdownHint) != 1 || strings.Count(stderr.String(), "traceai auto:") != 1 {
				t.Fatalf("want exactly the startup hint, got %q", stderr.String())
			}
			appSpans, _ := app.snapshot()
			fiSpans, fiRaw := fi.snapshot()
			var checkout *exportedSpan
			var appWire *exportedSpan
			for i := range appSpans {
				switch {
				case appSpans[i].name == "checkout":
					checkout = &appSpans[i]
				case appSpans[i].attrs["traceai.capture.layer"] == "wire":
					appWire = &appSpans[i]
				}
			}
			if checkout == nil {
				t.Fatalf("the application span did not reach the application's exporter: %v; stderr %s", spanNames(appSpans), stderr.String())
			}
			if mode == "explicit" && appWire == nil {
				t.Fatalf("the wire span did not land on the provider passed to auto.Init: %v", spanNames(appSpans))
			}
			if len(fiSpans) != 1 || fiSpans[0].attrs["traceai.capture.layer"] != "wire" {
				t.Fatalf("Future AGI must receive only the wire span, got %v", spanNames(fiSpans))
			}
			if !bytes.Equal(fiSpans[0].traceID, checkout.traceID) || !bytes.Equal(fiSpans[0].parent, checkout.spanID) {
				t.Fatal("the wire span left the application's trace")
			}
			if bytes.Contains(fiRaw, []byte("checkout")) || bytes.Contains(fiRaw, []byte("sk-test-PLACEHOLDER")) {
				t.Fatal("the application span or its credential-named attribute reached Future AGI")
			}
		})
	}
}
