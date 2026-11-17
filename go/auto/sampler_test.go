package auto

import (
	"bytes"
	"context"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func samplerEnvironment(t *testing.T) *bytes.Buffer {
	t.Helper()
	_ = Shutdown(context.Background())
	oldProvider := otel.GetTracerProvider()
	otel.SetTracerProvider(trace.NewNoopTracerProvider())
	t.Cleanup(func() { otel.SetTracerProvider(oldProvider) })
	for key, value := range map[string]string{
		"FI_CONFIG_FILE":          filepath.Join(t.TempDir(), "absent.toml"),
		"FI_API_KEY":              "",
		"FI_SECRET_KEY":           "",
		"FI_PROJECT_NAME":         "sampler-test",
		"FI_PROJECT_TYPE":         "observe",
		"FI_WIRE_MAX_BODY_BYTES":  "4194304",
		"FI_AUTO_DEBUG":           "false",
		"OTEL_SDK_DISABLED":       "false",
		"OTEL_TRACES_SAMPLER":     "",
		"OTEL_TRACES_SAMPLER_ARG": "",
	} {
		t.Setenv(key, value)
	}
	// Unlike an empty FI value, absence must not count as an explicit rate.
	t.Setenv("FI_SAMPLE_RATE", "")
	if err := os.Unsetenv("FI_SAMPLE_RATE"); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	oldOutput := log.Writer()
	log.SetOutput(&output)
	t.Cleanup(func() { log.SetOutput(oldOutput) })
	return &output
}

func startSamplerAgent(t *testing.T, options ...Option) (*sdktrace.TracerProvider, *tracetest.SpanRecorder) {
	t.Helper()
	recorder := tracetest.NewSpanRecorder()
	stop, err := InitForTest(context.Background(), append([]Option{WithWireCapture(false), WithSpanProcessor(recorder)}, options...)...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := stop(context.Background()); err != nil {
			t.Error(err)
		}
	})
	agent.Lock()
	state := agent.current
	agent.Unlock()
	if state == nil {
		t.Fatal("agent did not activate")
	}
	return state.provider, recorder
}

// Check both the sampling flag seen by callers and the processor's snapshots.
// want contains the root, sampled-parent and unsampled-parent decisions.
func checkSamplerDecisions(t *testing.T, provider *sdktrace.TracerProvider, recorder *tracetest.SpanRecorder, want [3]bool) {
	t.Helper()
	contexts := []struct {
		name string
		ctx  context.Context
		want bool
	}{{"root", context.Background(), want[0]}}
	for _, remote := range []bool{false, true} {
		for _, sampled := range []bool{false, true} {
			flags := trace.TraceFlags(0)
			decision := want[2]
			if sampled {
				flags = trace.FlagsSampled
				decision = want[1]
			}
			parent := trace.NewSpanContext(trace.SpanContextConfig{
				TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: flags, Remote: remote,
			})
			contexts = append(contexts, struct {
				name string
				ctx  context.Context
				want bool
			}{"remote=" + strconv.FormatBool(remote) + "/sampled=" + strconv.FormatBool(sampled), trace.ContextWithSpanContext(context.Background(), parent), decision})
		}
	}
	tracer := provider.Tracer("sampling")
	wantEnded := 0
	for _, test := range contexts {
		_, span := tracer.Start(test.ctx, test.name)
		if span.IsRecording() != test.want || span.SpanContext().IsSampled() != test.want {
			t.Errorf("%s: recording=%v sampled=%v, want %v", test.name, span.IsRecording(), span.SpanContext().IsSampled(), test.want)
		}
		span.End()
		if test.want {
			wantEnded++
		}
	}
	if got := len(recorder.Ended()); got != wantEnded {
		t.Errorf("processor received %d spans, want %d", got, wantEnded)
	}
	for _, span := range recorder.Ended() {
		if !span.SpanContext().IsSampled() {
			t.Errorf("processor received an unsampled span: %s", span.Name())
		}
	}
}

func TestOwnedProviderEnvironmentSampling(t *testing.T) {
	for _, test := range []struct {
		name    string
		sampler string
		arg     string
		want    [3]bool
	}{
		{"default", "", "", [3]bool{true, true, false}},
		{"always_on", "always_on", "ignored", [3]bool{true, true, true}},
		{"always_off", "always_off", "ignored", [3]bool{false, false, false}},
		{"traceidratio_zero", "traceidratio", "0", [3]bool{false, false, false}},
		{"traceidratio_one", "traceidratio", "1", [3]bool{true, true, true}},
		{"traceidratio_default_arg", "traceidratio", "", [3]bool{true, true, true}},
		{"parentbased_always_on", "parentbased_always_on", "ignored", [3]bool{true, true, false}},
		{"parentbased_always_off", "parentbased_always_off", "ignored", [3]bool{false, true, false}},
		{"parentbased_traceidratio_zero", "parentbased_traceidratio", "0", [3]bool{false, true, false}},
		{"parentbased_traceidratio_one", "parentbased_traceidratio", "1", [3]bool{true, true, false}},
		{"parentbased_traceidratio_default_arg", "parentbased_traceidratio", "", [3]bool{true, true, false}},
		{"case_insensitive", "ALWAYS_OFF", "", [3]bool{false, false, false}},
	} {
		t.Run(test.name, func(t *testing.T) {
			output := samplerEnvironment(t)
			t.Setenv("OTEL_TRACES_SAMPLER", test.sampler)
			t.Setenv("OTEL_TRACES_SAMPLER_ARG", test.arg)
			provider, recorder := startSamplerAgent(t)
			checkSamplerDecisions(t, provider, recorder, test.want)
			if output.Len() != 0 {
				t.Fatalf("valid sampler produced a warning: %s", output.String())
			}
		})
	}
}

func TestOwnedProviderInvalidEnvironmentSampler(t *testing.T) {
	for _, test := range []struct {
		name    string
		sampler string
		arg     string
	}{
		{"unknown", "not_a_sampler", ""},
		{"unsupported", "jaeger_remote", ""},
		{"malformed_ratio", "traceidratio", "not_a_number"},
		{"negative_ratio", "traceidratio", "-0.1"},
		{"large_ratio", "traceidratio", "1.1"},
		{"nan_ratio", "traceidratio", "NaN"},
		{"infinite_ratio", "traceidratio", "+Inf"},
		{"negative_infinite_ratio", "traceidratio", "-Inf"},
		{"parentbased_bad_ratio", "parentbased_traceidratio", "not_a_number"},
	} {
		t.Run(test.name, func(t *testing.T) {
			output := samplerEnvironment(t)
			t.Setenv("OTEL_TRACES_SAMPLER", test.sampler)
			t.Setenv("OTEL_TRACES_SAMPLER_ARG", test.arg)
			provider, recorder := startSamplerAgent(t)
			checkSamplerDecisions(t, provider, recorder, [3]bool{true, true, false})
			if strings.Count(output.String(), "traceai auto:") != 1 || !strings.Contains(output.String(), "OTEL_TRACES_SAMPLER") {
				t.Fatalf("want one sampler warning, got %q", output.String())
			}
		})
	}
}

func TestExplicitSampleRateOverridesEnvironmentSampler(t *testing.T) {
	for _, source := range []string{"environment", "option", "config_file", "full_config"} {
		for _, rate := range []float64{0, 1} {
			t.Run(source+"/rate="+strconv.FormatFloat(rate, 'f', -1, 64), func(t *testing.T) {
				output := samplerEnvironment(t)
				// The OTEL choice conflicts with the explicit rate in both directions.
				name := "always_on"
				if rate == 1 {
					name = "always_off"
				}
				t.Setenv("OTEL_TRACES_SAMPLER", name)
				var options []Option
				switch source {
				case "environment":
					t.Setenv("FI_SAMPLE_RATE", strconv.FormatFloat(rate, 'f', -1, 64))
				case "option":
					t.Setenv("FI_SAMPLE_RATE", "invalid_but_overridden")
					options = append(options, WithSampleRate(rate))
				case "config_file":
					path := filepath.Join(t.TempDir(), "futureagi.toml")
					if err := os.WriteFile(path, []byte("sample_rate = "+strconv.FormatFloat(rate, 'f', -1, 64)+"\n"), 0600); err != nil {
						t.Fatal(err)
					}
					t.Setenv("FI_CONFIG_FILE", path)
				case "full_config":
					config, err := readConfig()
					if err != nil {
						t.Fatal(err)
					}
					config.SampleRate = rate
					options = append(options, WithConfig(config))
				}
				provider, recorder := startSamplerAgent(t, options...)
				checkSamplerDecisions(t, provider, recorder, [3]bool{rate == 1, true, false})
				if output.Len() != 0 {
					t.Fatalf("overridden sampler produced a warning: %s", output.String())
				}
			})
		}
	}
}

func TestExplicitSampleRateIgnoresInvalidEnvironmentSampler(t *testing.T) {
	output := samplerEnvironment(t)
	t.Setenv("OTEL_TRACES_SAMPLER", "not_a_sampler")
	t.Setenv("OTEL_TRACES_SAMPLER_ARG", "not_a_number")
	provider, recorder := startSamplerAgent(t, WithSampleRate(0))
	checkSamplerDecisions(t, provider, recorder, [3]bool{false, true, false})
	// sdktrace.NewTracerProvider parses OTEL_TRACES_SAMPLER itself and reports an invalid value through the
	// global OpenTelemetry error handler even when WithSampler overrides it; only the agent's own warning is ours.
	if strings.Contains(output.String(), "traceai auto:") {
		t.Fatalf("unused OTEL sampler produced an agent warning: %s", output.String())
	}
}

func TestApplicationProviderSamplerUnchanged(t *testing.T) {
	for _, mode := range []string{"global", "explicit"} {
		for _, sampled := range []bool{false, true} {
			t.Run(mode+"/sampled="+strconv.FormatBool(sampled), func(t *testing.T) {
				output := samplerEnvironment(t)
				sampler := sdktrace.NeverSample()
				if sampled {
					sampler = sdktrace.AlwaysSample()
				}
				host := sdktrace.NewTracerProvider(sdktrace.WithSampler(sampler))
				t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
				// Neither standard env nor an explicit FI option owns host sampling.
				t.Setenv("OTEL_TRACES_SAMPLER", "not_a_sampler")
				options := []Option{WithSampleRate(1)}
				if sampled {
					options = []Option{WithSampleRate(0)}
				}
				if mode == "global" {
					otel.SetTracerProvider(host)
				} else {
					options = append(options, WithTracerProvider(host))
				}
				global := otel.GetTracerProvider()
				provider, recorder := startSamplerAgent(t, options...)
				if provider != host || otel.GetTracerProvider() != global {
					t.Fatal("application provider was replaced")
				}
				checkSamplerDecisions(t, provider, recorder, [3]bool{sampled, sampled, sampled})
				if output.Len() != 0 {
					t.Fatalf("host sampler produced a warning: %s", output.String())
				}
			})
		}
	}
}
