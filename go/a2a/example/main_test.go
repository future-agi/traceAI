package main

import (
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
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
		http.Error(w, "not an uncompressed OTLP protobuf export", http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	s.reqs = append(s.reqs, export{
		path: r.URL.Path, contentType: r.Header.Get("Content-Type"), contentEncoding: r.Header.Get("Content-Encoding"),
		apiKey: r.Header.Get("X-Api-Key"), secretKey: r.Header.Get("X-Secret-Key"), body: msg,
	})
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/x-protobuf")
	_, _ = w.Write(nil)
}

func attr(kvs []*commonpb.KeyValue, key string) (string, bool) {
	for _, kv := range kvs {
		if kv.Key == key {
			return kv.Value.GetStringValue(), true
		}
	}
	return "", false
}

func TestExampleExportsOneStitchedTraceToFutureAGI(t *testing.T) {
	sink := &otlpSink{}
	collector := httptest.NewServer(sink)
	defer collector.Close()
	t.Setenv("FI_BASE_URL", collector.URL)
	t.Setenv("FI_API_KEY", "test-api-key")
	t.Setenv("FI_SECRET_KEY", "test-secret-key")
	t.Setenv("FI_PROJECT_NAME", "a2a-go-example-test")
	t.Setenv("OPENAI_API_KEY", "") // echo agent, no model call
	*stream, *fail, *session, *question = true, true, "sess-test", "plan a trip"

	if err := run(); err != nil {
		t.Fatal(err)
	}

	sink.mu.Lock()
	defer sink.mu.Unlock()
	if len(sink.reqs) == 0 {
		t.Fatal("shutdown did not flush any span")
	}
	byName := map[string][]*tracepb.Span{}
	traces := map[string]bool{}
	for _, r := range sink.reqs {
		if r.path != "/tracer/v1/traces" || r.contentType != "application/x-protobuf" || r.contentEncoding != "" {
			t.Errorf("export %s %s encoding=%q", r.path, r.contentType, r.contentEncoding)
		}
		if r.apiKey != "test-api-key" || r.secretKey != "test-secret-key" {
			t.Error("export request is missing a Future AGI header")
		}
		for _, rs := range r.body.ResourceSpans {
			res := rs.GetResource().GetAttributes()
			if v, _ := attr(res, "project_name"); v != "a2a-go-example-test" {
				t.Errorf("project_name = %q", v)
			}
			if v, _ := attr(res, "project_type"); v != "observe" {
				t.Errorf("project_type = %q", v)
			}
			for _, k := range []string{"host.name", "process.owner", "process.executable.path", "os.type"} {
				if _, ok := attr(res, k); ok {
					t.Errorf("resource carries %s", k)
				}
			}
			for _, ss := range rs.ScopeSpans {
				for _, sp := range ss.Spans {
					byName[sp.Name] = append(byName[sp.Name], sp)
					traces[hex.EncodeToString(sp.TraceId)] = true
				}
			}
		}
	}
	if len(traces) != 1 {
		t.Errorf("want one trace, got %d", len(traces))
	}
	root := byName["trip-planner"]
	calls := byName["a2a.SendStreamingMessage"]
	remote := byName["trip-helper"]
	if len(root) != 1 || len(calls) != 2 || len(remote) != 2 {
		t.Fatalf("spans: %d root, %d a2a, %d remote", len(root), len(calls), len(remote))
	}
	failed := 0
	for _, c := range calls {
		if string(c.ParentSpanId) != string(root[0].SpanId) {
			t.Error("A2A span is not a child of the agent span")
		}
		if v, _ := attr(c.Attributes, "gen_ai.span.kind"); v != "AGENT" {
			t.Errorf("A2A span kind %q", v)
		}
		if v, _ := attr(c.Attributes, "session.id"); v != "sess-test" {
			t.Errorf("session.id %q", v)
		}
		if c.Status.GetCode() == tracepb.Status_STATUS_CODE_ERROR {
			failed++
		}
	}
	if failed != 1 {
		t.Errorf("want one failed A2A call, got %d", failed)
	}
	for _, r := range remote {
		found := false
		for _, c := range calls {
			if string(r.ParentSpanId) == string(c.SpanId) {
				found = true
			}
		}
		if !found {
			t.Error("the remote agent span did not continue the A2A call's trace context")
		}
	}
	for _, sp := range append(append(root, calls...), remote...) {
		for _, kv := range sp.Attributes {
			if strings.Contains(kv.Value.String(), "test-secret-key") || strings.Contains(kv.Value.String(), "test-api-key") {
				t.Errorf("span %s carries a Future AGI key in %s", sp.Name, kv.Key)
			}
		}
	}
}
