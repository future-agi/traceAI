package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/codes"
	"google.golang.org/genai"
)

const toolChoiceKey = "gen_ai.request.tool_choice"
const builtinToolsKey = "gen_ai.request.builtin_tools"
const groundingKey = "gen_ai.response.grounding"
const groundingBody = `{"candidates":[{"index":0,"content":{"role":"model","parts":[{"text":"Grounded answer"}]},"finishReason":"STOP","groundingMetadata":{"webSearchQueries":["fixture research"],"groundingChunks":[{"web":{"uri":"https://fixture.invalid/retrieved-source","title":"Fixture source"}}],"searchEntryPoint":{"renderedContent":"synthetic-private-html","sdkBlob":"c3ludGhldGlj"}},"citationMetadata":{"citationSources":[{"uri":"https://fixture.invalid/citation","startIndex":0,"endIndex":8,"license":"CC-BY"}]}}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5,"totalTokenCount":15}}`

func TestToolChoiceCapture(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, mode := range []string{"AUTO", "ANY", "NONE", "VALIDATED"} {
			t.Run(fmt.Sprintf("stream=%t/%s", stream, mode), func(t *testing.T) {
				qClearEnv(t)
				c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
					body, _ := io.ReadAll(r.Body)
					var req struct {
						ToolConfig struct {
							FunctionCallingConfig struct {
								Mode                 string   `json:"mode"`
								AllowedFunctionNames []string `json:"allowedFunctionNames"`
							} `json:"functionCallingConfig"`
						} `json:"toolConfig"`
					}
					if err := json.Unmarshal(body, &req); err != nil {
						t.Error(err)
					}
					f := req.ToolConfig.FunctionCallingConfig
					if f.Mode != mode || len(f.AllowedFunctionNames) != 2 || f.AllowedFunctionNames[0] != "lookup_one" {
						t.Errorf("wire constraints changed: %+v", f)
					}
					qReply(qBody, stream)(w, r)
				}, true)
				cfg := &genai.GenerateContentConfig{
					Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{{Name: "lookup_one"}, {Name: "lookup_two"}}}},
					ToolConfig: &genai.ToolConfig{FunctionCallingConfig: &genai.FunctionCallingConfig{
						Mode: genai.FunctionCallingConfigMode(mode), AllowedFunctionNames: []string{"lookup_one", "lookup_two"},
					}},
				}
				if err := qGenerate(c, context.Background(), stream, genai.Text(qRequest), cfg); err != nil {
					t.Fatal(err)
				}
				var choice struct {
					Mode  string   `json:"mode"`
					Names []string `json:"allowed_function_names"`
				}
				value := qAttrs(qOne(t, exp))[toolChoiceKey]
				if err := json.Unmarshal([]byte(value), &choice); err != nil {
					t.Fatalf("missing tool selection constraints: %q (%v)", value, err)
				}
				if choice.Mode != strings.ToLower(mode) || len(choice.Names) != 2 || choice.Names[1] != "lookup_two" {
					t.Errorf("tool constraints lost: %+v", choice)
				}
			})
		}
	}
}

func TestBuiltinToolCapture(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			qClearEnv(t)
			c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
				b, _ := io.ReadAll(r.Body)
				if !strings.Contains(string(b), `"googleSearch"`) {
					t.Error("SDK search tool missing on wire")
				}
				qReply(qBody, stream)(w, r)
			}, true)
			cfg := &genai.GenerateContentConfig{Tools: []*genai.Tool{{GoogleSearch: &genai.GoogleSearch{}}}}
			if err := qGenerate(c, context.Background(), stream, genai.Text(qRequest), cfg); err != nil {
				t.Fatal(err)
			}
			if value := qAttrs(qOne(t, exp))[builtinToolsKey]; !strings.Contains(value, "googleSearch") {
				t.Errorf("built-in tool identity lost: %q", value)
			}
		})
	}
}

func TestGroundingCapture(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			qClearEnv(t)
			c, _, exp := qSetup(t, qReply(groundingBody, stream), true)
			check := func(r *genai.GenerateContentResponse) {
				t.Helper()
				if r.Text() != "Grounded answer" || len(r.Candidates) != 1 || r.Candidates[0].GroundingMetadata == nil || len(r.Candidates[0].GroundingMetadata.GroundingChunks) != 1 || r.Candidates[0].GroundingMetadata.GroundingChunks[0].Web.URI != "https://fixture.invalid/retrieved-source" {
					t.Fatalf("SDK grounding response changed: %+v", r)
				}
			}
			if stream {
				for r, err := range c.Models.GenerateContentStream(context.Background(), "gemini-test", genai.Text(qRequest), nil) {
					if err != nil {
						t.Fatal(err)
					}
					check(r)
				}
			} else {
				r, err := c.Models.GenerateContent(context.Background(), "gemini-test", genai.Text(qRequest), nil)
				if err != nil {
					t.Fatal(err)
				}
				check(r)
			}
			s := qOne(t, exp)
			attrs := qAttrs(s)
			if s.Status.Code == codes.Error || attrs["gen_ai.usage.total_tokens"] != "15" || attrs["output.value"] != "Grounded answer" {
				t.Errorf("successful response changed: %v %v", s.Status, attrs)
			}
			value := attrs[groundingKey]
			if !json.Valid([]byte(value)) {
				t.Errorf("missing structured grounding evidence: %q", value)
			}
			for _, want := range []string{"https://fixture.invalid/retrieved-source", "Fixture source", "fixture research", "https://fixture.invalid/citation"} {
				if !strings.Contains(value, want) {
					t.Errorf("missing grounding/citation evidence %q", want)
				}
			}
			if strings.Contains(qAll(s), "synthetic-private-html") || strings.Contains(qAll(s), "c3ludGhldGlj") {
				t.Error("opaque provider metadata exported")
			}
		})
	}
}
