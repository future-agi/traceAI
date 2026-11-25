package auto

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// uncheckableTexts are configuration and attribute texts the privacy filter
// cannot finish.
func uncheckableTexts() []string {
	return []string{
		strings.Repeat("[", 200) + strings.Repeat("]", 200),
		strings.Repeat(`{"a":`, 200) + `"api_key=sk-live-0123456789abcdef"` + strings.Repeat("}", 200),
		strings.Repeat("x", 9<<20),
	}
}

// equal compares two values by their printed form; fmt prints map keys in
// sorted order.
func equal(a, b any) bool { return fmt.Sprintf("%#v", a) == fmt.Sprintf("%#v", b) }

// within runs call on its own goroutine and reports whether it returned
// normally within the timeout.
func within(t *testing.T, what string, call func()) {
	t.Helper()
	done := make(chan any, 1)
	go func() {
		defer func() { done <- recover() }()
		call()
	}()
	select {
	case v := <-done:
		if v != nil {
			t.Fatalf("%s did not return normally: %.80v", what, v)
		}
	case <-time.After(30 * time.Second):
		t.Fatalf("%s did not return", what)
	}
}

func TestShutdownReturnsWithAnUncheckableProjectName(t *testing.T) {
	for i, text := range uncheckableTexts() {
		t.Run(fmt.Sprintf("text%d", i), func(t *testing.T) {
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder()), WithProject(text, "observe")); err == nil {
				t.Error("Init accepted a project name the privacy filter cannot check")
			}
			within(t, "Shutdown", func() { _ = Shutdown(context.Background()) })
			var r AgentReport
			within(t, "Report", func() { r = Report() })
			if r.Config.ProjectName != wire.CredentialRedacted || r.Config.ProjectType != "observe" {
				t.Errorf("report = %.80q %q", r.Config.ProjectName, r.Config.ProjectType)
			}
			within(t, "a second Shutdown", func() { _ = Shutdown(context.Background()) })
			within(t, "ForceFlush", func() { _ = ForceFlush(context.Background()) })
		})
	}
}

func TestShutdownReportKeepsEveryCheckableValue(t *testing.T) {
	ordinary := Config{APIKey: "sk-test-0123456789abcdef", SecretKey: "secret-0123456789", BaseURL: "https://user:pw@collector.example/v1", ProjectName: "chat api_key=sk-test-0123456789abcdef", ProjectType: "observe", ProjectVersionName: "v1", ProjectVersionID: "id", ServiceName: "svc", ConfigFile: "/etc/traceai.yaml", Instrumentations: []string{"openai", "anthropic"}, DisabledInstrumentations: []string{"genai"}}
	if got, want := shutdownReport(ordinary), makeReport(ordinary, false, "disabled"); !equal(got, want) {
		t.Fatalf("got %+v, want %+v", got.Config, want.Config)
	}
	fields := map[string]func(*Config) *string{
		"ProjectType":              func(c *Config) *string { return &c.ProjectType },
		"BaseURL":                  func(c *Config) *string { return &c.BaseURL },
		"ProjectName":              func(c *Config) *string { return &c.ProjectName },
		"ProjectVersionName":       func(c *Config) *string { return &c.ProjectVersionName },
		"ProjectVersionID":         func(c *Config) *string { return &c.ProjectVersionID },
		"ServiceName":              func(c *Config) *string { return &c.ServiceName },
		"ConfigFile":               func(c *Config) *string { return &c.ConfigFile },
		"Instrumentations":         func(c *Config) *string { return &c.Instrumentations[1] },
		"DisabledInstrumentations": func(c *Config) *string { return &c.DisabledInstrumentations[0] },
	}
	text := uncheckableTexts()[0]
	for name, field := range fields {
		t.Run(name, func(t *testing.T) {
			c := ordinary
			c.Instrumentations = append([]string(nil), ordinary.Instrumentations...)
			c.DisabledInstrumentations = append([]string(nil), ordinary.DisabledInstrumentations...)
			*field(&c) = text
			var got AgentReport
			within(t, "shutdownReport", func() { got = shutdownReport(c) })
			placeholder := c
			placeholder.Instrumentations = append([]string(nil), c.Instrumentations...)
			placeholder.DisabledInstrumentations = append([]string(nil), c.DisabledInstrumentations...)
			*field(&placeholder) = wire.CredentialRedacted
			want := makeReport(placeholder, false, "disabled")
			if !equal(got, want) {
				t.Errorf("got %+v, want %+v", got.Config, want.Config)
			}
			if *field(&c) != text {
				t.Error("the caller's configuration changed")
			}
		})
	}
}

func TestExportedSpanKeepsItsFallback(t *testing.T) {
	cases := map[string]func(text string) tracetest.SpanStub{
		"name": func(text string) tracetest.SpanStub {
			return tracetest.SpanStub{Name: text, Status: sdktrace.Status{Code: codes.Error, Description: "kept status"}, Attributes: []attribute.KeyValue{attribute.String("output.value", "kept text")}, Events: []sdktrace.Event{{Name: "exception"}}}
		},
		"status": func(text string) tracetest.SpanStub {
			return tracetest.SpanStub{Name: "chat", Status: sdktrace.Status{Code: codes.Error, Description: text}, Attributes: []attribute.KeyValue{attribute.String("output.value", "kept text")}, Events: []sdktrace.Event{{Name: "exception"}}}
		},
		"event name": func(text string) tracetest.SpanStub {
			return tracetest.SpanStub{Name: "chat", Status: sdktrace.Status{Code: codes.Error, Description: "kept status"}, Attributes: []attribute.KeyValue{attribute.String("output.value", "kept text")}, Events: []sdktrace.Event{{Name: text}}}
		},
		"attribute": func(text string) tracetest.SpanStub {
			return tracetest.SpanStub{Name: "chat", Status: sdktrace.Status{Code: codes.Error, Description: "kept status"}, Attributes: []attribute.KeyValue{attribute.String("input.value", text), attribute.String("output.value", "kept text")}, Events: []sdktrace.Event{{Name: "exception"}}}
		},
	}
	for name, build := range cases {
		for i, text := range uncheckableTexts() {
			t.Run(fmt.Sprintf("%s/text%d", name, i), func(t *testing.T) {
				var span sdktrace.ReadOnlySpan
				within(t, "exportSpan", func() { span = exportSpan(build(text).Snapshot(), Config{}, nil) })
				if span.Name() == "chat" || span.Status().Description != "" || len(span.Attributes()) != 0 || len(span.Events()) != 0 {
					t.Errorf("unchecked content was exported: %.80q %.80q %d attributes %d events", span.Name(), span.Status().Description, len(span.Attributes()), len(span.Events()))
				}
			})
		}
	}
}

// grownName passes the privacy filter once, but its masked form exceeds what
// the filter can check.
func grownName() string {
	return `{"pad":"` + strings.Repeat("x", 2097131) + `","k0":{"api_key":1}}`
}

func TestReportReturnsWhenTheMaskedNameGrows(t *testing.T) {
	if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder()), WithProject(grownName(), "observe")); err != nil {
		t.Fatal(err)
	}
	var r AgentReport
	within(t, "Report", func() { r = Report() })
	if r.Config.ProjectName != wire.CredentialRedacted || r.Config.ProjectType != "observe" {
		t.Errorf("report = %.80q %q", r.Config.ProjectName, r.Config.ProjectType)
	}
	within(t, "Shutdown", func() { _ = Shutdown(context.Background()) })
	within(t, "Report after Shutdown", func() { r = Report() })
	if r.Config.ProjectName != wire.CredentialRedacted {
		t.Errorf("report = %.80q", r.Config.ProjectName)
	}
	ordinary := Config{APIKey: "sk-test-0123456789abcdef", ProjectName: "chat api_key=sk-test-0123456789abcdef", ProjectType: "observe", Instrumentations: []string{"openai"}}
	if got, want := reportedConfig(ordinary), maskReportConfig(ordinary); !equal(got, want) {
		t.Errorf("got %+v, want %+v", got, want)
	}
}

func returns(call func()) (ok bool) {
	defer func() {
		if recover() != nil {
			ok = false
		}
	}()
	call()
	return true
}

func TestCaptureFormattingLeavesTheFallbackToItsCaller(t *testing.T) {
	for i, text := range uncheckableTexts() {
		t.Run(fmt.Sprintf("text%d", i), func(t *testing.T) {
			if returns(func() { safeString(text, Config{}) }) {
				t.Error("safeString returned a value for a text it cannot check")
			}
			if returns(func() { captureString(text, Config{}) }) {
				t.Error("captureString returned a value for a text it cannot check")
			}
			if returns(func() { attributes(map[string]any{"input.value": text}, Config{}) }) {
				t.Error("attributes returned values for a text it cannot check")
			}
		})
	}
}
