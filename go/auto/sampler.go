package auto

import (
	"log"
	"math"
	"os"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// configuredSampler is called only when activation creates a private provider.
// Application-owned providers keep their sampler, without parsing or warning
// about environment settings that the agent will not use.
func configuredSampler(c Config) sdktrace.Sampler {
	if c.sampleRateSet {
		return withSamplingRatio(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(c.SampleRate)), c.SampleRate, true)
	}
	fallback := withSamplingRatio(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(1)), 1, true)
	name := strings.ToLower(os.Getenv("OTEL_TRACES_SAMPLER"))
	switch name {
	case "":
		return fallback
	case "always_on":
		return sdktrace.AlwaysSample()
	case "always_off":
		return sdktrace.NeverSample()
	case "parentbased_always_on":
		return withSamplingRatio(sdktrace.ParentBased(sdktrace.AlwaysSample()), 1, true)
	case "parentbased_always_off":
		return withSamplingRatio(sdktrace.ParentBased(sdktrace.NeverSample()), 0, true)
	case "traceidratio", "parentbased_traceidratio":
		rate := 1.0
		var err error
		if arg := os.Getenv("OTEL_TRACES_SAMPLER_ARG"); arg != "" {
			rate, err = strconv.ParseFloat(arg, 64)
		}
		if err == nil && !math.IsNaN(rate) && !math.IsInf(rate, 0) && rate >= 0 && rate <= 1 {
			ratio := sdktrace.TraceIDRatioBased(rate)
			if name == "parentbased_traceidratio" {
				return withSamplingRatio(sdktrace.ParentBased(ratio), rate, true)
			}
			return withSamplingRatio(ratio, rate, false)
		}
	}
	// Activation happens once, so report one warning, never environment values.
	log.Print("traceai auto: invalid or unsupported OTEL_TRACES_SAMPLER/OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIDRatioBased(1))")
	return fallback
}

// withSamplingRatio is used only for the known samplers created above. All
// parent-based selections can copy a parent's ratio, regardless of the root rate.
// Do not infer ratios from arbitrary samplers or application-owned providers.
func withSamplingRatio(delegate sdktrace.Sampler, rate float64, parentBased bool) sdktrace.Sampler {
	if !parentBased && !validSamplingRatio(rate) {
		return delegate
	}
	return ratioSampler{delegate: delegate, rate: rate, parentBased: parentBased}
}

func validSamplingRatio(rate float64) bool {
	return !math.IsNaN(rate) && !math.IsInf(rate, 0) && rate > 0 && rate < 1
}

type ratioSampler struct {
	delegate    sdktrace.Sampler
	rate        float64
	parentBased bool
}

func (s ratioSampler) Description() string { return s.delegate.Description() }

// ratioForParent also lets wire admission reserve the sampler's attribute slot
// without calling the delegate. Only fractional root decisions reserve a slot;
// parent-based children without a known inherited ratio need no reservation.
func (s ratioSampler) ratioForParent(parent trace.SpanContext) (float64, bool) {
	if !s.parentBased || !parent.IsValid() {
		return s.rate, validSamplingRatio(s.rate)
	}
	if !parent.IsSampled() {
		return 0, false
	}
	value, ok := strings.CutPrefix(parent.TraceState().Get("fi"), "r:")
	if !ok {
		return 0, false
	}
	rate, err := strconv.ParseFloat(value, 64)
	return rate, err == nil && validSamplingRatio(rate)
}

func (s ratioSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	result := s.delegate.ShouldSample(p)
	if result.Decision != sdktrace.RecordAndSample {
		return result
	}

	parent := trace.SpanContextFromContext(p.ParentContext)
	rate, ok := s.ratioForParent(parent)
	if !ok {
		return result
	}
	if !s.parentBased || !parent.IsValid() {
		// The ratio made this decision. Insert replaces only fi; retain the
		// delegate's other state (subject to W3C's 32-entry limit).
		result.Tracestate, _ = result.Tracestate.Insert("fi", "r:"+strconv.FormatFloat(rate, 'g', -1, 64))
	}
	// A delegate may reuse its attributes and their backing array across calls.
	attrs := make([]attribute.KeyValue, len(result.Attributes), len(result.Attributes)+1)
	copy(attrs, result.Attributes)
	result.Attributes = append(attrs, attribute.Float64("fi.sampling.ratio", rate))
	return result
}
