package auto

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func exporterRegressionSetup(t *testing.T) *sdktrace.TracerProvider {
	t.Helper()
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	p := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(p)
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	t.Cleanup(func() {
		_ = Shutdown(context.Background())
		_ = p.Shutdown(context.Background())
		otel.SetTracerProvider(old)
	})
	return p
}
func exportRegressionSpan(t *testing.T, p *sdktrace.TracerProvider) {
	t.Helper()
	_, span := p.Tracer("application").Start(context.Background(), "application-span")
	span.End()
	if err := ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestExporterRedirect(t *testing.T) {
	p := exporterRegressionSetup(t)
	var redirected atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		redirected.Add(1)
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(200)
	}))
	defer target.Close()
	var origin atomic.Int32
	collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin.Add(1)
		_, _ = io.Copy(io.Discard, r.Body)
		if r.Header.Get("X-Api-Key") != "DUMMY_FI_API_KEY" || r.Header.Get("X-Secret-Key") != "DUMMY_FI_SECRET_KEY" {
			t.Error("origin did not receive configured FI headers")
		}
		http.Redirect(w, r, strings.Replace(target.URL, "127.0.0.1", "localhost", 1)+"/sink", http.StatusTemporaryRedirect)
	}))
	defer collector.Close()
	t.Setenv("FI_BASE_URL", collector.URL)
	if _, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_API_KEY", "DUMMY_FI_SECRET_KEY")); err != nil {
		t.Fatal(err)
	}
	exportRegressionSpan(t, p)
	if origin.Load() != 1 || redirected.Load() != 0 {
		t.Fatalf("collector redirect followed: origin=%d redirected=%d", origin.Load(), redirected.Load())
	}
}

type feedbackTransport struct {
	base     http.RoundTripper
	provider *sdktrace.TracerProvider
	calls    atomic.Int32
}

func (f *feedbackTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	// Deliberately ignore the agent-private suppression key, just as a third
	// party HTTP instrumentor does. Bound the old feedback loop to four spans.
	if f.calls.Add(1) <= 4 {
		_, s := f.provider.Tracer("application-http").Start(r.Context(), "collector-http-span")
		defer s.End()
	}
	return f.base.RoundTrip(r)
}
func TestExporterFeedback(t *testing.T) {
	p := exporterRegressionSetup(t)
	var exports atomic.Int32
	collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		exports.Add(1)
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(200)
	}))
	defer collector.Close()
	t.Setenv("FI_BASE_URL", collector.URL)
	oldTransport := http.DefaultTransport
	instrumented := &feedbackTransport{base: oldTransport, provider: p}
	http.DefaultTransport = instrumented
	t.Cleanup(func() { http.DefaultTransport = oldTransport })
	if _, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_API_KEY", "DUMMY_FI_SECRET_KEY")); err != nil {
		t.Fatal(err)
	}
	exportRegressionSpan(t, p)
	if err := Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if exports.Load() != 1 || instrumented.calls.Load() != 0 {
		t.Fatalf("export feedback: collector exports=%d application instrumentor calls=%d", exports.Load(), instrumented.calls.Load())
	}
}

func TestCollectorBaseURLComponents(t *testing.T) {
	for _, suffix := range []string{"/base?tenant=example", "/base#fragment", "/base/?tenant=example#fragment", "/base%2Ftenant?x=1"} {
		t.Run(suffix, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			paths := make(chan string, 2)
			collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				paths <- r.URL.Path
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "application/x-protobuf")
				w.WriteHeader(200)
			}))
			defer collector.Close()
			t.Setenv("FI_BASE_URL", collector.URL+suffix)
			if _, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_API_KEY", "DUMMY_FI_SECRET_KEY")); err != nil {
				t.Fatal(err)
			}
			exportRegressionSpan(t, p)
			want := "/base/tracer/v1/traces"
			if strings.Contains(suffix, "%2Ftenant") {
				want = "/base/tenant/tracer/v1/traces"
			}
			select {
			case path := <-paths:
				if path != want {
					t.Fatalf("collector path=%q want %q", path, want)
				}
			default:
				t.Fatal("no collector request")
			}
		})
	}
}

func TestExporterClosesOwnedConnections(t *testing.T) {
	p := exporterRegressionSetup(t)
	closed := make(chan struct{}, 1)
	collector := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(200)
	}))
	collector.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateClosed {
			select {
			case closed <- struct{}{}:
			default:
			}
		}
	}
	collector.Start()
	defer collector.Close()
	t.Setenv("FI_BASE_URL", collector.URL)
	if _, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_API_KEY", "DUMMY_FI_SECRET_KEY")); err != nil {
		t.Fatal(err)
	}
	exportRegressionSpan(t, p)
	if err := Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("retired exporter kept its dedicated connection pool alive")
	}
}

func TestOwnedProviderRetainsSampling(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	t.Cleanup(func() { _ = Shutdown(context.Background()); otel.SetTracerProvider(old) })
	first, second := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(first), WithSampleRate(1)); err != nil {
		t.Fatal(err)
	}
	provider := agent.current.provider
	// Model the automatically installed global provider without altering any
	// process-wide OTel internal singleton in this test binary.
	otel.SetTracerProvider(provider)
	cached := provider.Tracer("cached")
	if _, err := InitForTest(context.Background(), WithSpanProcessor(second), WithSampleRate(0)); err != nil {
		t.Fatal(err)
	}
	if otel.GetTracerProvider() != provider || agent.current.provider != provider {
		t.Fatal("owned provider replaced during explicit init")
	}
	_, s := cached.Start(context.Background(), "unsampled-after-reconfigure")
	if !s.IsRecording() {
		t.Fatal("second init changed sampling on stable provider")
	}
	s.End()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(second), WithSampleRate(1)); err != nil {
		t.Fatal(err)
	}
	_, s = cached.Start(context.Background(), "sampled-after-reconfigure")
	s.End()
	if len(first.Ended()) != 2 || len(second.Ended()) != 0 {
		t.Fatalf("processor replacement/sampling failed: first=%d second=%d", len(first.Ended()), len(second.Ended()))
	}
}
