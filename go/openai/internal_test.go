package traceaiopenai

import (
	"os"
	"reflect"
	"strings"
	"testing"
)

func collect(chunks ...string) []string {
	var p sseParser
	var got []string
	on := func(b []byte) { got = append(got, string(b)) }
	for _, c := range chunks {
		p.feed([]byte(c), on)
	}
	p.flush(on)
	return got
}

func TestSSEParserSplitsAcrossReads(t *testing.T) {
	got := collect("da", "ta: {\"a\":1}\n", "\ndata: [DO", "NE]\n\n")
	if !reflect.DeepEqual(got, []string{`{"a":1}`, "[DONE]"}) {
		t.Fatalf("got %q", got)
	}
}

func TestSSEParserCRLFCommentsMultiLineAndTail(t *testing.T) {
	got := collect(": keep-alive\r\n\r\nevent: x\r\ndata: one\r\ndata: two\r\n\r\ndata:tail")
	if !reflect.DeepEqual(got, []string{"one\ntwo", "tail"}) {
		t.Fatalf("got %q", got)
	}
}

func TestParseAPIErrorShapes(t *testing.T) {
	for in, want := range map[string]apiError{
		`{"error":{"message":"m","type":"t","code":"c"}}`: {"m", "t", "c"},
		`{"error":{"message":"m","type":"t","code":429}}`: {"m", "t", "429"},
		`{"error":"plain"}`:            {Message: "plain"},
		`{"message":"top","type":"x"}`: {"top", "x", ""},
		`not json`:                     {},
	} {
		if got := parseAPIError([]byte(in)); got != want {
			t.Errorf("%s -> %+v, want %+v", in, got, want)
		}
	}
}

func TestSafeToken(t *testing.T) {
	for s, want := range map[string]bool{
		"invalid_request_error": true, "model_not_found": true, "429": true,
		"": false, "has space": false, "a/b": false, strings.Repeat("a", 65): false,
	} {
		if safeToken(s) != want {
			t.Errorf("safeToken(%q) != %v", s, want)
		}
	}
}

// AC-01 and AC-02: the module targets openai-go v3 and depends on no Arize
// module.
func TestGoModTargetsV3AndNoArize(t *testing.T) {
	b, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatal(err)
	}
	mod := string(b)
	if !strings.Contains(mod, "github.com/openai/openai-go/v3 v3.") {
		t.Error("go.mod must require github.com/openai/openai-go/v3")
	}
	if strings.Contains(mod, "github.com/openai/openai-go v") || strings.Contains(strings.ToLower(mod), "arize") || strings.Contains(mod, "openinference") {
		t.Error("go.mod must not require openai-go v1 or an Arize/OpenInference module")
	}
	if strings.Contains(mod, "\nreplace ") || strings.HasPrefix(mod, "replace ") {
		t.Error("library go.mod must not contain replace directives")
	}
}

// AC-11: the example flushes on exit.
func TestExampleDefersShutdown(t *testing.T) {
	b, err := os.ReadFile("example/main.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), "defer shutdown(") {
		t.Error("example must defer the TracerProvider shutdown")
	}
}
