package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/genai"
)

const qRequest = "synthetic-private-query"
const qAnswer = "synthetic-private-answer"
const qImage = "c3ludGhldGljLWltYWdlLWJ5dGVz"
const qBody = `{"candidates":[{"index":0,"content":{"role":"model","parts":[{"text":"synthetic-private-answer"}]},"finishReason":"STOP"}],"modelVersion":"gemini-test-version","responseId":"synthetic-response","usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":3,"thoughtsTokenCount":2,"totalTokenCount":15,"cachedContentTokenCount":4}}`
const qToolBody = `{"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"id":"call-1","name":"weather","args":{"city":"Paris"}}}]},"finishReason":"STOP"}],"modelVersion":"gemini-test-version","usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":3,"thoughtsTokenCount":2,"totalTokenCount":15,"cachedContentTokenCount":4}}`

func qSetup(t *testing.T, handler http.HandlerFunc, instrument bool) (*genai.Client, *sdktrace.TracerProvider, *tracetest.InMemoryExporter) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	hc := srv.Client()
	if instrument {
		hc = traceaigenai.WrapClient(hc, traceaigenai.WithTracerProvider(tp))
	}
	c, err := genai.NewClient(context.Background(), &genai.ClientConfig{APIKey: "synthetic-never-export-key-123", Backend: genai.BackendGeminiAPI, HTTPClient: hc, HTTPOptions: genai.HTTPOptions{BaseURL: srv.URL + "/"}})
	if err != nil {
		t.Fatal(err)
	}
	return c, tp, exp
}

func qAttrs(s tracetest.SpanStub) map[string]string {
	a := map[string]string{}
	for _, kv := range s.Attributes {
		a[string(kv.Key)] = kv.Value.Emit()
	}
	return a
}
func qAll(s tracetest.SpanStub) string { b, _ := json.Marshal(s); return string(b) }
func qOne(t *testing.T, exp *tracetest.InMemoryExporter) tracetest.SpanStub {
	t.Helper()
	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("exported spans=%d want=1", len(spans))
	}
	return spans[0]
}
func qReply(body string, stream bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if stream {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprintf(w, "data: %s\n\n", body)
		} else {
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, body)
		}
	}
}
func qClearEnv(t *testing.T) {
	for _, key := range []string{"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_ERROR_DETAILS", "FI_HIDE_LLM_INVOCATION_PARAMETERS"} {
		t.Setenv(key, "")
	}
}

func TestQualificationDefaultSmoke(t *testing.T) {
	for _, mode := range []string{"generate", "stream", "embed", "tools", "http-error"} {
		t.Run(mode, func(t *testing.T) {
			qClearEnv(t)
			c, tp, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
				b, _ := io.ReadAll(r.Body)
				if !strings.Contains(string(b), qRequest) {
					t.Error("wire input changed")
				}
				if r.Header.Get("x-goog-api-key") != "synthetic-never-export-key-123" {
					t.Error("SDK key missing")
				}
				switch mode {
				case "embed":
					qReply(`{"embeddings":[{"values":[0.123456,0.876544]}]}`, false)(w, r)
				case "http-error":
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(400)
					io.WriteString(w, `{"error":{"code":400,"status":"INVALID_ARGUMENT","message":"synthetic-never-export-key-123 invalid"}}`)
				case "tools":
					qReply(qToolBody, false)(w, r)
				default:
					qReply(qBody, mode == "stream")(w, r)
				}
			}, true)
			ctx, parent := tp.Tracer("qualification").Start(context.Background(), "agent")
			var err error
			if mode == "stream" {
				count := 0
				for v, e := range c.Models.GenerateContentStream(ctx, "gemini-test", genai.Text(qRequest), nil) {
					if e != nil {
						err = e
						break
					}
					if !strings.Contains(v.Text(), qAnswer) {
						t.Error("stream response changed")
					}
					count++
				}
				if count != 1 {
					t.Errorf("chunks=%d want=1", count)
				}
			} else if mode == "embed" {
				v, e := c.Models.EmbedContent(ctx, "gemini-test", genai.Text(qRequest), nil)
				err = e
				if e == nil && (len(v.Embeddings) != 1 || len(v.Embeddings[0].Values) != 2) {
					t.Error("embedding response changed")
				}
			} else {
				var cfg *genai.GenerateContentConfig
				if mode == "tools" {
					cfg = &genai.GenerateContentConfig{Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{{Name: "weather", Description: "weather lookup"}}}}}
				}
				_, err = c.Models.GenerateContent(ctx, "gemini-test", genai.Text(qRequest), cfg)
			}
			if (mode == "http-error") != (err != nil) {
				t.Fatalf("mode %s error %v", mode, err)
			}
			s := qOne(t, exp)
			a := qAttrs(s)
			if s.Parent.SpanID() != parent.SpanContext().SpanID() || s.SpanContext.TraceID() != parent.SpanContext().TraceID() || s.SpanKind != trace.SpanKindClient {
				t.Errorf("incorrect span tree/kind: %+v", s)
			}
			if a["gen_ai.provider.name"] != "google" || a["gen_ai.request.model"] != "gemini-test" {
				t.Errorf("model/provider %v", a)
			}
			if !strings.Contains(a["input.value"], qRequest) {
				t.Errorf("default privacy missing prompt: %v", a)
			}
			if s.EndTime.IsZero() {
				t.Error("span not ended")
			}
			switch mode {
			case "embed":
				if a["gen_ai.span.kind"] != "EMBEDDING" || a["embedding.count"] != "1" || a["gen_ai.embeddings.dimension.count"] != "2" {
					t.Errorf("embedding metadata %v", a)
				}
				if strings.Contains(qAll(s), "0.123456") {
					t.Error("embedding vector leaked")
				}
				if a["gen_ai.usage.input_tokens"] != "" {
					t.Error("invented embedding tokens")
				}
			case "http-error":
				if s.Status.Code != codes.Error || a["error.type"] != "INVALID_ARGUMENT" {
					t.Errorf("missing error %v %v", s.Status, a)
				}
			default:
				for key, want := range map[string]string{"gen_ai.span.kind": "LLM", "gen_ai.usage.input_tokens": "10", "gen_ai.usage.output_tokens": "5", "gen_ai.usage.total_tokens": "15", "gen_ai.response.model": "gemini-test-version"} {
					if a[key] != want {
						t.Errorf("%s=%q want %q", key, a[key], want)
					}
				}
				if mode == "tools" {
					if !strings.Contains(a["gen_ai.tool.definitions.0.tool.json_schema"], "weather") || !strings.Contains(a["output.value"], "Paris") {
						t.Errorf("missing tools %v", a)
					}
				} else if a["output.value"] != qAnswer {
					t.Errorf("output=%s", a["output.value"])
				}
			}
			if strings.Contains(qAll(s), "synthetic-never-export-key-123") {
				t.Error("credential leaked")
			}
			parent.End()
			if len(exp.GetSpans()) != 2 {
				t.Errorf("duplicate or missing parent span: %d", len(exp.GetSpans()))
			}
		})
	}
}

func TestQualificationNoInstrumentationControl(t *testing.T) {
	qClearEnv(t)
	c, _, exp := qSetup(t, qReply(qBody, false), false)
	if _, err := c.Models.GenerateContent(context.Background(), "gemini-test", genai.Text(qRequest), nil); err != nil {
		t.Fatal(err)
	}
	if len(exp.GetSpans()) != 0 {
		t.Error("upstream emitted unexpected native spans")
	}
}

func TestQualificationHideTextEveryAttribute(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, flag := range []string{"FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT"} {
			t.Run(fmt.Sprintf("stream=%t/%s", stream, flag), func(t *testing.T) {
				qClearEnv(t)
				t.Setenv(flag, "true")
				c, _, exp := qSetup(t, qReply(qBody, stream), true)
				if stream {
					for _, err := range c.Models.GenerateContentStream(context.Background(), "gemini-test", genai.Text(qRequest), nil) {
						if err != nil {
							t.Fatal(err)
						}
					}
				} else {
					if _, err := c.Models.GenerateContent(context.Background(), "gemini-test", genai.Text(qRequest), nil); err != nil {
						t.Fatal(err)
					}
				}
				s := qOne(t, exp)
				secret := qRequest
				if flag == "FI_HIDE_OUTPUT_TEXT" {
					secret = qAnswer
				}
				if strings.Contains(qAll(s), secret) {
					t.Errorf("%s leaked %q despite %s=true; aggregate input=%s output=%s", flag, secret, flag, qAttrs(s)["input.value"], qAttrs(s)["output.value"])
				}
			})
		}
	}
}

func TestQualificationHideInputImageEveryAttribute(t *testing.T) {
	qClearEnv(t)
	t.Setenv("FI_HIDE_INPUT_IMAGES", "true")
	c, _, exp := qSetup(t, qReply(qBody, false), true)
	contents := []*genai.Content{{Role: "user", Parts: []*genai.Part{{Text: qRequest}, {InlineData: &genai.Blob{MIMEType: "image/png", Data: []byte("synthetic-image-bytes")}}}}}
	if _, err := c.Models.GenerateContent(context.Background(), "gemini-test", contents, nil); err != nil {
		t.Fatal(err)
	}
	s := qOne(t, exp)
	if strings.Contains(qAll(s), qImage) {
		t.Errorf("FI_HIDE_INPUT_IMAGES=true leaked inline image in input.value=%s", qAttrs(s)["input.value"])
	}
}

func TestQualificationGenerationDecodeError(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, body := range []string{`synthetic-private-not-json`, `{"candidates":[{"content":{"parts":[{"text":123}]}}]}`} {
			t.Run(fmt.Sprintf("stream=%t/body=%s", stream, body), func(t *testing.T) {
				qClearEnv(t)
				c, _, exp := qSetup(t, qReply(body, stream), true)
				var err error
				if stream {
					for _, e := range c.Models.GenerateContentStream(context.Background(), "gemini-test", genai.Text(qRequest), nil) {
						if e != nil {
							err = e
						}
					}
				} else {
					_, err = c.Models.GenerateContent(context.Background(), "gemini-test", genai.Text(qRequest), nil)
				}
				if err == nil {
					t.Fatal("oracle: SDK did not reject invalid response")
				}
				s := qOne(t, exp)
				if s.Status.Code != codes.Error || qAttrs(s)["error.type"] == "" {
					t.Errorf("real SDK error %v exported as status=%v attributes=%v", err, s.Status, qAttrs(s))
				}
				if strings.Contains(qAll(s), "synthetic-private-not-json") {
					t.Error("decoder detail leaked")
				}
			})
		}
	}
}

func TestQualificationLongConversationKeepsUsage(t *testing.T) {
	qClearEnv(t)
	c, _, exp := qSetup(t, qReply(qBody, false), true)
	contents := make([]*genai.Content, 80)
	for i := range contents {
		role := "user"
		if i%2 == 1 {
			role = "model"
		}
		contents[i] = &genai.Content{Role: role, Parts: []*genai.Part{{Text: fmt.Sprintf("turn %d", i)}}}
	}
	if _, err := c.Models.GenerateContent(context.Background(), "gemini-test", contents, nil); err != nil {
		t.Fatal(err)
	}
	s := qOne(t, exp)
	a := qAttrs(s)
	if a["gen_ai.usage.input_tokens"] != "10" || a["gen_ai.usage.output_tokens"] != "5" || a["output.value"] != qAnswer {
		t.Errorf("80-turn call dropped %d attributes including usage/output; usage=%q/%q output=%q", s.DroppedAttributes, a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["output.value"])
	}
}
