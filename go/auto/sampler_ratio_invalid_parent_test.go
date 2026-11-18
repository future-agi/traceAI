package auto

import (
	"context"
	"strconv"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// Keep both newly generated and retained trace IDs below the fractional sampling
// threshold, and give every span a distinct ID, including zero-SpanID roots.
type invalidParentRatioIDGenerator struct{ next byte }

func (g *invalidParentRatioIDGenerator) NewIDs(ctx context.Context) (trace.TraceID, trace.SpanID) {
	id := trace.TraceID{1}
	return id, g.NewSpanID(ctx, id)
}

func (g *invalidParentRatioIDGenerator) NewSpanID(context.Context, trace.TraceID) trace.SpanID {
	g.next++
	return trace.SpanID{g.next + 16}
}

func TestRatioSamplerInvalidParentRootAndChild(t *testing.T) {
	for _, mode := range []struct {
		name, sampler, arg, fiRate string
		keep                       bool
		ratio                      float64
	}{
		{name: "default", keep: true},
		{name: "fi-one", sampler: "always_off", fiRate: "1", keep: true},
		{name: "fi-quarter", sampler: "always_off", fiRate: "0.25", keep: true, ratio: 0.25},
		{name: "fi-zero", sampler: "always_on", fiRate: "0"},
		{name: "parentbased-on", sampler: "parentbased_always_on", keep: true},
		{name: "parentbased-off", sampler: "parentbased_always_off"},
		{name: "parentbased-one", sampler: "parentbased_traceidratio", arg: "1", keep: true},
		{name: "parentbased-quarter", sampler: "parentbased_traceidratio", arg: "0.25", keep: true, ratio: 0.25},
		{name: "parentbased-zero", sampler: "parentbased_traceidratio", arg: "0"},
		{name: "always-on", sampler: "always_on", keep: true},
		{name: "always-off", sampler: "always_off"},
		{name: "ratio-one", sampler: "traceidratio", arg: "1", keep: true},
		{name: "ratio-quarter", sampler: "traceidratio", arg: "0.25", keep: true, ratio: 0.25},
		{name: "ratio-zero", sampler: "traceidratio", arg: "0"},
	} {
		t.Run(mode.name, func(t *testing.T) {
			samplerEnvironment(t)
			t.Setenv("OTEL_TRACES_SAMPLER", mode.sampler)
			t.Setenv("OTEL_TRACES_SAMPLER_ARG", mode.arg)
			if mode.fiRate != "" {
				t.Setenv("FI_SAMPLE_RATE", mode.fiRate)
			}
			config, err := readConfig()
			if err != nil {
				t.Fatal(err)
			}
			sampler := configuredSampler(config)
			contexts := []struct {
				name string
				ctx  context.Context
			}{{"absent", context.Background()}}
			for _, ids := range []struct {
				name    string
				traceID trace.TraceID
				spanID  trace.SpanID
			}{
				{name: "both-zero"},
				{name: "zero-trace-id", spanID: trace.SpanID{9}},
				{name: "zero-span-id", traceID: trace.TraceID{9}},
			} {
				for _, remote := range []bool{false, true} {
					for _, sampled := range []bool{false, true} {
						flags := trace.TraceFlags(0)
						if sampled {
							flags = trace.FlagsSampled
						}
						parent := trace.NewSpanContext(trace.SpanContextConfig{
							TraceID: ids.traceID, SpanID: ids.spanID, Remote: remote, TraceFlags: flags,
							TraceState: ratioTraceState(t, "vendor=kept,other=also-kept,fi=r:0.5"),
						})
						contexts = append(contexts, struct {
							name string
							ctx  context.Context
						}{ids.name + "/remote=" + strconv.FormatBool(remote) + "/sampled=" + strconv.FormatBool(sampled), trace.ContextWithSpanContext(context.Background(), parent)})
					}
				}
			}
			// Cleanup applies to vendor-only state and unparseable fi too, not
			// just contexts from which a fractional ratio could be copied.
			for _, state := range []string{"vendor=kept,other=also-kept", "vendor=kept,fi=r:invalid"} {
				parent := trace.NewSpanContext(trace.SpanContextConfig{
					TraceFlags: trace.FlagsSampled, Remote: true, TraceState: ratioTraceState(t, state),
				})
				contexts = append(contexts, struct {
					name string
					ctx  context.Context
				}{state, trace.ContextWithSpanContext(context.Background(), parent)})
			}
			for _, input := range contexts {
				t.Run(input.name, func(t *testing.T) {
					incoming := trace.SpanContextFromContext(input.ctx)
					if incoming.IsValid() {
						t.Fatal("fixture must not be a valid parent")
					}
					wantDecision := sdktrace.Drop
					wantState := ""
					if mode.keep {
						wantDecision = sdktrace.RecordAndSample
					}
					if mode.ratio != 0 {
						wantState = "fi=r:0.25"
					}
					result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: input.ctx, TraceID: trace.TraceID{1}})
					if result.Decision != wantDecision || result.Tracestate.String() != wantState {
						t.Errorf("root result decision=%v state=%q, want decision=%v state=%q", result.Decision, result.Tracestate, wantDecision, wantState)
					}
					checkRatioAttribute(t, result.Attributes, mode.ratio)
					if mode.ratio != 0 {
						// A rejected fractional root must not propagate invalid
						// incoming state either; no probabilistic ID selection.
						high := trace.TraceID{0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff}
						dropped := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: input.ctx, TraceID: high})
						if dropped.Decision != sdktrace.Drop || dropped.Tracestate.Len() != 0 {
							t.Errorf("rejected fractional root result = %+v, want Drop with empty state", dropped)
						}
						checkRatioAttribute(t, dropped.Attributes, 0)
					}

					recorder := tracetest.NewSpanRecorder()
					provider := sdktrace.NewTracerProvider(sdktrace.WithSampler(sampler), sdktrace.WithIDGenerator(&invalidParentRatioIDGenerator{}), sdktrace.WithSpanProcessor(recorder))
					t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })
					tracer := provider.Tracer("invalid-parent-ratio")
					ctx, root := tracer.Start(input.ctx, "root")
					_, child := tracer.Start(ctx, "child")
					for _, span := range []trace.Span{root, child} {
						if !span.SpanContext().IsValid() || span.IsRecording() != mode.keep || span.SpanContext().IsSampled() != mode.keep || span.SpanContext().TraceState().String() != wantState {
							t.Errorf("span recording=%v context=%+v, want valid context, recording/sample=%v state=%q", span.IsRecording(), span.SpanContext(), mode.keep, wantState)
						}
					}
					// The SDK retains a nonzero TraceID even when SpanID is zero;
					// that does not turn this into a parent-based sampling decision.
					wantTraceID := incoming.TraceID()
					if !wantTraceID.IsValid() {
						wantTraceID = trace.TraceID{1}
					}
					if root.SpanContext().TraceID() != wantTraceID || child.SpanContext().TraceID() != wantTraceID || child.SpanContext().SpanID() == root.SpanContext().SpanID() {
						t.Error("root/child trace continuity or distinct span IDs lost")
					}
					child.End()
					root.End()
					wantEnded := 0
					if mode.keep {
						wantEnded = 2
					}
					if len(recorder.Ended()) != wantEnded {
						t.Fatalf("recorded %d spans, want %d", len(recorder.Ended()), wantEnded)
					}
					for _, span := range recorder.Ended() {
						checkRatioAttribute(t, span.Attributes(), mode.ratio)
						if span.SpanContext().TraceState().String() != wantState || !span.SpanContext().IsSampled() {
							t.Errorf("%s: recorded state/sample flag changed", span.Name())
						}
						if span.Name() == "root" && span.Parent().IsValid() {
							t.Error("root has a valid parent")
						}
						if span.Name() == "child" && !span.Parent().Equal(root.SpanContext()) {
							t.Error("child does not refer to the new root")
						}
					}
					if !trace.SpanContextFromContext(input.ctx).Equal(incoming) {
						t.Error("incoming context was mutated")
					}
				})
			}
		})
	}
}

// Root-boundary cleanup must not strip vendor entries from valid parents when
// a non-parent-based sampler replaces fi with its own local fractional ratio.
func TestRatioSamplerValidParentNonParentFractionRetainsVendors(t *testing.T) {
	samplerEnvironment(t)
	t.Setenv("OTEL_TRACES_SAMPLER", "traceidratio")
	t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
	sampler := configuredSampler(Config{})
	for _, remote := range []bool{false, true} {
		for _, sampled := range []bool{false, true} {
			t.Run("remote="+strconv.FormatBool(remote)+"/sampled="+strconv.FormatBool(sampled), func(t *testing.T) {
				ctx := ratioParent(t, remote, sampled, "vendor=kept,other=also-kept,fi=r:0.5")
				incoming := trace.SpanContextFromContext(ctx)
				wantState := "fi=r:0.25,vendor=kept,other=also-kept"
				result := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ctx, TraceID: incoming.TraceID()})
				if result.Decision != sdktrace.RecordAndSample || result.Tracestate.String() != wantState {
					t.Errorf("valid-parent nonparent result = %+v, want sampled own ratio and retained vendors", result)
				}
				checkRatioAttribute(t, result.Attributes, 0.25)
				recorder := tracetest.NewSpanRecorder()
				provider := sdktrace.NewTracerProvider(sdktrace.WithSampler(sampler), sdktrace.WithIDGenerator(&invalidParentRatioIDGenerator{}), sdktrace.WithSpanProcessor(recorder))
				t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })
				_, span := provider.Tracer("valid-parent-ratio").Start(ctx, "child")
				if !span.IsRecording() || !span.SpanContext().IsSampled() || span.SpanContext().TraceState().String() != wantState || span.SpanContext().TraceID() != incoming.TraceID() {
					t.Error("valid parent's child lost sampling, trace continuity or vendor state")
				}
				span.End()
				if len(recorder.Ended()) != 1 {
					t.Fatalf("recorded %d spans, want one", len(recorder.Ended()))
				}
				checkRatioAttribute(t, recorder.Ended()[0].Attributes(), 0.25)
				if !recorder.Ended()[0].Parent().Equal(incoming) || !trace.SpanContextFromContext(ctx).Equal(incoming) {
					t.Error("valid parent context changed")
				}
			})
		}
	}
}
