package auto

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
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

const imageJSON = `{"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://img.example.test/secret.png"}},{"type":"text","text":"hello"}]}]}`

// hostActive activates the agent on an application-owned provider and returns
// what that provider's own exporters receive.
func hostActive(t *testing.T, env map[string]string) *tracetest.SpanRecorder {
	t.Helper()
	moduleSetup(t)
	for k, v := range env {
		t.Setenv(k, v)
	}
	host := tracetest.NewSpanRecorder()
	app := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(host))
	t.Cleanup(func() { _ = app.Shutdown(context.Background()) })
	if _, err := InitForTest(context.Background(), WithTracerProvider(app), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	return host
}

func attrsOf(span sdktrace.ReadOnlySpan) map[string]any {
	out := map[string]any{}
	for _, kv := range span.Attributes() {
		out[string(kv.Key)] = kv.Value.AsInterface()
	}
	return out
}

// The copy of a module span that the application's own provider exports equals
// what the wire rules give for the same attributes: string values pass the
// structural capture (which removes image references when they are hidden)
// before the switches apply.
func TestApplicationCopyEqualsTheWireRules(t *testing.T) {
	for name, env := range map[string]map[string]string{
		"hide input images":          {"FI_HIDE_INPUT_IMAGES": "true"},
		"hide invocation parameters": {"FI_HIDE_LLM_INVOCATION_PARAMETERS": "true"},
		"hide inputs and images":     {"FI_HIDE_INPUTS": "true", "FI_HIDE_INPUT_IMAGES": "true"},
	} {
		t.Run(name, func(t *testing.T) {
			host := hostActive(t, env)
			values := map[string]any{
				"gen_ai.span.kind":          "CHAIN",
				"input.value":               imageJSON,
				"output.value":              "the answer",
				"gen_ai.request.parameters": `{"temperature":1}`,
				"tags":                      []string{imageJSON, "plain"},
			}
			want := map[string]any{}
			for _, kv := range attributes(values, agent.current.config) {
				want[string(kv.Key)] = kv.Value.AsInterface()
			}
			all := attributes(values, Config{}) // the unmasked attributes, in any order
			_, span := Attach("eino").TracerProvider().Tracer("m").Start(context.Background(), "chain", trace.WithAttributes(all[:2]...))
			span.SetAttributes(all[2:]...)
			span.End()
			ended := host.Ended()
			if len(ended) != 1 {
				t.Fatalf("application provider received %d spans", len(ended))
			}
			got := attrsOf(ended[0])
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("application copy differs from the wire rules\n got: %v\nwant: %v", got, want)
			}
			if _, leaked := got["input.value"]; leaked && strings.Contains(got["input.value"].(string), "img.example.test") && env["FI_HIDE_INPUT_IMAGES"] != "" {
				t.Fatal("an image reference reached the application's exporter")
			}
		})
	}
}

// Events, links and the status of a module span follow the wire rules too.
func TestApplicationCopyOfEventsLinksAndStatus(t *testing.T) {
	host := hostActive(t, map[string]string{"FI_HIDE_INPUTS": "true"})
	linked := trace.SpanContext{}
	linked = linked.WithTraceID(trace.TraceID{1}).WithSpanID(trace.SpanID{1}).WithTraceFlags(trace.FlagsSampled)
	_, span := Attach("eino").TracerProvider().Tracer("m").Start(context.Background(), "chain",
		trace.WithLinks(trace.Link{SpanContext: linked, Attributes: []attribute.KeyValue{attribute.String("input.value", "link secret")}}))
	span.AddEvent("user said hello", trace.WithAttributes(attribute.String("note", "event secret")))
	span.AddEvent("exception", trace.WithAttributes(attribute.String("exception.type", "*errors.errorString"), attribute.String("exception.message", "message secret")))
	span.SetStatus(codes.Error, "*errors.errorString")
	span.End()
	ended := host.Ended()
	if len(ended) != 1 {
		t.Fatalf("application provider received %d spans", len(ended))
	}
	raw := fmt.Sprint(ended[0].Links(), ended[0].Events())
	for _, secret := range []string{"link secret", "event secret", "message secret", "user said hello"} {
		if strings.Contains(raw, secret) {
			t.Errorf("%q reached the application's exporter", secret)
		}
	}
	var sawType bool
	for _, e := range ended[0].Events() {
		for _, kv := range e.Attributes {
			sawType = sawType || (kv.Key == "exception.type" && kv.Value.AsString() == "*errors.errorString")
		}
	}
	if !sawType {
		t.Error("exception.type was redacted from the exception event")
	}
	if got := ended[0].Status().Description; got != "*errors.errorString" {
		t.Errorf("status description = %q, want the Go error type the handler set", got)
	}
}

// Once the agent is active a module span does not take the agent's lock.
func TestActiveModuleSpansDoNotWaitForTheAgentLock(t *testing.T) {
	hostActive(t, nil)
	startModuleSpan("eino") // the first span resolves the route
	agent.Lock()
	done := make(chan struct{})
	go func() { startModuleSpan("eino"); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		agent.Unlock()
		t.Fatal("a module span waited for the agent lock although the agent is active")
	}
	agent.Unlock()
}

// Concurrent module runs while the agent activates and shuts down.
func TestConcurrentModuleSpansRaceFree(t *testing.T) {
	moduleSetup(t)
	t.Setenv("FI_BASE_URL", "http://127.0.0.1:9")
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				startModuleSpan("eino")
			}
		}()
	}
	if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	_ = Shutdown(context.Background())
}
