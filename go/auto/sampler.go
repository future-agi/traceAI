package auto

import (
	"log"
	"math"
	"os"
	"strconv"
	"strings"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// configuredSampler is called only when activation creates a private provider.
// Application-owned providers keep their sampler, without parsing or warning
// about environment settings that the agent will not use.
func configuredSampler(c Config) sdktrace.Sampler {
	if c.sampleRateSet {
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(c.SampleRate))
	}
	fallback := sdktrace.ParentBased(sdktrace.TraceIDRatioBased(1))
	name := strings.ToLower(os.Getenv("OTEL_TRACES_SAMPLER"))
	switch name {
	case "":
		return fallback
	case "always_on":
		return sdktrace.AlwaysSample()
	case "always_off":
		return sdktrace.NeverSample()
	case "parentbased_always_on":
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	case "parentbased_always_off":
		return sdktrace.ParentBased(sdktrace.NeverSample())
	case "traceidratio", "parentbased_traceidratio":
		rate := 1.0
		var err error
		if arg := os.Getenv("OTEL_TRACES_SAMPLER_ARG"); arg != "" {
			rate, err = strconv.ParseFloat(arg, 64)
		}
		if err == nil && !math.IsNaN(rate) && !math.IsInf(rate, 0) && rate >= 0 && rate <= 1 {
			ratio := sdktrace.TraceIDRatioBased(rate)
			if name == "parentbased_traceidratio" {
				return sdktrace.ParentBased(ratio)
			}
			return ratio
		}
	}
	// Activation happens once, so report one warning, never environment values.
	log.Print("traceai auto: invalid or unsupported OTEL_TRACES_SAMPLER/OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIDRatioBased(1))")
	return fallback
}
