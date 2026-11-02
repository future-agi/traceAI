package traceaigenai

import (
	"net/http"
	"net/http/cookiejar"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace/noop"
)

func TestRewrapInheritsAndOverridesWithoutMutation(t *testing.T) {
	clearHideEnv(t)
	t.Setenv(EnvHideInputText, "true")
	base := &http.Transport{}
	tp := noop.NewTracerProvider()
	first := NewTransport(base, WithTracerProvider(tp))
	t.Setenv(EnvHideInputText, "false")
	second := NewTransport(first)
	if second.base != base || second.cfg.tp != tp || !second.cfg.tc.HideInputText {
		t.Fatal("rewrapping must inherit resolved settings and unwrap the original transport")
	}
	if first.cfg == second.cfg {
		t.Fatal("rewrapping must not share mutable configuration")
	}
	third := NewTransport(second, WithTraceConfig(TraceConfig{HideOutputs: true}), WithTracerProvider(nil))
	if third.base != base || third.cfg.tp != nil || !third.cfg.tc.HideOutputs || third.cfg.tc.HideInputText {
		t.Fatal("explicit options must replace inherited settings")
	}
	if first.cfg.tp != tp || !first.cfg.tc.HideInputText || first.cfg.tc.HideOutputs || !second.cfg.tc.HideInputText {
		t.Fatal("wrapping changed an earlier transport")
	}
}

func TestRewrapPreservesHTTPClientOptions(t *testing.T) {
	clearHideEnv(t)
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	base := &http.Transport{}
	redirectCalled := false
	hc := &http.Client{
		Transport: NewTransport(base), Timeout: 5 * time.Second, Jar: jar,
		CheckRedirect: func(*http.Request, []*http.Request) error { redirectCalled = true; return http.ErrUseLastResponse },
	}
	wrapped := WrapClient(hc)
	if wrapped == hc || wrapped.Transport == hc.Transport || wrapped.Timeout != hc.Timeout || wrapped.Jar != jar {
		t.Fatal("wrapping must copy the client without losing HTTP options")
	}
	if wrapped.CheckRedirect(nil, nil) != http.ErrUseLastResponse || !redirectCalled {
		t.Error("redirect policy lost")
	}
	if wrapped.Transport.(*Transport).base != base {
		t.Error("transport stacked")
	}
}

func TestTransportSnapshotsErrorDetailsOverride(t *testing.T) {
	clearHideEnv(t)
	hide := true
	first := NewTransport(nil, WithTraceConfig(TraceConfig{HideErrorDetails: &hide}))
	hide = false
	if first.cfg.tc.errorMessageAllowed() {
		t.Fatal("transport retained caller's mutable bool pointer")
	}
	second := NewTransport(first)
	if second.cfg.tc.HideErrorDetails == first.cfg.tc.HideErrorDetails || second.cfg.tc.errorMessageAllowed() {
		t.Fatal("rewrapped transport must snapshot the inherited override")
	}
}
