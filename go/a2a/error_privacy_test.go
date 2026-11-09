package traceaia2a_test

import (
	"context"
	"strings"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
)

func TestErrorDetailsPolicy(t *testing.T) {
	contentFlags := []string{"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT"}
	for _, flag := range append([]string{"none", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS"}, contentFlags...) {
		for _, override := range []string{"", "true", " TrUe ", "false", " FaLsE ", "invalid", "1"} {
			t.Run(flag+"/"+override, func(t *testing.T) {
				for _, name := range append(append([]string{}, contentFlags...), "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS") {
					t.Setenv(name, "false")
				}
				if flag != "none" {
					t.Setenv(flag, "true")
				}
				t.Setenv("FI_HIDE_ERROR_DETAILS", override)
				wantHidden := false
				for _, name := range contentFlags {
					wantHidden = wantHidden || flag == name
				}
				switch strings.ToLower(strings.TrimSpace(override)) {
				case "true":
					wantHidden = true
				case "false":
					wantHidden = false
				}
				config := traceaia2a.TraceConfigFromEnv()
				for _, key := range []string{traceaia2a.ErrorMessage, "exception.message", "exception.stacktrace"} {
					if _, keep := config.Mask(attribute.String(key, marker)); keep == wantHidden {
						t.Errorf("%s keep=%v, hidden=%v", key, keep, wantHidden)
					}
				}
				for _, key := range []string{traceaia2a.ErrorType, "exception.type"} {
					if _, keep := config.Mask(attribute.String(key, "UNAUTHORIZED")); !keep {
						t.Errorf("error class %s was hidden", key)
					}
				}
				// Exercise the same derived/default policy on actual client errors.
				tp, rec := serverRecorder(t)
				i := traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp))
				ctx, _, err := i.Before(context.Background(), &a2aclient.Request{Method: "GetTask"})
				if err != nil {
					t.Fatal(err)
				}
				rejection := a2a.NewError(a2a.ErrUnauthorized, marker)
				if err := i.After(ctx, &a2aclient.Response{Method: "GetTask", Err: rejection}); err != nil {
					t.Fatal(err)
				}
				span := only(t, rec)
				if span.Status().Code != codes.Error || str(attrs(span), traceaia2a.ErrorType) != "UNAUTHORIZED" {
					t.Fatalf("error classification lost: %v", span.Status())
				}
				if strings.Contains(everything(span), marker) == wantHidden {
					t.Errorf("error text policy mismatch: hidden=%v", wantHidden)
				}
				if wantHidden && span.Status().Description != "" {
					t.Error("hidden error retained a status description")
				}
			})
		}
	}
}

func TestErrorDetailsProgrammaticOverride(t *testing.T) {
	for _, hide := range []bool{true, false} {
		t.Run(map[bool]string{true: "hide", false: "allow"}[hide], func(t *testing.T) {
			// Code wins over environment, and the constructor snapshots the pointer.
			t.Setenv("FI_HIDE_ERROR_DETAILS", map[bool]string{true: "false", false: "true"}[hide])
			tp, rec := serverRecorder(t)
			override := hide
			i := traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp), traceaia2a.WithTraceConfig(traceaia2a.TraceConfig{HideInputs: true, HideErrorDetails: &override}))
			override = !hide
			ctx, _, _ := i.Before(context.Background(), &a2aclient.Request{Method: "GetTask"})
			_ = i.After(ctx, &a2aclient.Response{Method: "GetTask", Err: a2a.NewError(a2a.ErrUnauthorized, marker)})
			if strings.Contains(everything(only(t, rec)), marker) == hide {
				t.Fatal("programmatic override did not win or changed after construction")
			}
		})
	}
}
