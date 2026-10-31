package auto

import (
	"context"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestConfigPrecedence(t *testing.T) {
	file := filepath.Join(t.TempDir(), "futureagi.toml")
	if err := os.WriteFile(file, []byte("project_name = \"file#project\" # comment\nproject_type = \"observe\"\nwire_capture = false\nsample_rate = 0.25\ninstrumentations = [\"wire\", \"openai\"]\ndisabled_instrumentations = [\"anthropic\"]\ndebug = true\n[ignored]\nwire_max_body_bytes = 200\nhide_inputs = true\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FI_CONFIG_FILE", file)
	t.Setenv("FI_PROJECT_NAME", "env-project")
	t.Setenv("FI_SAMPLE_RATE", "0.5")
	c, err := readConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.ProjectName != "env-project" || c.SampleRate != 0.5 || c.WireCapture || c.WireMaxBodyBytes != 200 || !c.Debug || !c.TraceConfig.HideInputs || allowed(c, "anthropic") || !allowed(c, "wire") {
		t.Fatalf("incorrect config %+v", c)
	}
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	p := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(p)
	defer otel.SetTracerProvider(old)
	defer p.Shutdown(context.Background())
	stop, err := InitForTest(context.Background(), WithProject("code-project", "observe"), WithSampleRate(0.75), WithSpanProcessor(tracetest.NewSpanRecorder()))
	if err != nil {
		t.Fatal(err)
	}
	defer stop(context.Background())
	if Report().Config.ProjectName != "code-project" || Report().Config.SampleRate != 0.75 {
		t.Fatal("code did not override env")
	}
}
func TestSDKDisabledAndMissingCredentials(t *testing.T) {
	_ = Shutdown(context.Background())
	base := http.DefaultTransport
	old := otel.GetTracerProvider()
	t.Setenv("OTEL_SDK_DISABLED", "true")
	_, err := InitForTest(context.Background(), WithCredentials("key", "secret"))
	if err != nil {
		t.Fatal(err)
	}
	if http.DefaultTransport != base || otel.GetTracerProvider() != old {
		t.Fatal("disabled agent changed globals")
	}
	t.Setenv("OTEL_SDK_DISABLED", "false")
	t.Setenv("FI_API_KEY", "")
	t.Setenv("FI_SECRET_KEY", "")
	_, err = InitForTest(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if http.DefaultTransport != base {
		t.Fatal("missing-key agent patched transport")
	}
}
func TestExistingProviderAndNoStartupNetwork(t *testing.T) {
	_ = Shutdown(context.Background())
	var requests atomic.Int64
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests.Add(1); w.WriteHeader(200) }))
	defer s.Close()
	t.Setenv("FI_BASE_URL", s.URL)
	old := otel.GetTracerProvider()
	p := sdktrace.NewTracerProvider()
	original := tracetest.NewSpanRecorder()
	p.RegisterSpanProcessor(original)
	otel.SetTracerProvider(p)
	defer otel.SetTracerProvider(old)
	defer p.Shutdown(context.Background())
	added := tracetest.NewSpanRecorder()
	stop, err := InitForTest(context.Background(), WithSpanProcessor(added), WithCredentials("key", "secret"))
	if err != nil {
		t.Fatal(err)
	}
	if otel.GetTracerProvider() != p {
		t.Fatal("existing provider replaced")
	}
	_, span := p.Tracer("app").Start(context.Background(), "app")
	span.End()
	if len(original.Ended()) != 1 || len(added.Ended()) != 1 {
		t.Fatal("processors not coexisting")
	}
	if err = stop(context.Background()); err != nil {
		t.Fatal(err)
	}
	_, span = p.Tracer("app").Start(context.Background(), "after shutdown")
	span.End()
	if len(original.Ended()) != 2 {
		t.Fatal("agent shutdown shut down application provider")
	}
	stop, err = InitForTest(context.Background(), WithCredentials("key", "secret"))
	if err != nil {
		t.Fatal(err)
	}
	if requests.Load() != 0 {
		t.Fatal("network call during startup")
	}
	if err = stop(context.Background()); err != nil {
		t.Fatal(err)
	}
}
func TestInvalidOptionsFailOpen(t *testing.T) {
	for _, rate := range []float64{-1, 2, math.NaN()} {
		_ = Shutdown(context.Background())
		_, err := InitForTest(context.Background(), WithSampleRate(rate), WithSpanProcessor(tracetest.NewSpanRecorder()))
		if err == nil {
			t.Fatal("invalid rate accepted")
		}
	}
}
func TestMaskingAndSecrets(t *testing.T) {
	c := Config{APIKey: "top-secret-api-key", SecretKey: "secret-key-value", TraceConfig: TraceConfig{HideInputs: true, HideOutputs: true}}
	a := map[string]any{"input.value": `{"api_key":"provider-secret","input_audio":{"data":"AUDIO"}}`, "output.value": "top-secret-api-key", "gen_ai.input.messages.0.message.content": "sensitive", "gen_ai.output.messages.0.message.content": "sensitive", "gen_ai.audio.transcript": "sensitive"}
	got := attributes(a, c)
	data, _ := json.Marshal(got)
	s := string(data)
	for _, secret := range []string{"provider-secret", "AUDIO", "top-secret-api-key", "sensitive"} {
		if strings.Contains(s, secret) {
			t.Errorf("leaked %s", secret)
		}
	}
	raw := safeString(`{"api_key":"provider-secret","input_audio":{"data":"AUDIO"},"key":"top-secret-api-key"}`, Config{APIKey: "top-secret-api-key"})
	if strings.Contains(raw, "AUDIO") || strings.Contains(raw, "provider-secret") || strings.Contains(raw, "top-secret-api-key") {
		t.Fatal("JSON secret/audio leakage")
	}
	cfg := TraceConfig{HideInputText: true, HideOutputText: true, HideLLMInvocationParameters: true}
	for _, kv := range []attribute.KeyValue{attribute.String("gen_ai.request.parameters", "secret"), attribute.String("gen_ai.input.messages.0.message.content", "text"), attribute.String("gen_ai.output.messages.0.message.content", "text")} {
		v, ok := cfg.Mask(kv)
		if ok && v.Value.AsString() != "__REDACTED__" {
			t.Fatal("text masking failed")
		}
	}
}
func TestBoundedAndBinaryCountOnly(t *testing.T) {
	for _, binary := range []bool{false, true} {
		b := bounded{max: 4, binary: binary}
		b.write([]byte("12345"))
		if len(b.data) != 0 || b.size != 5 || (!binary && !b.truncated) {
			t.Fatal("unbounded body")
		}
	}
	o := newObserver("audio/wav", "", 4)
	o.write(make([]byte, 100))
	o.finish()
	r, _ := http.NewRequest("POST", "https://api.deepgram.com/v1/listen", nil)
	r.Header.Set("Content-Type", "audio/wav")
	rq := o.snapshot(r)
	if rq.Size != 100 || rq.Body != nil {
		t.Fatal("audio bytes buffered")
	}
	r.Body = io.NopCloser(strings.NewReader("hello"))
}

func TestExplicitInitRetainsAutomaticState(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	p := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(p)
	defer otel.SetTracerProvider(old)
	defer p.Shutdown(context.Background())
	first, second := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
	_, err := InitForTest(context.Background(), WithProject("environment", "observe"), WithSpanProcessor(first))
	if err != nil {
		t.Fatal(err)
	}
	stop, err := InitForTest(context.Background(), WithProject("code", "observe"), WithSpanProcessor(second))
	if err != nil {
		t.Fatal(err)
	}
	defer stop(context.Background())
	if Report().Config.ProjectName != "environment" {
		t.Fatal("explicit options changed automatic init")
	}
	_, span := p.Tracer("test").Start(context.Background(), "second")
	span.End()
	if len(first.Ended()) != 1 || len(second.Ended()) != 0 {
		t.Fatal("second init rewired the processor")
	}
}

func TestOwnedProviderParentBasedSampling(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	defer otel.SetTracerProvider(old)
	recorder := tracetest.NewSpanRecorder()
	stop, err := InitForTest(context.Background(), WithSampleRate(0), WithSpanProcessor(recorder))
	if err != nil {
		t.Fatal(err)
	}
	defer stop(context.Background())
	agent.Lock()
	provider := agent.current.provider
	agent.Unlock()
	tracer := provider.Tracer("sampling")
	_, root := tracer.Start(context.Background(), "unsampled root")
	root.End()
	parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: trace.FlagsSampled, Remote: true})
	_, child := tracer.Start(trace.ContextWithRemoteSpanContext(context.Background(), parent), "sampled child")
	child.End()
	if len(recorder.Ended()) != 1 || recorder.Ended()[0].Name() != "sampled child" {
		t.Fatal("parent-based sampling not honored")
	}
}

func TestSanitizerPreservesToolArgumentBytes(t *testing.T) {
	input := `{"city": "Oslo"}`
	if got := safeString(input, Config{}); got != input {
		t.Fatalf("tool argument JSON changed %q", got)
	}
	audio := `{"contents":[{"parts":[{"inlineData":{"mimeType":"audio/wav","data":"RAW_AUDIO_BYTES"}}]}]}`
	if got := safeString(audio, Config{}); strings.Contains(got, "RAW_AUDIO_BYTES") {
		t.Fatal("inline audio leaked")
	}
}
