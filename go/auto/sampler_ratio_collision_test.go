package auto

import (
	"context"
	"fmt"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	common "go.opentelemetry.io/proto/otlp/common/v1"
)

// Start attributes are applied after sampler attributes by the SDK. Exercise
// that boundary through real wire capture and the local OTLP collector.
func TestRatioSamplerWireContextAuthorityOTLP(t *testing.T) {
	for _, mode := range []string{"inherited", "traceidratio", "missing-fi", "invalid-fi", "host-explicit", "host-global", "rate-one", "always-on"} {
		t.Run(mode, func(t *testing.T) {
			owned := mode == "inherited" || mode == "traceidratio" || mode == "missing-fi" || mode == "invalid-fi"
			limits := []int{-1}
			if owned {
				limits = append(limits, 16)
			}
			for _, limit := range limits {
				t.Run(fmt.Sprint(limit), func(t *testing.T) {
					for _, override := range []struct {
						name string
						attr attribute.KeyValue
					}{
						{"control", attribute.KeyValue{}},
						{"numeric", attribute.Float64("fi.sampling.ratio", 0.9)},
						{"type", attribute.String("fi.sampling.ratio", "invalid-ratio")},
					} {
						t.Run(override.name, func(t *testing.T) {
							samplerEnvironment(t)
							t.Setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", fmt.Sprint(limit))
							t.Setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "-1")
							received := privacyCollector(t)
							options := []Option{WithCredentials("RATIO_CONTEXT_KEY", "RATIO_CONTEXT_SECRET"), WithWireCapture(true)}
							state, wantState := "vendor=kept,fi=r:0.5", "vendor=kept,fi=r:0.5"
							want := attribute.Float64Value(0.5)
							switch mode {
							case "traceidratio":
								t.Setenv("OTEL_TRACES_SAMPLER", "traceidratio")
								t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
								want, wantState = attribute.Float64Value(0.25), "fi=r:0.25,vendor=kept"
							case "host-explicit", "host-global":
								host := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
								t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
								if mode == "host-global" {
									otel.SetTracerProvider(host)
								} else {
									options = append(options, WithTracerProvider(host))
								}
								options = append(options, WithSampleRate(0.25))
							case "rate-one":
								options = append(options, WithSampleRate(1))
							case "always-on":
								t.Setenv("OTEL_TRACES_SAMPLER", "always_on")
							default:
								options = append(options, WithSampleRate(0.25))
								if mode == "missing-fi" {
									state, wantState, want = "vendor=kept", "vendor=kept", attribute.Value{}
								} else if mode == "invalid-fi" {
									state, wantState, want = "vendor=kept,fi=r:NaN", "vendor=kept,fi=r:NaN", attribute.Value{}
								}
							}
							if !owned {
								want = override.attr.Value
							}
							stop, err := InitForTest(context.Background(), options...)
							if err != nil {
								t.Fatal(err)
							}
							t.Cleanup(func() { _ = stop(context.Background()) })
							if (agent.current.ownedRatio != nil) != owned {
								t.Fatal("unexpected sampler ownership")
							}
							raw := tracetest.NewSpanRecorder()
							agent.current.provider.RegisterSpanProcessor(raw)
							ctx := ratioParent(t, true, true, state)
							if override.name != "control" {
								ctx = ContextWithAttributes(ctx, override.attr)
							}
							r6aLongChat(t, ctx, 8)
							request := flushedPrivacyExport(t, received)
							if len(raw.Ended()) != 1 {
								t.Fatalf("got %d SDK spans, want one", len(raw.Ended()))
							}
							span := raw.Ended()[0]
							if !span.SpanContext().IsSampled() || span.SpanContext().TraceState().String() != wantState {
								t.Error("sampling decision or tracestate changed")
							}
							values := map[string]attribute.Value{}
							for _, kv := range span.Attributes() {
								values[string(kv.Key)] = kv.Value
							}
							if got := values["fi.sampling.ratio"]; got != want {
								t.Errorf("SDK ratio = %v (%v), want %v (%v)", got, got.Type(), want, want.Type())
							}
							if span.DroppedAttributes() != 0 {
								t.Errorf("SDK dropped %d attributes", span.DroppedAttributes())
							}
							if limit > 0 && (len(values) != limit || !values["traceai.wire.attributes_truncated"].AsBool()) {
								t.Errorf("expected saturated budget %d with truncation marker, got %d attributes", limit, len(values))
							}
							exported := request.ResourceSpans[0].ScopeSpans[0].Spans[0]
							exportedState := ratioTraceState(t, exported.TraceState)
							if exportedState.Get("fi") != span.SpanContext().TraceState().Get("fi") || exportedState.Get("vendor") != "kept" || exported.Flags&1 == 0 || exported.DroppedAttributesCount != 0 || len(exported.Attributes) != len(values) {
								t.Errorf("export state=%q flags=%d dropped=%d attrs=%d; want state=%q sampled, dropped=0 attrs=%d", exported.TraceState, exported.Flags, exported.DroppedAttributesCount, len(exported.Attributes), wantState, len(values))
							}
							var got attribute.Value
							for _, kv := range exported.Attributes {
								if kv.Key != "fi.sampling.ratio" {
									continue
								}
								switch v := kv.Value.Value.(type) {
								case *common.AnyValue_DoubleValue:
									got = attribute.Float64Value(v.DoubleValue)
								case *common.AnyValue_StringValue:
									got = attribute.StringValue(v.StringValue)
								default:
									t.Errorf("unexpected exported ratio type %T", v)
								}
							}
							if got != want {
								t.Errorf("OTLP ratio = %v (%v), want %v (%v)", got, got.Type(), want, want.Type())
							}
						})
					}
				})
			}
		})
	}
}
