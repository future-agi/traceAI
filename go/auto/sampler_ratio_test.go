package auto

import (
	"context"
	"math"
	"reflect"
	"strconv"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	common "go.opentelemetry.io/proto/otlp/common/v1"
)

func ratioTraceState(t *testing.T, value string) trace.TraceState {
	t.Helper()
	state, err := trace.ParseTraceState(value)
	if err != nil {
		t.Fatal(err)
	}
	return state
}

func ratioParent(t *testing.T, remote, sampled bool, state string) context.Context {
	t.Helper()
	flags := trace.TraceFlags(0)
	if sampled {
		flags = trace.FlagsSampled
	}
	return trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, Remote: remote,
		TraceFlags: flags, TraceState: ratioTraceState(t, state),
	}))
}

func checkRatioAttribute(t *testing.T, attrs []attribute.KeyValue, want float64) {
	t.Helper()
	found := 0
	for _, attr := range attrs {
		if attr.Key != "fi.sampling.ratio" {
			continue
		}
		found++
		if want == 0 || attr.Value.Type() != attribute.FLOAT64 || attr.Value.AsFloat64() != want {
			t.Errorf("ratio attribute = %v, want double %v (0 means absent)", attr, want)
		}
	}
	if want != 0 && found != 1 {
		t.Errorf("found %d ratio attributes, want one", found)
	}
}

type ratioIDGenerator struct{}

func (ratioIDGenerator) NewIDs(context.Context) (trace.TraceID, trace.SpanID) {
	return trace.TraceID{1}, trace.SpanID{1}
}
func (ratioIDGenerator) NewSpanID(context.Context, trace.TraceID) trace.SpanID {
	return trace.SpanID{2}
}

func TestRatioSamplerRootAndLocalChild(t *testing.T) {
	samplerEnvironment(t)
	recorder := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(
		sdktrace.WithSampler(configuredSampler(Config{SampleRate: 0.25, sampleRateSet: true})),
		sdktrace.WithIDGenerator(ratioIDGenerator{}), sdktrace.WithSpanProcessor(recorder),
	)
	t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })
	ctx, root := provider.Tracer("ratio").Start(context.Background(), "root", trace.WithAttributes(attribute.String("existing", "root")))
	_, child := provider.Tracer("ratio").Start(ctx, "child")
	child.End()
	root.End()
	if len(recorder.Ended()) != 2 {
		t.Fatalf("got %d spans, want root and child", len(recorder.Ended()))
	}
	for _, span := range recorder.Ended() {
		checkRatioAttribute(t, span.Attributes(), 0.25)
		if !span.SpanContext().IsSampled() || span.SpanContext().TraceState().Get("fi") != "r:0.25" {
			t.Errorf("%s: sampled flag or ratio tracestate missing", span.Name())
		}
		if span.Name() == "root" {
			found := false
			for _, attr := range span.Attributes() {
				found = found || attr == attribute.String("existing", "root")
			}
			if !found {
				t.Error("existing span attribute changed")
			}
		}
	}
}

func TestRatioSamplerParentState(t *testing.T) {
	for _, mode := range []string{"explicit-quarter", "explicit-zero", "explicit-one", "", "parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio"} {
		t.Run(mode, func(t *testing.T) {
			samplerEnvironment(t)
			config := Config{}
			switch mode {
			case "explicit-quarter":
				config = Config{SampleRate: 0.25, sampleRateSet: true}
			case "explicit-zero":
				config = Config{SampleRate: 0, sampleRateSet: true}
			case "explicit-one":
				config = Config{SampleRate: 1, sampleRateSet: true}
			default:
				t.Setenv("OTEL_TRACES_SAMPLER", mode)
			}
			sampler := configuredSampler(config)
			for _, remote := range []bool{false, true} {
				for _, value := range []string{"", "r:0.5", "r:0.125", "r:2", "r:1", "r:0", "r:-0.5", "r:abc", "r:NaN", "r:+Inf", "r:-Inf", "r:1e-999", "r:", "0.5", "x:0.5"} {
					t.Run("remote="+strconv.FormatBool(remote)+"/fi="+value, func(t *testing.T) {
						state := "vendor=preserved"
						if value != "" {
							state += ",fi=" + value
						}
						for _, sampled := range []bool{false, true} {
							result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ratioParent(t, remote, sampled, state), TraceID: trace.TraceID{1}})
							want := 0.0
							if sampled && (value == "r:0.5" || value == "r:0.125") {
								want, _ = strconv.ParseFloat(value[2:], 64)
							}
							checkRatioAttribute(t, result.Attributes, want)
							decision := sdktrace.Drop
							if sampled {
								decision = sdktrace.RecordAndSample
							}
							if result.Decision != decision || result.Tracestate.String() != state {
								t.Errorf("sampled=%v: decision or inherited tracestate changed: %+v", sampled, result)
							}
						}
					})
				}
			}
		})
	}
}

func TestRatioSamplerNonParentBased(t *testing.T) {
	samplerEnvironment(t)
	t.Setenv("OTEL_TRACES_SAMPLER", "traceidratio")
	t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
	sampler := configuredSampler(Config{})
	for _, remote := range []bool{false, true} {
		for _, sampled := range []bool{false, true} {
			for _, inherited := range []string{"r:0.5", "r:abc"} {
				result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ratioParent(t, remote, sampled, "vendor=preserved,fi="+inherited), TraceID: trace.TraceID{1}})
				checkRatioAttribute(t, result.Attributes, 0.25)
				if result.Decision != sdktrace.RecordAndSample || result.Tracestate.String() != "fi=r:0.25,vendor=preserved" {
					t.Errorf("non-parent ratio did not replace inherited ratio: %+v", result)
				}
			}
		}
	}
}

func TestRatioSamplerNonFractionalSelections(t *testing.T) {
	for _, name := range []string{"", "always_on", "always_off", "parentbased_always_on", "parentbased_always_off", "traceidratio", "parentbased_traceidratio", "unknown"} {
		for _, rate := range []string{"0", "1", "NaN", "+Inf", "-0.1", "1.1"} {
			t.Run(name+"/"+rate, func(t *testing.T) {
				samplerEnvironment(t)
				t.Setenv("OTEL_TRACES_SAMPLER", name)
				t.Setenv("OTEL_TRACES_SAMPLER_ARG", rate)
				sampler := configuredSampler(Config{})
				// Invalid ratio arguments fall back to the default parent-based sampler.
				parentBased := name != "always_on" && name != "always_off" && !(name == "traceidratio" && (rate == "0" || rate == "1"))
				for _, ctx := range []context.Context{context.Background(), ratioParent(t, true, true, "vendor=preserved,fi=r:0.5"), ratioParent(t, false, true, "vendor=preserved,fi=r:0.5")} {
					result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ctx, TraceID: trace.TraceID{1}})
					want := 0.0
					if parentBased && trace.SpanContextFromContext(ctx).IsValid() {
						want = 0.5
					}
					checkRatioAttribute(t, result.Attributes, want)
					if result.Tracestate.String() != trace.SpanContextFromContext(ctx).TraceState().String() {
						t.Error("non-fractional selection changed tracestate")
					}
				}
			})
		}
	}
}

func TestRatioSamplerOwnedProviderSelection(t *testing.T) {
	for _, source := range []string{"option", "fi_env", "otel_parent", "otel_ratio"} {
		t.Run(source, func(t *testing.T) {
			samplerEnvironment(t)
			var options []Option
			switch source {
			case "option":
				t.Setenv("OTEL_TRACES_SAMPLER", "always_off")
				options = append(options, WithSampleRate(0.25))
			case "fi_env":
				t.Setenv("FI_SAMPLE_RATE", "0.25")
				t.Setenv("OTEL_TRACES_SAMPLER", "always_off")
			case "otel_parent":
				t.Setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
				t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
			case "otel_ratio":
				t.Setenv("OTEL_TRACES_SAMPLER", "traceidratio")
				t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
			}
			provider, recorder := startSamplerAgent(t, options...)
			_, span := provider.Tracer("ratio").Start(ratioParent(t, true, true, "vendor=preserved,fi=r:0.5"), "child")
			span.End()
			if len(recorder.Ended()) != 1 {
				t.Fatalf("got %d spans, want one", len(recorder.Ended()))
			}
			want, state := 0.5, "vendor=preserved,fi=r:0.5"
			if source == "otel_ratio" {
				want, state = 0.25, "fi=r:0.25,vendor=preserved"
			}
			checkRatioAttribute(t, recorder.Ended()[0].Attributes(), want)
			if span.SpanContext().TraceState().String() != state {
				t.Error("owned provider did not propagate expected ratio state")
			}
			// The existing export sanitizer rebuilds tracestate in reverse order;
			// verify exported entries without imposing propagation order on it.
			exportedState := recorder.Ended()[0].SpanContext().TraceState()
			if exportedState.Get("fi") != ratioTraceState(t, state).Get("fi") || exportedState.Get("vendor") != "preserved" {
				t.Error("exported ratio or other tracestate entry changed")
			}
		})
	}
}

// Full-rate root decisions must not disable copying upstream sampling metadata.
// Exercise activation, a remote parent, a real local child and OTLP serialization.
func TestRatioSamplerFullRateParentExport(t *testing.T) {
	for _, mode := range []string{"default", "parentbased_always_on", "parentbased_traceidratio", "fi-one", "option-one", "always_on", "root-default", "unsampled-default"} {
		t.Run(mode, func(t *testing.T) {
			samplerEnvironment(t)
			t.Setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "128")
			t.Setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "-1")
			received := privacyCollector(t)
			options := []Option{WithCredentials("RATIO_COPY_KEY", "RATIO_COPY_SECRET"), WithWireCapture(false)}
			switch mode {
			case "parentbased_always_on", "parentbased_traceidratio", "always_on":
				t.Setenv("OTEL_TRACES_SAMPLER", mode)
				t.Setenv("OTEL_TRACES_SAMPLER_ARG", "1")
			case "fi-one":
				t.Setenv("FI_SAMPLE_RATE", "1")
				t.Setenv("OTEL_TRACES_SAMPLER", "always_off")
			case "option-one":
				options = append(options, WithSampleRate(1))
			}
			stop, err := InitForTest(context.Background(), options...)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = stop(context.Background()) })
			ctx := ratioParent(t, true, mode != "unsampled-default", "vendor=preserved,fi=r:0.125")
			wantRatio, wantState := 0.125, "vendor=preserved,fi=r:0.125"
			if mode == "root-default" {
				ctx, wantRatio, wantState = context.Background(), 0, ""
			} else if mode == "always_on" {
				wantRatio = 0
			}
			tracer := agent.current.provider.Tracer("ratio-copy")
			ctx, parent := tracer.Start(ctx, "remote-child")
			_, child := tracer.Start(ctx, "local-child")
			for _, span := range []trace.Span{child, parent} {
				if span.SpanContext().TraceState().String() != wantState || span.SpanContext().IsSampled() != (mode != "unsampled-default") {
					t.Error("sampling flag or parent state changed")
				}
				span.End()
			}
			if mode == "unsampled-default" {
				if err := ForceFlush(context.Background()); err != nil {
					t.Fatal(err)
				}
				select {
				case <-received:
					t.Fatal("unsampled parent's descendants were exported")
				default:
				}
				return
			}
			request := flushedPrivacyExport(t, received)
			count := 0
			for _, resource := range request.ResourceSpans {
				for _, scope := range resource.ScopeSpans {
					for _, span := range scope.Spans {
						count++
						found := 0
						for _, kv := range span.Attributes {
							if kv.Key == "fi.sampling.ratio" {
								found++
								value, ok := kv.Value.Value.(*common.AnyValue_DoubleValue)
								if !ok || wantRatio == 0 || value.DoubleValue != wantRatio {
									t.Errorf("%s: exported ratio = %v, want double %v", span.Name, kv.Value, wantRatio)
								}
							}
						}
						if wantRatio != 0 && found != 1 {
							t.Errorf("%s: found %d ratios, want one", span.Name, found)
						}
						state := ratioTraceState(t, span.TraceState)
						expected := ratioTraceState(t, wantState)
						if state.Get("fi") != expected.Get("fi") || state.Get("vendor") != expected.Get("vendor") || span.Flags&1 == 0 {
							t.Error("export changed sampled flag or parent state")
						}
					}
				}
			}
			if count != 2 {
				t.Fatalf("exported %d spans, want remote child and local child", count)
			}
		})
	}
}

func TestRatioSamplerApplicationProviderUnchanged(t *testing.T) {
	for _, mode := range []string{"global", "explicit"} {
		t.Run(mode, func(t *testing.T) {
			samplerEnvironment(t)
			host := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
			t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
			options := []Option{WithSampleRate(0.25)}
			if mode == "global" {
				otel.SetTracerProvider(host)
			} else {
				options = append(options, WithTracerProvider(host))
			}
			global := otel.GetTracerProvider()
			provider, recorder := startSamplerAgent(t, options...)
			if provider != host || otel.GetTracerProvider() != global {
				t.Fatal("application provider replaced")
			}
			for _, ctx := range []context.Context{context.Background(), ratioParent(t, true, true, "vendor=preserved,fi=r:0.5")} {
				_, span := provider.Tracer("ratio").Start(ctx, "host")
				if span.SpanContext().TraceState().String() != trace.SpanContextFromContext(ctx).TraceState().String() {
					t.Error("host tracestate changed")
				}
				span.End()
			}
			if len(recorder.Ended()) != 2 {
				t.Fatalf("got %d host spans, want two", len(recorder.Ended()))
			}
			for _, span := range recorder.Ended() {
				checkRatioAttribute(t, span.Attributes(), 0)
			}
		})
	}
}

type ratioResultSampler struct {
	result sdktrace.SamplingResult
	calls  int
	params sdktrace.SamplingParameters
}

func (s *ratioResultSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	s.calls++
	s.params = p
	return s.result
}
func (*ratioResultSampler) Description() string { return "test delegate" }

func TestRatioSamplerDelegatePreservation(t *testing.T) {
	for _, decision := range []sdktrace.SamplingDecision{sdktrace.Drop, sdktrace.RecordOnly, sdktrace.RecordAndSample} {
		for _, parentBased := range []bool{false, true} {
			attrs := make([]attribute.KeyValue, 1, 2)
			attrs[0] = attribute.String("delegate", "preserved")
			delegate := &ratioResultSampler{result: sdktrace.SamplingResult{Decision: decision, Attributes: attrs, Tracestate: ratioTraceState(t, "delegate=kept,fi=r:0.75")}}
			original := sdktrace.SamplingResult{Decision: decision, Attributes: append([]attribute.KeyValue(nil), attrs...), Tracestate: delegate.result.Tracestate}
			sampler := withSamplingRatio(delegate, 0.25, parentBased)
			params := sdktrace.SamplingParameters{ParentContext: ratioParent(t, true, true, "parent=kept,fi=r:0.5"), TraceID: trace.TraceID{1}, Name: "span", Attributes: []attribute.KeyValue{attribute.String("input", "preserved")}}
			result := sampler.ShouldSample(params)
			if result.Decision != decision || delegate.calls != 1 || !reflect.DeepEqual(delegate.params, params) || sampler.Description() != delegate.Description() {
				t.Error("wrapper changed delegate invocation, decision or description")
			}
			if !reflect.DeepEqual(original, delegate.result) || !reflect.DeepEqual(attrs[:cap(attrs)][1], attribute.KeyValue{}) {
				t.Error("wrapper mutated delegate attributes or backing array")
			}
			if decision != sdktrace.RecordAndSample {
				if !reflect.DeepEqual(result, original) {
					t.Error("non-sampled result was modified")
				}
				continue
			}
			want, state := 0.25, "fi=r:0.25,delegate=kept"
			if parentBased {
				want, state = 0.5, "delegate=kept,fi=r:0.75"
			}
			checkRatioAttribute(t, result.Attributes, want)
			if result.Tracestate.String() != state || result.Attributes[0] != attrs[0] {
				t.Error("wrapper did not preserve delegate attributes/tracestate")
			}
		}
	}
}

func TestRatioSamplerCopyOnlyDelegatePreservation(t *testing.T) {
	for _, rate := range []float64{0, 1} {
		for _, decision := range []sdktrace.SamplingDecision{sdktrace.Drop, sdktrace.RecordOnly, sdktrace.RecordAndSample} {
			for _, inherited := range []bool{false, true} {
				original := sdktrace.SamplingResult{Decision: decision, Attributes: []attribute.KeyValue{attribute.String("delegate", "kept")}, Tracestate: ratioTraceState(t, "vendor=kept,fi=r:0.5")}
				delegate := &ratioResultSampler{result: original}
				sampler := withSamplingRatio(delegate, rate, true)
				ctx := context.Background()
				if inherited {
					ctx = ratioParent(t, true, true, "fi=r:0.5")
				}
				result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ctx, TraceID: trace.TraceID{1}})
				if decision == sdktrace.RecordAndSample && inherited {
					checkRatioAttribute(t, result.Attributes, 0.5)
				} else if !reflect.DeepEqual(result, original) {
					t.Error("copy-only wrapper modified a root or non-sampled result")
				}
				if result.Decision != decision || result.Tracestate.String() != original.Tracestate.String() || !reflect.DeepEqual(delegate.result, original) || delegate.calls != 1 {
					t.Error("copy-only wrapper changed delegate result or invocation")
				}
			}
		}
	}
}

func TestRatioSamplerRatioGuardAndFormatting(t *testing.T) {
	for _, rate := range []float64{0, 1, -1, 2, math.NaN(), math.Inf(1), math.Inf(-1)} {
		delegate := &ratioResultSampler{result: sdktrace.SamplingResult{Decision: sdktrace.RecordAndSample}}
		if withSamplingRatio(delegate, rate, false) != delegate {
			t.Errorf("inactive ratio %v wrapped a sampler", rate)
		}
	}
	for _, rate := range []float64{0.25, 0.12345678901234568, math.SmallestNonzeroFloat64, math.Nextafter(1, 0)} {
		sampler := withSamplingRatio(sdktrace.AlwaysSample(), rate, true)
		// An invalid parent context is a root decision, even with a sampled bit and fi entry.
		ctx := trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{TraceFlags: trace.FlagsSampled, TraceState: ratioTraceState(t, "vendor=preserved,fi=r:0.5")}))
		result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ctx, TraceID: trace.TraceID{1}})
		checkRatioAttribute(t, result.Attributes, rate)
		if result.Tracestate.Get("fi") != "r:"+strconv.FormatFloat(rate, 'g', -1, 64) || result.Tracestate.Get("vendor") != "preserved" {
			t.Errorf("ratio %v not serialized losslessly or other state lost", rate)
		}
	}
}

func TestRatioSamplerDroppedSpansNotExported(t *testing.T) {
	samplerEnvironment(t)
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSampler(configuredSampler(Config{SampleRate: 0.25, sampleRateSet: true})), sdktrace.WithSyncer(exporter))
	t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })
	_, span := provider.Tracer("ratio").Start(ratioParent(t, true, false, "fi=r:0.5"), "dropped")
	if span.IsRecording() || span.SpanContext().IsSampled() {
		t.Error("unsampled parent's child was kept")
	}
	span.End()
	// A high TraceID must also be dropped when the ratio itself decides.
	sampler := configuredSampler(Config{SampleRate: 0.25, sampleRateSet: true})
	result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: context.Background(), TraceID: trace.TraceID{0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff}})
	if result.Decision != sdktrace.Drop || len(result.Attributes) != 0 || result.Tracestate.Len() != 0 {
		t.Error("dropped root was stamped or decision changed")
	}
	if len(exporter.GetSpans()) != 0 {
		t.Error("dropped spans were exported")
	}
}
