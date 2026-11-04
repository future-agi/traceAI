package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/codes"
	"google.golang.org/genai"
)

func qGenerate(c *genai.Client, ctx context.Context, stream bool, contents []*genai.Content, cfg *genai.GenerateContentConfig) error {
	if !stream {
		_, err := c.Models.GenerateContent(ctx, "gemini-test", contents, cfg)
		return err
	}
	for _, err := range c.Models.GenerateContentStream(ctx, "gemini-test", contents, cfg) {
		if err != nil {
			return err
		}
	}
	return nil
}

func TestQualificationMixedContentPrivacy(t *testing.T) {
	const imageURI = "https://example.invalid/synthetic-private-image.png"
	const response = `{"candidates":[{"content":{"role":"model","parts":[{"text":"synthetic-private-answer"},{"functionCall":{"name":"lookup","args":{"city":"allowed-tool-output"}}}]}}]}`
	for _, stream := range []bool{false, true} {
		for _, flag := range []string{"FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES"} {
			t.Run(fmt.Sprintf("stream=%t/%s", stream, flag), func(t *testing.T) {
				qClearEnv(t)
				t.Setenv(flag, "true")
				c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
					b, _ := io.ReadAll(r.Body)
					for _, value := range []string{qRequest, qImage, imageURI, "synthetic-system-text", "allowed-tool-input", "allowed-tool-result"} {
						if !strings.Contains(string(b), value) {
							t.Errorf("privacy changed wire request: missing %s", value)
						}
					}
					qReply(response, stream)(w, r)
				}, true)
				contents := []*genai.Content{{Role: "user", Parts: []*genai.Part{
					{Text: qRequest},
					{InlineData: &genai.Blob{MIMEType: "image/png", Data: []byte("synthetic-image-bytes")}},
					{FileData: &genai.FileData{MIMEType: "image/png", FileURI: imageURI}},
					{FunctionCall: &genai.FunctionCall{Name: "lookup", Args: map[string]any{"city": "allowed-tool-input"}}},
					{FunctionResponse: &genai.FunctionResponse{Name: "lookup", Response: map[string]any{"answer": "allowed-tool-result"}}},
				}}}
				cfg := &genai.GenerateContentConfig{SystemInstruction: genai.NewContentFromText("synthetic-system-text", genai.RoleUser)}
				if err := qGenerate(c, context.Background(), stream, contents, cfg); err != nil {
					t.Fatal(err)
				}
				s := qOne(t, exp)
				all, attrs := qAll(s), qAttrs(s)
				secrets := map[string][]string{
					"FI_HIDE_INPUT_TEXT":   {qRequest, "synthetic-system-text"},
					"FI_HIDE_OUTPUT_TEXT":  {qAnswer},
					"FI_HIDE_INPUT_IMAGES": {qImage, imageURI},
				}
				for _, secret := range secrets[flag] {
					if strings.Contains(all, secret) {
						t.Errorf("%s leaked in exported span", secret)
					}
				}
				for _, value := range []string{"allowed-tool-input", "allowed-tool-result", `"role":"user"`} {
					if !strings.Contains(attrs["input.value"], value) {
						t.Errorf("allowed input missing: %s", value)
					}
				}
				if !strings.Contains(attrs["output.value"], "allowed-tool-output") {
					t.Error("allowed output tool data missing")
				}
				if flag == "FI_HIDE_INPUT_MESSAGES" || flag == "FI_HIDE_OUTPUT_MESSAGES" {
					prefix, key, value := "gen_ai.input.messages.", "input.value", qRequest
					if flag == "FI_HIDE_OUTPUT_MESSAGES" {
						prefix, key, value = "gen_ai.output.messages.", "output.value", qAnswer
					}
					for key := range attrs {
						if strings.HasPrefix(key, prefix) {
							t.Errorf("message-only switch kept %s", key)
						}
					}
					if !strings.Contains(attrs[key], value) {
						t.Error("message-only switch unexpectedly redacted aggregate")
					}
				}
			})
		}
	}
}

func TestQualificationBoundedPreviews(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, turns := range []int{1, 80} {
			t.Run(fmt.Sprintf("stream=%t/turns=%d", stream, turns), func(t *testing.T) {
				qClearEnv(t)
				contents := make([]*genai.Content, turns)
				for i := range contents {
					contents[i] = &genai.Content{Role: "user"}
					for j := 0; j < 40; j++ {
						contents[i].Parts = append(contents[i].Parts,
							&genai.Part{Text: fmt.Sprintf("turn-%d-part-%d", i, j)},
							&genai.Part{FunctionCall: &genai.FunctionCall{Name: "lookup", ID: fmt.Sprintf("call-%d", j), Args: map[string]any{"n": j}}})
					}
				}
				cfg := &genai.GenerateContentConfig{Temperature: genai.Ptr(float32(0.5)), MaxOutputTokens: 100, Tools: []*genai.Tool{{}}}
				for i := 0; i < 150; i++ {
					cfg.Tools[0].FunctionDeclarations = append(cfg.Tools[0].FunctionDeclarations, &genai.FunctionDeclaration{Name: fmt.Sprintf("tool_%d", i)})
				}
				var response map[string]any
				if err := json.Unmarshal([]byte(qBody), &response); err != nil {
					t.Fatal(err)
				}
				var candidates []any
				for i := 0; i < 80; i++ {
					candidates = append(candidates, map[string]any{"index": i, "finishReason": "STOP", "content": map[string]any{"role": "model", "parts": []any{
						map[string]any{"text": fmt.Sprintf("answer-%d", i)},
						map[string]any{"functionCall": map[string]any{"id": fmt.Sprintf("out-%d", i), "name": "lookup", "args": map[string]any{"n": i}}},
					}}})
				}
				response["candidates"] = candidates
				body, _ := json.Marshal(response)
				c, _, exp := qSetup(t, qReply(string(body), stream), true)
				ctx := traceaigenai.ContextWithUserID(traceaigenai.ContextWithSessionID(context.Background(), "test-session"), "test-user")
				if err := qGenerate(c, ctx, stream, contents, cfg); err != nil {
					t.Fatal(err)
				}
				s := qOne(t, exp)
				attrs := qAttrs(s)
				if s.DroppedAttributes != 0 {
					t.Errorf("default span limit dropped %d attributes", s.DroppedAttributes)
				}
				for key, want := range map[string]string{"gen_ai.usage.input_tokens": "10", "gen_ai.usage.output_tokens": "5", "gen_ai.usage.total_tokens": "15", "session.id": "test-session", "user.id": "test-user", "gen_ai.response.model": "gemini-test-version", "gen_ai.response.id": "synthetic-response", "http.response.status_code": "200"} {
					if attrs[key] != want {
						t.Errorf("%s=%q want %q", key, attrs[key], want)
					}
				}
				if !strings.Contains(attrs["input.value"], fmt.Sprintf("turn-%d-part-39", turns-1)) || !strings.Contains(attrs["output.value"], "answer-79") {
					t.Error("aggregate lost conversation outside flattened preview")
				}
			})
		}
	}
}

func TestQualificationUsagePresenceAndFinalChunk(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, tc := range []struct{ name, usage, input, tool, output, total string }{
			{"absent", `{}`, "", "", "", ""},
			{"zero", `{"promptTokenCount":0,"toolUsePromptTokenCount":0,"candidatesTokenCount":0,"totalTokenCount":0}`, "0", "0", "0", "0"},
			{"tool-only", `{"toolUsePromptTokenCount":7}`, "7", "7", "", ""},
			{"cache-only", `{"cachedContentTokenCount":4}`, "", "", "", ""},
			{"complete", `{"promptTokenCount":10,"toolUsePromptTokenCount":7,"candidatesTokenCount":3,"thoughtsTokenCount":2,"totalTokenCount":22,"cachedContentTokenCount":4}`, "17", "7", "5", "22"},
			{"provider-total", `{"promptTokenCount":10,"toolUsePromptTokenCount":7,"totalTokenCount":99}`, "17", "7", "", "99"},
		} {
			t.Run(fmt.Sprintf("stream=%t/%s", stream, tc.name), func(t *testing.T) {
				qClearEnv(t)
				c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
					if stream {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprint(w, "data: {\"usageMetadata\":{\"promptTokenCount\":1,\"toolUsePromptTokenCount\":1,\"candidatesTokenCount\":1,\"totalTokenCount\":3}}\n\n")
					}
					qReply(`{"usageMetadata":`+tc.usage+`}`, stream)(w, r)
				}, true)
				if err := qGenerate(c, context.Background(), stream, genai.Text(qRequest), nil); err != nil {
					t.Fatal(err)
				}
				attrs := qAttrs(qOne(t, exp))
				for key, want := range map[string]string{"gen_ai.usage.input_tokens": tc.input, "gen_ai.usage.input_tokens.tool_use": tc.tool, "gen_ai.usage.output_tokens": tc.output, "gen_ai.usage.total_tokens": tc.total} {
					if attrs[key] != want {
						t.Errorf("%s=%q want=%q", key, attrs[key], want)
					}
				}
			})
		}
	}
}

func TestQualificationDecoderMatchesSDK(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, tc := range []struct {
			name, body string
			invalid    bool
		}{
			{"valid", qBody, false},
			{"empty-object", `{}`, false},
			{"array", `[]`, true},
			{"syntax", `synthetic-private-not-json`, true},
			{"text-type", `{"candidates":[{"content":{"parts":[{"text":123}]}}]}`, true},
			{"usage-type", `{"usageMetadata":{"toolUsePromptTokenCount":"synthetic-private-value"}}`, true},
			{"unused-accepted-field", `{"promptFeedback":{"blockReason":42}}`, true},
			{"unknown-field", `{"arbitraryProviderField":"synthetic-private-value"}`, false},
			{"top-level-casing", `{"Candidates":42,"UsageMetadata":42}`, false},
			{"ignored-candidate-field", `{"candidates":[{"Content":42,"finishMessage":42}]}`, false},
			{"duplicate-overwritten", `{"candidates":42,"candidates":[],"usageMetadata":{"promptTokenCount":"bad","promptTokenCount":10}}`, false},
			{"normalized-number", `{"usageMetadata":{"promptTokenCount":10.0}}`, false},
			{"http-response-field", `{"sdkHttpResponse":42}`, stream},
			{"citation-selection", `{"candidates":[{"citationMetadata":{"citations":42}}]}`, false},
			{"citation-type", `{"candidates":[{"citationMetadata":{"citationSources":[{"startIndex":"synthetic-private-value"}]}}]}`, true},
		} {
			t.Run(fmt.Sprintf("stream=%t/%s", stream, tc.name), func(t *testing.T) {
				qClearEnv(t)
				var originalErr string
				for _, instrument := range []bool{false, true} {
					c, _, exp := qSetup(t, qReply(tc.body, stream), instrument)
					err := qGenerate(c, context.Background(), stream, genai.Text(qRequest), nil)
					if (err != nil) != tc.invalid {
						t.Fatalf("SDK oracle invalid=%t err=%v", tc.invalid, err)
					}
					if !instrument {
						if err != nil {
							originalErr = err.Error()
						}
						continue
					}
					if err != nil && err.Error() != originalErr {
						// The SDK embeds HTTP headers (including Date) in some unary
						// decoder errors; verify the stable decoder classification.
						if !strings.Contains(err.Error(), "unmarshal") || !strings.Contains(originalErr, "unmarshal") {
							t.Errorf("instrumentation changed SDK error: %v", err)
						}
					}
					s := qOne(t, exp)
					if (s.Status.Code == codes.Error) != tc.invalid {
						t.Errorf("SDK invalid=%t but span status=%v", tc.invalid, s.Status)
					}
					if tc.invalid && qAttrs(s)["error.type"] != "decode" {
						t.Errorf("error.type=%q want decode", qAttrs(s)["error.type"])
					}
					for _, secret := range []string{"synthetic-private-not-json", "synthetic-private-value"} {
						if strings.Contains(qAll(s), secret) {
							t.Error("decoder body or details exported")
						}
					}
				}
			})
		}
	}
}

func TestQualificationEmptyUnaryResponseAccepted(t *testing.T) {
	qClearEnv(t)
	c, _, exp := qSetup(t, qReply("", false), true)
	if err := qGenerate(c, context.Background(), false, genai.Text(qRequest), nil); err != nil {
		t.Fatal(err)
	}
	if s := qOne(t, exp); s.Status.Code == codes.Error {
		t.Errorf("empty response rejected: %v", s.Status)
	}
}
