package auto

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func moduleSetup(t *testing.T) {
	t.Helper()
	_ = Shutdown(context.Background())
	resetModulesForTest()
	agent.Lock()
	agent.current, agent.first, agent.armed = nil, nil, nil
	agent.Unlock()
	old := otel.GetTracerProvider()
	base := http.DefaultTransport
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	t.Setenv("FI_API_KEY", "")
	t.Setenv("FI_SECRET_KEY", "")
	t.Cleanup(func() {
		_ = Shutdown(context.Background())
		http.DefaultTransport = base
		otel.SetTracerProvider(old)
		resetModulesForTest()
	})
}

func startModuleSpan(name string) {
	_, span := Attach(name).TracerProvider().Tracer("module").Start(context.Background(), "step", trace.WithAttributes(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.End()
}

// A module the configuration does not allow never activates the agent, so a
// later Init still applies its options.
func TestDisallowedModuleDoesNotActivateTheAgent(t *testing.T) {
	moduleSetup(t)
	t.Setenv("FI_INSTRUMENTATIONS", "openai")
	global := sdktrace.NewTracerProvider()
	defer global.Shutdown(context.Background())
	otel.SetTracerProvider(global)
	startModuleSpan("eino")
	agent.Lock()
	active, first := agent.current != nil, agent.first != nil
	agent.Unlock()
	if active || first {
		t.Fatalf("a disallowed module activated the agent (current=%v first=%v)", active, first)
	}
	if _, err := InitForTest(context.Background(), WithProject("late-project", "observe")); err != nil {
		t.Fatal(err)
	}
	if got := Report().Config.ProjectName; got != "late-project" {
		t.Fatalf("Init options were ignored after a disallowed module ran: project %q", got)
	}
}

// Activation from a module's first span must not replace http.DefaultTransport
// while requests are in flight.
func TestModuleActivationLeavesTheDefaultTransportAlone(t *testing.T) {
	moduleSetup(t)
	global := sdktrace.NewTracerProvider()
	defer global.Shutdown(context.Background())
	otel.SetTracerProvider(global)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "ok") }))
	defer srv.Close()
	before := http.DefaultTransport
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				resp, err := http.DefaultClient.Get(srv.URL)
				if err == nil {
					_, _ = io.Copy(io.Discard, resp.Body)
					resp.Body.Close()
				}
			}
		}()
	}
	startModuleSpan("eino")
	close(stop)
	wg.Wait()
	if http.DefaultTransport != before {
		t.Fatal("module activation replaced http.DefaultTransport")
	}
}

// With no credentials the agent is not armed, so an attached module is not
// reported as auto-instrumented.
func TestAttachedModuleIsManualWhenTheAgentIsInactive(t *testing.T) {
	moduleSetup(t)
	agent.Lock()
	c, _ := readConfig()
	agent.last = makeReport(c, false, "disabled")
	agent.Unlock()
	Attach("eino")
	for _, i := range Report().Integrations {
		if i.Name == "eino" && i.Status != "manual" {
			t.Fatalf("eino = %q while the agent is inactive, want manual", i.Status)
		}
	}
}
