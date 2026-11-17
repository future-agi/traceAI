package auto

import (
	"context"
	"fmt"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestRatioSamplerBudgetPrediction(t *testing.T) {
	for _, parentBased := range []bool{false, true} {
		delegate := &ratioResultSampler{}
		sampler := ratioSampler{delegate: delegate, rate: 0.25, parentBased: parentBased}
		for _, tc := range []struct {
			name string
			ctx  context.Context
			want bool
		}{
			{"root", context.Background(), true},
			{"inherited", ratioParent(t, true, true, "fi=r:0.5"), true},
			{"missing", ratioParent(t, true, true, "vendor=kept"), !parentBased},
			{"invalid", ratioParent(t, true, true, "fi=r:NaN"), !parentBased},
			{"unsampled", ratioParent(t, true, false, "fi=r:0.5"), !parentBased},
		} {
			if _, stamp := sampler.ratioForParent(trace.SpanContextFromContext(tc.ctx)); stamp != tc.want {
				t.Errorf("parentBased=%v/%s: stamp=%v, want %v", parentBased, tc.name, stamp, tc.want)
			}
		}
		if delegate.calls != 0 {
			t.Fatal("budget prediction called the sampling delegate")
		}
	}
}

// Exercise real wire capture, the sampled SDK container, and OTLP export. The
// unlimited control supplies the full content count and admission key set;
// sweeping constrained limits hits both full groups and one-slot boundaries.
func TestRatioSamplerWireBudgetOTLP(t *testing.T) {
	for _, mode := range []string{"inherited", "local-parent", "traceidratio", "missing-fi", "invalid-fi", "host", "rate-one"} {
		t.Run(mode, func(t *testing.T) {
			var full []attribute.KeyValue
			totalContent := 0
			limits := []int{-1, 0, 1, 2, 3, 16, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 40, 64}
			for _, limit := range limits {
				t.Run(fmt.Sprint(limit), func(t *testing.T) {
					samplerEnvironment(t)
					t.Setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", fmt.Sprint(limit))
					t.Setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "-1")
					received := privacyCollector(t)
					options := []Option{WithCredentials("RATIO_BUDGET_KEY", "RATIO_BUDGET_SECRET"), WithWireCapture(true)}
					wantRatio := 0.5
					state := "vendor=kept,fi=r:0.5"
					switch mode {
					case "traceidratio":
						t.Setenv("OTEL_TRACES_SAMPLER", "traceidratio")
						t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
						wantRatio = 0.25
					case "host":
						host := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
						t.Cleanup(func() { _ = host.Shutdown(context.Background()) })
						options = append(options, WithTracerProvider(host), WithSampleRate(0.25))
						wantRatio = 0
					case "rate-one":
						options = append(options, WithSampleRate(1))
						wantRatio = 0
					default:
						options = append(options, WithSampleRate(0.25))
						if mode == "missing-fi" {
							state, wantRatio = "vendor=kept", 0
						} else if mode == "invalid-fi" {
							state, wantRatio = "vendor=kept,fi=r:NaN", 0
						}
					}
					stop, err := InitForTest(context.Background(), options...)
					if err != nil {
						t.Fatal(err)
					}
					t.Cleanup(func() { _ = stop(context.Background()) })
					if (agent.current.ownedRatio != nil) != (mode != "host" && mode != "rate-one") {
						t.Fatal("reservation state does not match the owned sampler")
					}
					raw := tracetest.NewSpanRecorder()
					agent.current.provider.RegisterSpanProcessor(raw)
					ctx := ratioParent(t, mode != "local-parent", true, state)
					ctx = ContextWithAttributes(ctx,
						attribute.String("z.record.mimeType", "audio/L16"),
						attribute.String("z.record.data", r6aPCM))
					r6aLongChat(t, ctx, 8)
					request := flushedPrivacyExport(t, received)
					if len(raw.Ended()) != 1 {
						t.Fatalf("got %d SDK spans, want one", len(raw.Ended()))
					}
					span := raw.Ended()[0]
					if !span.SpanContext().IsSampled() {
						t.Fatal("wire span was not sampled")
					}
					wantDropped := 0
					if limit == 0 && wantRatio != 0 {
						// Zero retains its SDK semantics: even the sampler attr is dropped.
						wantDropped, wantRatio = 1, 0
					}
					checkRatioAttribute(t, span.Attributes(), wantRatio)
					if span.DroppedAttributes() != wantDropped {
						t.Fatalf("SDK dropped %d attrs, want %d; wire admission overflowed", span.DroppedAttributes(), wantDropped)
					}
					values := map[string]attribute.Value{}
					keptContent := 0
					for _, kv := range span.Attributes() {
						values[string(kv.Key)] = kv.Value
						if kv.Key == "fi.sampling.ratio" {
							continue
						}
						if limit < 0 {
							full = append(full, kv)
						}
						if protectedAttributePriority(kv) < 0 && contentGroup(string(kv.Key), "chat").content {
							keptContent++
						}
					}
					if limit < 0 {
						totalContent = keptContent
						if totalContent == 0 {
							t.Fatal("unlimited control has no content")
						}
					}
					wireLimit := limit
					if wireLimit > 0 && wantRatio != 0 {
						wireLimit--
					}
					expected := budgetAttributes(full, wireLimit)
					wantCount := len(expected)
					if wantRatio != 0 {
						wantCount++
					}
					if len(values) != wantCount {
						t.Fatalf("got %d attrs, want %d (wire budget %d)", len(values), wantCount, wireLimit)
					}
					for _, kv := range expected {
						if _, ok := values[string(kv.Key)]; !ok {
							t.Errorf("SDK lost admitted key %s", kv.Key)
						}
					}
					if dropped := totalContent - keptContent; dropped > 0 && wireLimit > 0 {
						if got := values["traceai.capture.dropped_content_count"].AsInt64(); got != int64(dropped) {
							t.Errorf("dropped content count=%d, want %d", got, dropped)
						}
						if wireLimit >= 2 && !values["traceai.wire.attributes_truncated"].AsBool() {
							t.Error("lost truncation marker")
						}
					}
					// At limit 1, the ratio wins; no wire marker can also fit.
					for _, pair := range [][2]string{{"output.value", "output.mime_type"}, {"input.value", "input.mime_type"}} {
						if (values[pair[0]].Type() != attribute.EMPTY) != (values[pair[1]].Type() != attribute.EMPTY) {
							t.Errorf("split content/MIME group %v", pair)
						}
					}
					for i := 0; i < 8; i++ {
						prefix := fmt.Sprintf("gen_ai.input.messages.%d.message.", i)
						if (values[prefix+"role"].Type() != attribute.EMPTY) != (values[prefix+"content"].Type() != attribute.EMPTY) {
							t.Errorf("split input message %d", i)
						}
					}
					if values["z.record.data"].Type() != attribute.EMPTY && values["z.record.mimeType"].Type() == attribute.EMPTY {
						t.Error("lost media companion")
					}
					exported := request.ResourceSpans[0].ScopeSpans[0].Spans[0]
					if len(exported.Attributes) != len(values) || exported.DroppedAttributesCount != uint32(wantDropped) {
						t.Fatal("export changed attribute count or SDK dropped count")
					}
					for _, kv := range exported.Attributes {
						if _, ok := values[kv.Key]; !ok {
							t.Errorf("export changed admitted key %s", kv.Key)
						}
						if kv.Key == "fi.sampling.ratio" && kv.Value.GetDoubleValue() != wantRatio {
							t.Error("export changed ratio double")
						}
						if kv.Key == "traceai.capture.dropped_content_count" && kv.Value.GetIntValue() != int64(totalContent-keptContent) {
							t.Error("export changed dropped-content accounting")
						}
					}
					if leaks := rc2Leaks(t, request, r6aPCM); len(leaks) != 0 {
						t.Fatalf("media leaked: %v", leaks)
					}
				})
			}
		})
	}
}
