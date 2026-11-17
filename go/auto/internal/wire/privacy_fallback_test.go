package wire

import (
	"net/http"
	"strings"
	"testing"
)

func TestPrivacyBudgetRetainsWireResult(t *testing.T) {
	for _, side := range []string{"input", "output"} {
		t.Run(side, func(t *testing.T) {
			input, output := "hi", "ok"
			if side == "input" {
				input = strings.Repeat("p", 1600000)
			} else {
				output = strings.Repeat("r", 2200000)
			}
			const secret = "S6_BODY_CREDENTIAL_PRIVATE"
			req := Request{Method: "POST", URL: "http://127.0.0.1/v1/chat/completions", Header: http.Header{"Content-Type": {"application/json"}}, Body: []byte(`{"model":"m","client_secret":"` + secret + `","messages":[{"role":"user","content":"` + input + `"}]}`)}
			var observed bool
			req.ObserveSecret = func(value string) { observed = observed || value == secret }
			resp := Response{Status: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: []byte(`{"choices":[{"message":{"role":"assistant","content":"` + output + `"}}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}`)}
			if len(req.Body) >= DefaultMaxBodyBytes || len(resp.Body) >= DefaultMaxBodyBytes {
				t.Fatal("reproduction must remain under the body limit")
			}
			r := Build(Find(req.Method, req.URL), req, resp)
			if r == nil {
				t.Fatal("privacy-budget exhaustion discarded the request")
			}
			if !observed {
				t.Fatal("body credentials were not harvested before content degradation")
			}
			if !r.OK || r.Description != "" || r.Name != "traceai span" || r.Attributes["traceai.wire.privacy_degraded"] != true {
				t.Fatal("fallback lost outcome or degradation marker")
			}
			for key, want := range map[string]any{"gen_ai.span.kind": "LLM", "gen_ai.operation.name": "chat", "traceai.capture.layer": "wire", "http.response.status_code": 200, "gen_ai.usage.input_tokens": int64(7), "gen_ai.usage.output_tokens": int64(3), "gen_ai.usage.total_tokens": int64(10)} {
				if r.Attributes[key] != want {
					t.Errorf("fallback %s=%v; want %v", key, r.Attributes[key], want)
				}
			}
			data := dump(r.Attributes)
			if strings.Contains(data, secret) || strings.Contains(data, strings.Repeat("p", 128)) || strings.Contains(data, strings.Repeat("r", 128)) {
				t.Fatal("fallback retained unchecked content")
			}
		})
	}
}

func TestPrivacyResultFallbackBudgets(t *testing.T) {
	deep := any("PRIVATE_DEEP_CONTENT")
	for i := 0; i < privacyRules.Limits.Depth+1; i++ {
		deep = map[string]any{"body": deep}
	}
	cyclic := map[string]any{}
	cyclic["body"] = cyclic
	for _, tc := range []struct {
		name  string
		value any
	}{
		{"text", strings.Repeat("p", privacyRules.Limits.Text+1)},
		{"nodes", make([]any, privacyRules.Limits.Nodes+1)},
		{"depth", deep},
		{"cycle", cyclic},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := &Result{Name: "chat m", OK: false, Description: "PRIVATE_DESCRIPTION", Attributes: map[string]any{"payload": tc.value, "output.value": "PRIVATE_OUTPUT", "gen_ai.span.kind": "LLM", "http.response.status_code": 503, "gen_ai.usage.input_tokens": int64(0), "traceai.wire.incomplete": true}}
			scrubResult(r, Request{})
			if r.OK || r.Name != "traceai span" || r.Description != "" || r.Attributes["traceai.wire.privacy_degraded"] != true {
				t.Fatal("fallback lost outcome or exposed description")
			}
			if r.Attributes["http.response.status_code"] != 503 || r.Attributes["gen_ai.usage.input_tokens"] != int64(0) || r.Attributes["traceai.wire.incomplete"] != true {
				t.Fatal("fallback lost generated metadata, known zero usage or degradation")
			}
			if _, ok := r.Attributes["payload"]; ok {
				t.Fatal("unchecked input retained")
			}
			if _, ok := r.Attributes["output.value"]; ok {
				t.Fatal("unchecked output retained")
			}
		})
	}
}

func TestPrivacyFallbackIdentityAlsoFailsClosed(t *testing.T) {
	r := &Result{Name: "chat m", OK: true, Attributes: map[string]any{"input.value": strings.Repeat("p", privacyRules.Limits.Text+1), "server.address": strings.Repeat("q", privacyRules.Limits.Text+1)}}
	scrubResult(r, Request{})
	if len(r.Attributes) != 2 || r.Attributes["traceai.capture.layer"] != "wire" || r.Attributes["traceai.wire.privacy_degraded"] != true || !r.OK {
		t.Fatal("even fallback identity must be bounded and fail closed without losing outcome")
	}
}
