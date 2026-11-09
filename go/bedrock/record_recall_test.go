package traceaibedrock

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"go.opentelemetry.io/otel/attribute"
)

func TestBoundedUTF8(t *testing.T) {
	for _, tc := range []struct {
		name, input, want string
		limit             int
	}{
		{"valid", "hello", "hello", 10},
		{"invalid-under-limit", "a\xffb", "a�b", 10},
		{"invalid-before-suffix", "a\xffbcdef", "a�b", 5},
		{"replacement-exceeds-limit", "a\xff", "a", 2},
		{"split-rune", "a€z", "a", 3},
		{"whole-rune", "a€z", "a€", 4},
		{"zero", "\xff", "", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := bounded(tc.input, tc.limit)
			if got != tc.want || len(got) > tc.limit || !utf8.ValidString(got) {
				t.Fatalf("bounded = %q (%d bytes), want %q within %d", got, len(got), tc.want, tc.limit)
			}
		})
	}
}

func TestIndexedToolResultsPreserveSurroundingMessages(t *testing.T) {
	p, e := provider(t)
	_, r := NewClient(nil, WithTracerProvider(p)).start(context.Background(), "test", nil)
	r.messages("input", []message{
		{Role: "user", Content: []block{
			{Type: "text", Text: "before"},
			{Type: "tool_result", ID: "call-a", Text: "A"},
			{Type: "tool_result", ID: "call-b"},
			{Type: "text", Text: "after"},
		}},
		{Role: "assistant", Content: []block{{Type: "text", Text: "next"}}},
	})
	r.finish(nil, false)
	a := attrs(llms(e)[0])
	for i, want := range []struct{ role, text, id string }{
		{"user", "before", ""}, {"tool", "A", "call-a"},
		{"tool", "", "call-b"}, {"user", "after", ""}, {"assistant", "next", ""},
	} {
		prefix := fmt.Sprintf("gen_ai.input.messages.%d.message.", i)
		if a[prefix+"role"].AsString() != want.role || a[prefix+"content"].AsString() != want.text || a[prefix+"tool_call_id"].AsString() != want.id {
			t.Fatalf("message %d changed: %v", i, a)
		}
	}
}

func TestStopSequencesPrivacyAndBounds(t *testing.T) {
	for name, privacy := range map[string]TraceConfig{
		"visible": {}, "inputs": {HideInputs: true}, "messages": {HideInputMessages: true},
		"text": {HideInputText: true}, "parameters": {HideLLMInvocationParameters: true},
	} {
		t.Run(name, func(t *testing.T) {
			p, e := provider(t)
			_, r := NewClient(nil, WithTracerProvider(p), WithTraceConfig(privacy)).start(context.Background(), "test", nil)
			stops := make([]string, 17)
			for i := range stops {
				stops[i] = strings.Repeat("x\xff", maxCapture)
			}
			r.converseInput(nil, nil, &types.InferenceConfiguration{StopSequences: stops}, nil)
			r.finish(nil, false)
			a := attrs(llms(e)[0])
			v := a["gen_ai.request.stop_sequences"]
			if name != "visible" {
				if v.Type() != attribute.INVALID {
					t.Fatal("stop sequences leaked")
				}
				return
			}
			if v.Type() != attribute.STRINGSLICE || len(v.AsStringSlice()) != 16 || !a["traceai.capture.truncated"].AsBool() {
				t.Fatal("stop sequence bounds missing")
			}
			total := 0
			for _, value := range v.AsStringSlice() {
				total += len(value)
				if !utf8.ValidString(value) {
					t.Fatal("invalid UTF-8 stop sequence")
				}
			}
			if total > maxCapture {
				t.Fatalf("captured %d bytes", total)
			}
			for key := range a {
				if strings.HasPrefix(key, "gen_ai.request.stop_sequences.") {
					t.Fatal("noncanonical key retained")
				}
			}
		})
	}
}
