package traceaia2a

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"go.opentelemetry.io/otel/attribute"
)

func TestSanitizeURL(t *testing.T) {
	for in, want := range map[string]string{
		"":                                     "",
		"https://agent.example/a2a":            "https://agent.example/a2a",
		"https://bob:s3cret@agent.example/a2a": "https://agent.example/a2a",
		"https://agent.example/a2a?api_key=s3cret": "https://agent.example/a2a",
		"https://agent.example/a2a#s3cret":         "https://agent.example/a2a",
		"http://127.0.0.1:9001":                    "http://127.0.0.1:9001",
		"bob:s3cret@agent.example:50051":           "agent.example:50051",
		"agent.example:50051?token=s3cret":         "agent.example:50051",
		"https://[::1]:8443/x?y=s3cret":            "https://[::1]:8443/x",
		"://bad":                                   "",
	} {
		if got := sanitizeURL(in); got != want {
			t.Errorf("sanitizeURL(%q) = %q, want %q", in, got, want)
		}
		if strings.Contains(sanitizeURL(in), "s3cret") {
			t.Errorf("sanitizeURL(%q) kept a secret", in)
		}
	}
}

func TestPartsTextAndKind(t *testing.T) {
	parts := a2a.ContentParts{
		a2a.NewTextPart("hello"),
		a2a.NewDataPart(map[string]any{"city": "Paris"}),
		a2a.NewRawPart([]byte("binary-secret")),
		a2a.NewFileURLPart("https://files.example/x.pdf?sig=s3cret", "application/pdf"),
		nil,
	}
	got := partsText(parts)
	if got != "hello\n{\"city\":\"Paris\"}" {
		t.Errorf("partsText = %q", got)
	}
	if strings.Contains(got, "binary-secret") || strings.Contains(got, "s3cret") {
		t.Error("file parts must not be copied")
	}
	for want, ps := range map[string]a2a.ContentParts{
		"text": {a2a.NewTextPart("x")},
		"data": {a2a.NewDataPart(1)},
		"file": {a2a.NewRawPart([]byte{1})},
		"":     {},
	} {
		if got := partsKind(ps); got != want {
			t.Errorf("partsKind = %q, want %q", got, want)
		}
	}
	if partsKind(a2a.ContentParts{a2a.NewFileURLPart("https://x", "image/png")}) != "file" {
		t.Error("URL part is a file")
	}
}

func TestTruncateKeepsRunesWhole(t *testing.T) {
	s := strings.Repeat("é", maxContentLen) // 2 bytes each
	got := truncate(s)
	if len(got) > maxContentLen || !strings.HasSuffix(got, truncatedSuffix) || !utf8.ValidString(got) {
		t.Errorf("len %d valid %v", len(got), utf8.ValidString(got))
	}
	if truncate("short") != "short" {
		t.Error("short strings are kept")
	}
}

func TestClassifyError(t *testing.T) {
	cases := []struct {
		err      error
		typ, msg string
	}{
		{context.Canceled, "canceled", ""},
		{fmt.Errorf("failed to send HTTP request: %w", context.DeadlineExceeded), "timeout", ""},
		{a2a.NewError(a2a.ErrTaskNotFound, "task t-1 is gone"), "TASK_NOT_FOUND", "task t-1 is gone"},
		{fmt.Errorf("wrapped: %w", a2a.ErrExtendedCardNotConfigured), "EXTENDED_AGENT_CARD_NOT_CONFIGURED", ""},
		{errors.New("dial tcp 10.0.0.1:443: connect: connection refused"), "transport", ""},
		{nil, "", ""},
	}
	for _, c := range cases {
		typ, msg := classifyError(c.err)
		if typ != c.typ || msg != c.msg {
			t.Errorf("classifyError(%v) = %q, %q; want %q, %q", c.err, typ, msg, c.typ, c.msg)
		}
	}
}

func clearHideEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{EnvHideLLMInvocationParameters, EnvHideInputs, EnvHideOutputs, EnvHideInputMessages,
		EnvHideOutputMessages, EnvHideInputImages, EnvHideInputText, EnvHideOutputText, EnvBase64ImageMaxLength} {
		t.Setenv(k, "")
	}
}

func TestTraceConfigDefaultCapturesEverything(t *testing.T) {
	clearHideEnv(t)
	c := TraceConfigFromEnv()
	if c != (TraceConfig{}) {
		t.Fatalf("default must hide nothing, like DEFAULT_HIDE_* = False in Python: %+v", c)
	}
	for _, kv := range []attribute.KeyValue{
		attribute.String(InputValue, "in"), attribute.String(OutputValue, "out"),
		attribute.String(ErrorMessage, "err"), attribute.String(A2ATaskID, "t"),
	} {
		if got, keep := c.Mask(kv); !keep || got != kv {
			t.Errorf("default config changed %s", kv.Key)
		}
	}
}

func TestTraceConfigFromEnvParsesLikePython(t *testing.T) {
	clearHideEnv(t)
	t.Setenv(EnvHideInputs, "TRUE")
	t.Setenv(EnvHideOutputs, "true")
	t.Setenv(EnvHideInputMessages, "1") // not "true": Python falls back to the default
	t.Setenv(EnvBase64ImageMaxLength, "100")
	c := TraceConfigFromEnv()
	if !c.HideInputs || !c.HideOutputs || c.HideInputMessages || c.Base64ImageMaxLength != 100 {
		t.Fatalf("parsed %+v", c)
	}
}

func TestTraceConfigMaskForA2AKeys(t *testing.T) {
	in := attribute.String(InputValue, "SECRET")
	out := attribute.String(OutputValue, "SECRET")
	em := attribute.String(ErrorMessage, "SECRET")
	task := attribute.String(A2ATaskState, "TASK_STATE_COMPLETED")
	c := TraceConfig{HideInputs: true}
	if got, keep := c.Mask(in); !keep || got.Value.AsString() != RedactedValue {
		t.Error("HideInputs redacts input.value")
	}
	if got, keep := c.Mask(out); !keep || got != out {
		t.Error("HideInputs keeps output.value")
	}
	if _, keep := c.Mask(em); keep {
		t.Error("HideInputs drops error.message")
	}
	if _, keep := c.Mask(attribute.String(InputMimeType, MimeTypeText)); keep {
		t.Error("HideInputs drops input.mime_type")
	}
	c = TraceConfig{HideOutputs: true}
	if got, keep := c.Mask(out); !keep || got.Value.AsString() != RedactedValue {
		t.Error("HideOutputs redacts output.value")
	}
	if _, keep := c.Mask(em); keep {
		t.Error("HideOutputs drops error.message")
	}
	for _, c := range []TraceConfig{{HideInputs: true, HideOutputs: true}, {HideInputText: true, HideOutputText: true}} {
		if got, keep := c.Mask(task); !keep || got != task {
			t.Errorf("%+v must keep task facts", c)
		}
	}
	if (TraceConfig{HideInputs: true}).errorMessageAllowed() || !(TraceConfig{}).errorMessageAllowed() {
		t.Error("errorMessageAllowed")
	}
}
