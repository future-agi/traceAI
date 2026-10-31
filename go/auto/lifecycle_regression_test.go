package auto

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestConfigOverrideCannotRepairEnv(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	t.Cleanup(func() { _ = Shutdown(context.Background()); otel.SetTracerProvider(old) })
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	valid, err := readConfig()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("FI_SAMPLE_RATE", "invalid")
	stop, err := InitForTest(context.Background(), WithSampleRate(.5), WithSpanProcessor(tracetest.NewSpanRecorder()))
	if err != nil {
		t.Fatalf("valid sample-rate override rejected: %v", err)
	}
	if Report().Config.SampleRate != .5 {
		t.Fatal("override ignored")
	}
	_ = stop(context.Background())
	t.Setenv("FI_WIRE_MAX_BODY_BYTES", "invalid")
	stop, err = InitForTest(context.Background(), WithConfig(valid), WithSpanProcessor(tracetest.NewSpanRecorder()))
	if err != nil {
		t.Fatalf("valid complete config override rejected: %v", err)
	}
	_ = stop(context.Background())
	if _, err = InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err == nil {
		t.Fatal("unoverridden invalid env accepted")
	}
}
func TestDefaultProjectConfig(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	t.Cleanup(func() { _ = Shutdown(context.Background()); otel.SetTracerProvider(old) })
	for _, key := range []string{"FI_PROJECT_NAME", "FI_PROJECT_VERSION_NAME", "OTEL_SERVICE_NAME"} {
		t.Setenv(key, "")
	}
	t.Setenv("FI_CONFIG_FILE", filepath.Join(t.TempDir(), "absent.toml"))
	rec := tracetest.NewSpanRecorder()
	if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
		t.Fatal(err)
	}
	wd, _ := os.Getwd()
	if c := Report().Config; c.ProjectName != filepath.Base(wd) || c.ProjectVersionName != "DEFAULT_PROJECT_VERSION_NAME" {
		t.Fatalf("defaults differ: %+v", c)
	}
	agent.Lock()
	p := agent.current.provider
	agent.Unlock()
	_, s := p.Tracer("resource-test").Start(context.Background(), "resource")
	s.End()
	attrs := map[string]string{}
	for _, kv := range rec.Ended()[0].Resource().Attributes() {
		attrs[string(kv.Key)] = kv.Value.AsString()
	}
	for key, want := range map[string]string{"project_name": filepath.Base(wd), "project_type": "observe", "project_version_name": "DEFAULT_PROJECT_VERSION_NAME", "eval_tags": "[]", "metadata": "{}", "semantic_convention": "fi"} {
		if attrs[key] != want {
			t.Errorf("resource %s=%q want %q", key, attrs[key], want)
		}
	}
	if attrs["project_version_id"] == "" {
		t.Fatal("missing version id")
	}
	_ = Shutdown(context.Background())
	t.Setenv("OTEL_SERVICE_NAME", "otel-default")
	c, err := readConfig()
	if err != nil || c.ProjectName != "otel-default" {
		t.Fatalf("OTEL_SERVICE_NAME fallback: %+v %v", c, err)
	}
}
func TestExporterExplicitCredentialsOverrideEnvironment(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	p := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(p)
	t.Cleanup(func() {
		_ = Shutdown(context.Background())
		_ = p.Shutdown(context.Background())
		otel.SetTracerProvider(old)
	})
	headers := make(chan http.Header, 1)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		headers <- r.Header.Clone()
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(200)
	}))
	defer sink.Close()
	t.Setenv("FI_BASE_URL", sink.URL)
	t.Setenv("FI_API_KEY", "environment-api")
	t.Setenv("FI_SECRET_KEY", "environment-secret")
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "X-Api-Key=otel-api,X-Secret-Key=otel-secret")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "X-Api-Key=otel-traces-api,X-Secret-Key=otel-traces-secret")
	if _, err := InitForTest(context.Background(), WithCredentials("explicit-api-key", "explicit-secret-key")); err != nil {
		t.Fatal(err)
	}
	_, span := p.Tracer("app").Start(context.Background(), "test-export")
	span.End()
	if err := ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case h := <-headers:
		if h.Get("X-Api-Key") != "explicit-api-key" || h.Get("X-Secret-Key") != "explicit-secret-key" {
			t.Fatalf("exporter auth precedence: %v", h)
		}
	case <-time.After(time.Second):
		t.Fatal("no export")
	}
}

type callbackProcessor struct {
	flush    func(context.Context) error
	shutdown func(context.Context) error
}

func (*callbackProcessor) OnStart(context.Context, sdktrace.ReadWriteSpan) {}
func (*callbackProcessor) OnEnd(sdktrace.ReadOnlySpan)                     {}
func (p *callbackProcessor) ForceFlush(ctx context.Context) error {
	if p.flush != nil {
		return p.flush(ctx)
	}
	return nil
}
func (p *callbackProcessor) Shutdown(ctx context.Context) error {
	if p.shutdown != nil {
		return p.shutdown(ctx)
	}
	return nil
}
func TestBoundedAndReentrantFlushShutdown(t *testing.T) {
	for _, mode := range []string{"flush-deadline", "shutdown-deadline", "reentrant-flush", "reentrant-shutdown"} {
		t.Run(mode, func(t *testing.T) {
			_ = Shutdown(context.Background())
			old := otel.GetTracerProvider()
			p := sdktrace.NewTracerProvider()
			otel.SetTracerProvider(p)
			release := make(chan struct{})
			t.Cleanup(func() {
				close(release)
				_ = Shutdown(context.Background())
				_ = p.Shutdown(context.Background())
				otel.SetTracerProvider(old)
			})
			proc := &callbackProcessor{}
			switch mode {
			case "flush-deadline":
				proc.flush = func(context.Context) error { <-release; return nil }
			case "shutdown-deadline":
				proc.shutdown = func(context.Context) error { <-release; return nil }
			case "reentrant-flush":
				proc.flush = func(ctx context.Context) error { return ForceFlush(ctx) }
			case "reentrant-shutdown":
				proc.shutdown = func(ctx context.Context) error { return Shutdown(ctx) }
			}
			if _, err := InitForTest(context.Background(), WithSpanProcessor(proc)); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Millisecond)
			defer cancel()
			start := time.Now()
			var err error
			if strings.Contains(mode, "flush") {
				err = ForceFlush(ctx)
			} else {
				err = Shutdown(ctx)
			}
			if time.Since(start) > 150*time.Millisecond {
				t.Fatal("unbounded/reentrant callback")
			}
			if strings.HasSuffix(mode, "deadline") && !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("deadline ignored: %v", err)
			}
			if mode == "reentrant-flush" && (err == nil || !strings.Contains(err.Error(), "in progress")) {
				t.Fatalf("recursive flush not guarded: %v", err)
			}
			if mode == "reentrant-shutdown" && err != nil {
				t.Fatal(err)
			}
		})
	}
}
func TestAppProviderOwnershipSurvivesExplicitReconfiguration(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	p := sdktrace.NewTracerProvider()
	otel.SetTracerProvider(p)
	t.Cleanup(func() {
		_ = Shutdown(context.Background())
		_ = p.Shutdown(context.Background())
		otel.SetTracerProvider(old)
	})
	rec := tracetest.NewSpanRecorder()
	for i := 0; i < 3; i++ {
		if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(rec)); err != nil {
			t.Fatal(err)
		}
	}
	_, s := p.Tracer("app").Start(context.Background(), "once")
	s.End()
	if len(rec.Ended()) != 1 {
		t.Fatalf("duplicate processors: %d", len(rec.Ended()))
	}
	if err := Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	_, s = p.Tracer("app").Start(context.Background(), "after-shutdown")
	if !s.IsRecording() {
		t.Fatal("app-owned provider was shut down")
	}
	s.End()
	if len(rec.Ended()) != 1 {
		t.Fatal("agent processor not detached")
	}
}
func TestAudioTranscriptSideMasking(t *testing.T) {
	for _, operation := range []string{"transcription", "text_to_speech"} {
		for _, flag := range []string{"inputs", "outputs", "input-text", "output-text"} {
			t.Run(operation+"/"+flag, func(t *testing.T) {
				c := TraceConfig{HideInputs: flag == "inputs", HideOutputs: flag == "outputs", HideInputText: flag == "input-text", HideOutputText: flag == "output-text"}
				attrs := attributes(map[string]any{"gen_ai.operation.name": operation, "gen_ai.audio.transcript": "PRIVATE"}, Config{TraceConfig: c})
				got := ""
				for _, kv := range attrs {
					if kv.Key == "gen_ai.audio.transcript" {
						got = kv.Value.AsString()
					}
				}
				hidden := (operation == "text_to_speech" && (flag == "inputs" || flag == "input-text")) || (operation == "transcription" && (flag == "outputs" || flag == "output-text"))
				want := "PRIVATE"
				if hidden {
					want = RedactedValue
				}
				if got != want {
					t.Fatalf("transcript=%q want %q", got, want)
				}
			})
		}
	}
}

func TestInvalidFileValuesCannotBlockOverrides(t *testing.T) {
	_ = Shutdown(context.Background())
	old := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	t.Cleanup(func() { _ = Shutdown(context.Background()); otel.SetTracerProvider(old) })
	file := filepath.Join(t.TempDir(), "futureagi.toml")
	t.Setenv("FI_CONFIG_FILE", file)
	if err := os.WriteFile(file, []byte("sample_rate = [invalid array\nproject_type = \"broken\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FI_PROJECT_TYPE", "observe")
	stop, err := InitForTest(context.Background(), WithSampleRate(.5), WithSpanProcessor(tracetest.NewSpanRecorder()))
	if err != nil {
		t.Fatalf("explicit override blocked by invalid lower file value: %v", err)
	}
	_ = stop(context.Background())
	t.Setenv("FI_SAMPLE_RATE", ".75")
	stop, err = InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder()))
	if err != nil || Report().Config.SampleRate != .75 {
		t.Fatalf("env override blocked by invalid lower file value: %v", err)
	}
	_ = stop(context.Background())
	valid, err := readConfig()
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(file, []byte("not even a config line\n"), 0600); err != nil {
		t.Fatal(err)
	}
	stop, err = InitForTest(context.Background(), WithConfig(valid), WithSpanProcessor(tracetest.NewSpanRecorder()))
	if err != nil {
		t.Fatalf("complete config still parsed discarded file: %v", err)
	}
	_ = stop(context.Background())
}

func TestNativeAudioPreservesLengthMarker(t *testing.T) {
	payload := strings.Repeat("YWJj", 100)
	for _, body := range []string{
		`{"input_audio":{"data":"` + payload + `","format":"wav"}}`,
		`{"inlineData":{"mimeType":"audio/wav","data":"` + payload + `"}}`,
		`{"audio":{"data":"` + payload + `"}}`,
	} {
		got := safeString(body, Config{})
		if strings.Contains(got, payload) || !strings.Contains(got, "<omitted: 400 base64 chars>") {
			t.Fatalf("audio marker lost: %s", got)
		}
	}
}
